"""Facebook Pages provider over Graph API v26.0.

M1 covers the read half (DESIGN.md section 5 Facebook): list_posts, get_post,
list_comments, get_insights. The write methods declared by the Provider
protocol land in M2 behind the Publisher Lambda; here they raise
NotImplementedError so a tool-level accident fails loudly rather than
silently calling a stub.
"""

import json
import logging

from sharedModules import crypto, dynamo, registry
from sharedModules.errors import AccountError, CapabilityError
from sharedModules.models import (
    Comment,
    Insights,
    Post,
    PostSummary,
    ProfileInfo,
    ProfilePatch,
    PublishResult,
)

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
            Capability.PROFILE_READ,
            Capability.PROFILE_EDIT,
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
            "id,message,created_time,permalink_url,attachments{media_type,media,subattachments}"
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
                "id,message,created_time,permalink_url,attachments{media_type,media,subattachments}"
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

    def fetch_comment(self, comment_id: str) -> dict:
        """One-shot GET /{comment_id}?fields=... for webhookIngest.

        Returns a dict shaped like what list_comments produces, with
        enough fields to populate the stored COMMENT_EVENT item. Called
        on first sight of a comment id on a Page webhook; subsequent
        events for the same id are deduped before this method is called
        again.
        """
        params = {"fields": "id,from,message,created_time,is_hidden,comment_count,parent"}
        raw = self._client.get(f"/{comment_id}", params=params) or {}
        parent = (raw.get("parent") or {}).get("id") or raw.get("id", "")
        return {
            "comment_id": raw.get("id", ""),
            "created_time": raw.get("created_time", ""),
            "author": (raw.get("from") or {}).get("name", ""),
            "text": raw.get("message", ""),
            "is_hidden": bool(raw.get("is_hidden", False)),
            "thread_id": parent,
            "reply_count": int(raw.get("comment_count", 0)),
        }

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

    def publish(self, draft) -> PublishResult:
        """Publish a draft to the Page.

        Kind-specific helpers each return the Meta-native post id; this
        method composes the permalink and the unified `PublishResult`.
        Scheduling is deliberately NOT delegated to Meta (DESIGN.md
        section 7.3): a draft carrying a future `schedule` is still
        published immediately when this method is called, because the
        Publisher Lambda is itself invoked at the scheduled moment by
        EventBridge Scheduler, which gives us a second approval re-check
        at publish time.
        """
        kind = (getattr(draft, "kind", "") or "").lower()
        record = registry.account_record(self.alias)
        page_id = record["account_id"]
        if not page_id:
            raise AccountError(
                f"account '{self.alias}' is not connected; "
                f"run `python scripts/admin.py connect --alias {self.alias}`"
            )

        if kind == "text":
            post_id = self._publish_text(page_id, draft)
        elif kind == "link":
            post_id = self._publish_link(page_id, draft)
        elif kind == "image":
            post_id = self._publish_photo(page_id, draft)
        elif kind == "multi_image":
            post_id = self._publish_multi_photo(page_id, draft)
        elif kind == "video":
            post_id = self._publish_video(page_id, draft)
        elif kind == "reel":
            raise NotImplementedError(
                "Facebook reel publishing lands in a later milestone "
                "(requires the resumable upload API on rupload.facebook.com)"
            )
        else:
            raise CapabilityError(
                f"kind '{kind}' is not supported on facebook; "
                "use text, link, image, multi_image, or video"
            )

        return PublishResult(
            account=self.alias,
            post_id=post_id,
            permalink=f"https://www.facebook.com/{post_id}",
            kind=kind,
        )

    def edit_post(self, post_id: str, text: str) -> None:
        """Edit an existing Page post's message text.

        Meta only allows edits on posts created by the same app
        (DESIGN.md section 5 Facebook). A foreign-authored target comes
        back as a 200-level AccountError via metaClient's error mapping;
        this method surfaces that specifically so the operator sees the
        actual reason, not "edit failed".
        """
        try:
            self._client.post(f"/{post_id}", data={"message": text or ""})
        except AccountError as e:
            msg = str(e)
            if "token does not belong" in msg.lower() or "foreign" in msg.lower():
                raise AccountError(
                    f"cannot edit post '{post_id}': edits only work on posts "
                    "the same Meta app created"
                ) from e
            raise

    # Protocol-shim retained for callers that still look for .edit(...)
    def edit(self, post_id: str, patch: dict) -> None:
        self.edit_post(post_id, (patch or {}).get("text", "") or "")

    def delete_post(self, post_id: str) -> None:
        """Delete a Page post (reversible for a short Meta-side window)."""
        self._client.delete(f"/{post_id}")

    # Protocol-shim retained for callers that still look for .delete(...)
    def delete(self, post_id: str) -> None:
        self.delete_post(post_id)

    def reply_to_comment(self, comment_id: str, text: str) -> Comment:
        """Post a reply under an existing comment and return the created comment."""
        resp = self._client.post(f"/{comment_id}/comments", data={"message": text or ""})
        new_id = str(resp.get("id") or "")
        return Comment(
            account=self.alias,
            comment_id=new_id,
            post_id="",  # Meta does not return the parent post id on reply create
            thread_id=comment_id,
            created_time="",
            author="",
            text=text or "",
            is_hidden=False,
            reply_count=0,
        )

    def hide_comment(self, comment_id: str) -> None:
        """Hide a comment on a Page post (reversible via Meta's own UI)."""
        self._client.post(f"/{comment_id}", data={"is_hidden": "true"})

    # ---------- profile ops (M9) ----------

    def get_profile(self) -> ProfileInfo:
        """Fetch the Page's public-facing profile.

        One GET against `/{page-id}` with the writable text fields plus
        `picture` and `cover`. The Graph API returns `emails` as a list
        and `picture` as a nested object; the mapping below collapses
        those into the unified ProfileInfo shape.
        """
        record = registry.account_record(self.alias)
        page_id = record["account_id"] or "me"
        fields = (
            "id,name,username,about,description,website,phone,emails,"
            "category,picture.width(1080),cover"
        )
        data = self._client.get(f"/{page_id}", params={"fields": fields})
        picture_url = ((data.get("picture") or {}).get("data") or {}).get("url", "") or ""
        cover_url = (data.get("cover") or {}).get("source", "") or ""
        return ProfileInfo(
            account=self.alias,
            handle=data.get("username", "") or "",
            display_name=data.get("name", "") or "",
            about=data.get("about", "") or "",
            description=data.get("description", "") or "",
            website=data.get("website", "") or "",
            phone=data.get("phone", "") or "",
            emails=list(data.get("emails", []) or []),
            picture_url=picture_url,
            cover_url=cover_url,
            category=data.get("category", "") or "",
        )

    def update_profile(self, patch: ProfilePatch) -> None:
        """Apply the non-empty text fields of `patch` to the Page.

        Meta's `/{page-id}` POST accepts `about`, `description`, `website`,
        `phone`, and `emails`. Scope requirement: `pages_manage_metadata`.
        `emails` is comma-joined per the Graph wire format. The Page's
        display name (`name`) is deliberately omitted here: changing a
        Page's name requires Meta's name-change review workflow (which
        is not a Graph API call), and the shim-level `display_name` on
        ProfilePatch is reserved for providers that do support it (today
        Bluesky).
        """
        data = _profile_patch_wire(patch)
        if not data:
            return
        self._client.post(f"/{self._page_id()}", data=data)

    def _page_id(self) -> str:
        record = registry.account_record(self.alias)
        page_id = record.get("account_id") or ""
        if not page_id:
            raise AccountError(
                f"account '{self.alias}' is not connected; "
                f"run `python scripts/admin.py connect --alias {self.alias}`"
            )
        return page_id

    # ---------- publish helpers ----------

    def _publish_text(self, page_id: str, draft) -> str:
        resp = self._client.post(
            f"/{page_id}/feed",
            data={"message": getattr(draft, "text", "") or ""},
        )
        return str(resp.get("id") or resp.get("post_id") or "")

    def _publish_link(self, page_id: str, draft) -> str:
        resp = self._client.post(
            f"/{page_id}/feed",
            data={
                "message": getattr(draft, "text", "") or "",
                "link": getattr(draft, "link", "") or "",
            },
        )
        return str(resp.get("id") or resp.get("post_id") or "")

    def _publish_photo(self, page_id: str, draft) -> str:
        urls = _media_urls(draft)
        if not urls:
            raise AccountError("image draft has no media url")
        resp = self._client.post(
            f"/{page_id}/photos",
            data={
                "url": urls[0],
                "caption": getattr(draft, "text", "") or "",
            },
        )
        # /PAGE/photos returns {"id": "<photo_id>", "post_id": "<page_id>_<post_id>"}
        return str(resp.get("post_id") or resp.get("id") or "")

    def _publish_multi_photo(self, page_id: str, draft) -> str:
        urls = _media_urls(draft)
        if len(urls) < 2:
            raise AccountError("multi_image needs at least two media urls")
        attached: list[dict] = []
        for url in urls:
            child = self._client.post(
                f"/{page_id}/photos",
                data={"url": url, "published": "false"},
            )
            fbid = str(child.get("id") or "")
            if not fbid:
                raise AccountError("Meta returned no photo id for a multi-photo child")
            attached.append({"media_fbid": fbid})
        resp = self._client.post(
            f"/{page_id}/feed",
            data={
                "message": getattr(draft, "text", "") or "",
                # Graph accepts `attached_media[0]`, `attached_media[1]`, ...
                # as repeated form fields, which httpx encodes from a list:
                "attached_media": json.dumps(attached),
            },
        )
        return str(resp.get("id") or resp.get("post_id") or "")

    def _publish_video(self, page_id: str, draft) -> str:
        urls = _media_urls(draft)
        if not urls:
            raise AccountError("video draft has no media url")
        resp = self._client.post(
            f"/{page_id}/videos",
            data={
                "file_url": urls[0],
                "description": getattr(draft, "text", "") or "",
            },
        )
        # /PAGE/videos returns {"id": "<video_id>"}
        return str(resp.get("id") or "")

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


def _profile_patch_wire(patch: ProfilePatch) -> dict:
    """Produce the Graph POST form for a Page profile update.

    Only non-empty writable fields are included so the operator can approve
    a partial change. `display_name` is deliberately dropped: Meta's Page
    name changes do not go through the Graph `/{page-id}` POST.
    """
    data: dict[str, str] = {}
    for key in ("about", "description", "website", "phone"):
        value = getattr(patch, key, "") or ""
        if value:
            data[key] = value
    emails = [e for e in (getattr(patch, "emails", []) or []) if e]
    if emails:
        data["emails"] = ",".join(emails)
    return data


def _media_urls(draft) -> list[str]:
    """Pull https URLs from a draft's media list.

    M2 accepts https URLs that Meta fetches directly; S3 staging and
    presigned URLs land with Instagram publishing in M5 (DESIGN.md 9).
    """
    urls: list[str] = []
    for m in getattr(draft, "media", []) or []:
        if hasattr(m, "key"):
            key = getattr(m, "key", "") or ""
            source = getattr(m, "source", "url")
        elif isinstance(m, dict):
            key = m.get("key", "") or m.get("url", "")
            source = m.get("source", "url")
        else:
            key = str(m)
            source = "url"
        if source != "url" or not key.startswith("https://"):
            raise CapabilityError(
                "facebook publish accepts only https media URLs in M2; "
                "S3 staging lands with Instagram publishing in M5"
            )
        urls.append(key)
    return urls


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
        "views": ins.views,
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
