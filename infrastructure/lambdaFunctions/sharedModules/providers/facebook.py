"""Facebook Pages provider over Graph API v26.0.

M1 covers the read half (DESIGN.md section 5 Facebook): list_posts, get_post,
list_comments, get_insights. The write methods declared by the Provider
protocol land in M2 behind the Publisher Lambda; here they raise
NotImplementedError so a tool-level accident fails loudly rather than
silently calling a stub.
"""

import logging

from sharedModules import crypto, dynamo, registry
from sharedModules.errors import AccountError, CapabilityError
from sharedModules.models import Comment, Insights, Post, PostSummary

from .base import Capability, ProviderLimits
from .metaClient import GraphClient

log = logging.getLogger(__name__)

# Post-level insights we expose today. The `*_unique` family was deprecated
# at v22 and bare `post_impressions*` on the 2026-06-15 wave, so neither is
# requested. Mapping to the unified Insights shape lives in `_to_insights`.
POST_METRICS = [
    "post_impressions_organic",
    "post_impressions_paid",
    "post_impressions_viral",
    "post_impressions_nonviral",
    "post_clicks",
    "post_reactions_like_total",
    "post_reactions_love_total",
    "post_reactions_wow_total",
    "post_reactions_haha_total",
    "post_reactions_sorry_total",
    "post_reactions_anger_total",
]


class FacebookProvider:
    name = "facebook"
    capabilities = frozenset(
        {
            Capability.TEXT,
            Capability.LINK,
            Capability.IMAGE,
            Capability.MULTI_IMAGE,
            Capability.VIDEO,
            Capability.REEL,
            Capability.SCHEDULE,
            Capability.EDIT,
            Capability.DELETE,
            Capability.COMMENTS,
            Capability.INSIGHTS,
        }
    )
    # Facebook does not publish hard limits for text message length on Pages
    # posts; what exists is the long-form Note cap (~63k chars) and a soft
    # readability threshold. Caller-side policy caps hashtags; the provider
    # does not fight it.
    limits = ProviderLimits(max_text_length=63000, max_media_per_post=30, max_hashtags=-1)

    def __init__(self, alias: str, *, _client: GraphClient | None = None) -> None:
        self.alias = alias
        self._client = _client or GraphClient(
            access_token_fn=self._access_token,
            refresh_fn=self._refresh_tokens,
        )

    # ---------- read ops ----------

    def list_posts(self, since_days: int, limit: int) -> list[PostSummary]:
        record = registry.account_record(self.alias)
        page_id = record["account_id"] or "me"
        fields = (
            "id,message,created_time,permalink_url,"
            "attachments{media_type,media,subattachments}"
        )
        params = {"fields": fields, "limit": max(1, min(limit, 50))}
        if since_days > 0:
            # Graph accepts strtotime-parseable `since`; "-N days" is clearest.
            params["since"] = f"-{since_days} days"
        data = self._client.get(f"/{page_id}/posts", params=params)
        return [self._to_summary(item) for item in data.get("data", [])]

    def get_post(self, post_id: str) -> Post:
        params = {
            "fields": (
                "id,message,created_time,permalink_url,"
                "attachments{media_type,media,subattachments}"
            ),
        }
        item = self._client.get(f"/{post_id}", params=params)
        summary = self._to_summary(item)
        try:
            insights = self.get_insights(post_id)
        except (AccountError, CapabilityError) as exc:
            log.info("insights unavailable for %s: %s", post_id, exc)
            insights = Insights(account=self.alias, post_id=post_id)
        return Post(
            account=summary.account,
            post_id=summary.post_id,
            kind=summary.kind,
            permalink=summary.permalink,
            created_time=summary.created_time,
            text=summary.text,
            media_count=summary.media_count,
            engagement=summary.engagement,
            full_text=item.get("message", ""),
            media=_extract_media(item),
            insights=_insights_dict(insights),
        )

    def list_comments(self, post_id: str, limit: int) -> list[Comment]:
        params = {
            "fields": "id,from,message,created_time,is_hidden,comment_count,parent",
            "limit": max(1, min(limit, 100)),
            # Flat ordering so nested replies land next to their parent for
            # the thread_id mapping below.
            "order": "chronological",
        }
        data = self._client.get(f"/{post_id}/comments", params=params)
        return [self._to_comment(post_id, c) for c in data.get("data", [])]

    def get_insights(self, post_id: str) -> Insights:
        params = {"metric": ",".join(POST_METRICS)}
        data = self._client.get(f"/{post_id}/insights", params=params)
        return _to_insights(self.alias, post_id, data)

    # ---------- write ops (M2) ----------

    def publish(self, draft, account) -> dict:  # pragma: no cover - M2
        raise NotImplementedError("Facebook publish lands in milestone 2")

    def edit(self, post_id: str, patch: dict) -> None:  # pragma: no cover - M3
        raise NotImplementedError("Facebook edit lands in milestone 3")

    def delete(self, post_id: str) -> None:  # pragma: no cover - M3
        raise NotImplementedError("Facebook delete lands in milestone 3")

    def reply_to_comment(self, comment_id: str, text: str) -> Comment:  # pragma: no cover - M3
        raise NotImplementedError("reply_to_comment lands in milestone 3")

    def hide_comment(self, comment_id: str) -> None:  # pragma: no cover - M3
        raise NotImplementedError("hide_comment lands in milestone 3")

    # ---------- token plumbing ----------

    def _access_token(self) -> str:
        record = registry.account_record(self.alias)
        if record.get("status") != "active" or not record.get("page_token_ct"):
            raise AccountError(
                f"account '{self.alias}' is not connected; "
                f"run `python scripts/admin.py connect --alias {self.alias}`"
            )
        return crypto.decrypt(record["page_token_ct"])

    def _refresh_tokens(self, _stale: str) -> str:
        """Re-mint the Page token from the long-lived user token.

        The user token refresh and `/me/accounts` call live in
        `scripts/admin.py` today; here we mark the account `needs_reconnect`
        so the next tool call surfaces the exact command rather than looping.
        Automated server-side re-mint lands in a later milestone.
        """
        dynamo.mark_needs_reconnect(self.alias)
        raise AccountError(
            f"account '{self.alias}' needs reconnecting; "
            f"run `python scripts/admin.py connect --alias {self.alias}`"
        )

    # ---------- mapping ----------

    def _to_summary(self, item: dict) -> PostSummary:
        attachments = item.get("attachments", {}).get("data", [])
        kind = _kind_from_attachments(item, attachments)
        media_count = _media_count(attachments)
        return PostSummary(
            account=self.alias,
            post_id=item.get("id", ""),
            kind=kind,
            permalink=item.get("permalink_url", ""),
            created_time=item.get("created_time", ""),
            text=_truncate(item.get("message", "") or "", 300),
            media_count=media_count,
            engagement={},
        )

    def _to_comment(self, post_id: str, raw: dict) -> Comment:
        parent = (raw.get("parent") or {}).get("id") or raw.get("id", "")
        return Comment(
            account=self.alias,
            comment_id=raw.get("id", ""),
            post_id=post_id,
            thread_id=parent,
            created_time=raw.get("created_time", ""),
            author=(raw.get("from") or {}).get("name", ""),
            text=raw.get("message", ""),
            is_hidden=bool(raw.get("is_hidden", False)),
            reply_count=int(raw.get("comment_count", 0)),
        )


# ---------- module-level pure helpers (unit-testable without HTTP) ----------


def _kind_from_attachments(item: dict, attachments: list[dict]) -> str:
    if not attachments:
        return "text" if item.get("message") else "text"
    first = attachments[0]
    media_type = (first.get("media_type") or "").lower()
    sub = first.get("subattachments", {}).get("data", [])
    if media_type == "video" or media_type == "video_inline":
        return "video"
    if media_type == "video_autoplay" or media_type == "animated_image_autoplay":
        return "reel"
    if sub and len(sub) > 1:
        return "multi_image"
    if media_type in {"photo", "album"}:
        return "image"
    if media_type == "link":
        return "link"
    return "text"


def _media_count(attachments: list[dict]) -> int:
    if not attachments:
        return 0
    first = attachments[0]
    sub = first.get("subattachments", {}).get("data", [])
    return max(1, len(sub))


def _extract_media(item: dict) -> list[dict]:
    attachments = item.get("attachments", {}).get("data", [])
    out: list[dict] = []
    for att in attachments:
        sub = att.get("subattachments", {}).get("data", [])
        if sub:
            for s in sub:
                out.append(_media_record(s))
        else:
            out.append(_media_record(att))
    return out


def _media_record(att: dict) -> dict:
    media = att.get("media") or {}
    image = media.get("image") or {}
    return {
        "type": (att.get("media_type") or "").lower(),
        "url": image.get("src", "") or (media.get("source") or ""),
        "thumbnail": image.get("src", ""),
    }


def _to_insights(alias: str, post_id: str, data: dict) -> Insights:
    """Collapse the Graph insights response into the unified shape."""
    values: dict[str, int] = {}
    for entry in data.get("data", []):
        name = entry.get("name", "")
        points = entry.get("values", [])
        if not points:
            continue
        value = points[-1].get("value", 0)
        if isinstance(value, dict):
            # Some metrics return a {key: int} breakdown; sum it.
            value = sum(v for v in value.values() if isinstance(v, (int, float)))
        values[name] = int(value or 0)

    impressions = (
        values.get("post_impressions_organic", 0)
        + values.get("post_impressions_paid", 0)
        + values.get("post_impressions_viral", 0)
        + values.get("post_impressions_nonviral", 0)
    )
    reactions = sum(
        values.get(f"post_reactions_{r}_total", 0)
        for r in ("like", "love", "wow", "haha", "sorry", "anger")
    )
    return Insights(
        account=alias,
        post_id=post_id,
        impressions=impressions,
        reach=0,  # Page reach is a page-level metric, not per-post
        engagements=values.get("post_clicks", 0) + reactions,
        reactions=reactions,
        comments=0,  # the comments edge count is a separate call
        shares=0,
        source_metrics=values,
    )


def _insights_dict(ins: Insights) -> dict:
    return {
        "impressions": ins.impressions,
        "reach": ins.reach,
        "engagements": ins.engagements,
        "reactions": ins.reactions,
        "comments": ins.comments,
        "shares": ins.shares,
        "source_metrics": ins.source_metrics,
    }


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 1].rstrip() + "…"
