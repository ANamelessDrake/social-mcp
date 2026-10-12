"""Instagram provider over Graph API v26.0.

Instagram business and creator accounts are reached via the Page they are
linked to; the Page token is reused as the IG access token on the same
Graph endpoint. See DESIGN.md section 5 Instagram for the capability set
and section 3.3 for the connect flow that stores both the Page and IG
account items.

M4 covers the read half: list_posts, get_post, list_comments, get_insights.
Publishing (M5) stays NotImplementedError here; the container flow and S3
staging land with the s3Stack in that milestone.
"""

import logging

from sharedModules import crypto, dynamo, registry
from sharedModules.errors import AccountError, CapabilityError
from sharedModules.models import Comment, Insights, Post, PostSummary, PublishResult

from .base import Capability, ProviderLimits
from .metaClient import GraphClient

log = logging.getLogger(__name__)

# Insights metric sets per media type. Deprecated entries that Meta still
# accepts but which return zeroed data (or outright error) are deliberately
# absent: `impressions` (media level, deprecated 2025-01-21), `plays`,
# `clips_replays_count`, `ig_reels_aggregated_all_plays_count`.
IMAGE_METRICS = [
    "reach",
    "likes",
    "comments",
    "shares",
    "saved",
    "total_interactions",
]
CAROUSEL_METRICS = list(IMAGE_METRICS)  # Carousel-level only; per-child is not supported
VIDEO_METRICS = [
    "reach",
    "views",
    "likes",
    "comments",
    "shares",
    "saved",
    "total_interactions",
    "ig_reels_video_view_total_time",
]
REEL_METRICS = list(VIDEO_METRICS)

# Deprecated metrics we must never request. Enforced by test.
BANNED_METRICS = frozenset(
    {
        "impressions",  # media-level, deprecated 2025-01-21
        "plays",
        "clips_replays_count",
        "ig_reels_aggregated_all_plays_count",
    }
)

# IG media_type -> unified kind. REELS and VIDEO are distinct here because
# `get_insights` picks a slightly different metric set for each, and
# `publish` (M5) will have different container flows.
_KIND_BY_MEDIA_TYPE = {
    "IMAGE": "image",
    "VIDEO": "video",
    "CAROUSEL_ALBUM": "multi_image",
    "REELS": "reel",
}


class InstagramProvider:
    name = "instagram"
    capabilities = frozenset(
        {
            # No LINK: Instagram does not render URL previews from captions.
            # No EDIT: Meta's IG media endpoints do not support content edits.
            Capability.TEXT,
            Capability.IMAGE,
            Capability.MULTI_IMAGE,
            Capability.VIDEO,
            Capability.REEL,
            Capability.SCHEDULE,
            Capability.DELETE,
            Capability.COMMENTS,
            Capability.INSIGHTS,
        }
    )
    # Instagram caption cap is 2200 chars; carousel children max 10;
    # hashtag cap is 30.
    limits = ProviderLimits(max_text_length=2200, max_media_per_post=10, max_hashtags=30)

    def __init__(self, alias: str, *, _client: GraphClient | None = None) -> None:
        self.alias = alias
        self._client = _client or GraphClient(
            access_token_fn=self._access_token,
            refresh_fn=self._refresh_tokens,
        )

    # ---------- read ops (M4) ----------

    def list_posts(self, since_days: int, limit: int) -> list[PostSummary]:
        registry.require_scope(self.alias, "instagram_basic")
        record = registry.account_record(self.alias)
        ig_user_id = record["account_id"] or "me"
        fields = (
            "id,caption,timestamp,permalink,media_type,"
            "media_url,thumbnail_url,children{id}"
        )
        params = {"fields": fields, "limit": max(1, min(limit, 50))}
        if since_days > 0:
            params["since"] = f"-{since_days} days"
        data = self._client.get(f"/{ig_user_id}/media", params=params)
        return [self._to_summary(item) for item in data.get("data", [])]

    def get_post(self, post_id: str) -> Post:
        registry.require_scope(self.alias, "instagram_basic")
        fields = (
            "id,caption,timestamp,permalink,media_type,"
            "media_url,thumbnail_url,like_count,comments_count,"
            "children{id,media_type,media_url,thumbnail_url}"
        )
        item = self._client.get(f"/{post_id}", params={"fields": fields})
        summary = self._to_summary(item)
        # Engagement from the direct counters IG exposes on the media node
        summary.engagement = {
            "likes": int(item.get("like_count", 0) or 0),
            "comments": int(item.get("comments_count", 0) or 0),
        }
        try:
            insights = self.get_insights(post_id, _media_type_hint=item.get("media_type", ""))
        except (AccountError, CapabilityError) as exc:
            log.info("instagram insights unavailable for %s: %s", post_id, exc)
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
            full_text=item.get("caption", "") or "",
            media=_extract_media(item),
            insights=_insights_dict(insights),
        )

    def list_comments(self, post_id: str, limit: int) -> list[Comment]:
        registry.require_scope(self.alias, "instagram_basic")
        fields = (
            "id,text,username,timestamp,like_count,"
            "replies{id,text,username,timestamp,like_count}"
        )
        params = {"fields": fields, "limit": max(1, min(limit, 50))}
        data = self._client.get(f"/{post_id}/comments", params=params)
        out: list[Comment] = []
        for raw in data.get("data", []):
            top_id = raw.get("id", "")
            out.append(self._to_comment(post_id, raw, thread_id=top_id))
            for reply in (raw.get("replies") or {}).get("data", []) or []:
                out.append(self._to_comment(post_id, reply, thread_id=top_id))
        return out

    def get_insights(self, post_id: str, *, _media_type_hint: str = "") -> Insights:
        registry.require_scope(self.alias, "instagram_manage_insights")
        media_type = (_media_type_hint or "").upper()
        if not media_type:
            # One extra round trip to learn the type when we were not told
            type_resp = self._client.get(f"/{post_id}", params={"fields": "media_type"})
            media_type = (type_resp.get("media_type") or "").upper()
        metrics = _metrics_for(media_type)
        # Belt-and-braces: never let a deprecated metric slip into a request
        safe = [m for m in metrics if m not in BANNED_METRICS]
        data = self._client.get(f"/{post_id}/insights", params={"metric": ",".join(safe)})
        return _to_insights(self.alias, post_id, data)

    # ---------- write ops (M5) ----------

    def publish(self, draft) -> PublishResult:
        raise NotImplementedError("instagram publishing lands in M5")

    def edit(self, post_id: str, patch: dict) -> None:
        raise NotImplementedError("Instagram does not support post edits")

    def edit_post(self, post_id: str, text: str) -> None:
        raise NotImplementedError("Instagram does not support post edits")

    def delete(self, post_id: str) -> None:
        raise NotImplementedError("Instagram delete lands in M5")

    def delete_post(self, post_id: str) -> None:
        raise NotImplementedError("Instagram delete lands in M5")

    def reply_to_comment(self, comment_id: str, text: str) -> Comment:
        raise NotImplementedError("Instagram comment replies land in M5")

    def hide_comment(self, comment_id: str) -> None:
        raise NotImplementedError("Instagram comment hides land in M5")

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
        """Mark the account `needs_reconnect` on 190 (same pattern as Facebook).

        IG uses the Page token; a dead IG token usually means a dead Page
        token upstream, so autonomous re-mint lives with the Facebook refresh
        work in a later milestone.
        """
        dynamo.mark_needs_reconnect(self.alias)
        raise AccountError(
            f"account '{self.alias}' needs reconnecting; "
            f"run `python scripts/admin.py connect --alias {self.alias}`"
        )

    # ---------- mapping ----------

    def _to_summary(self, item: dict) -> PostSummary:
        media_type = (item.get("media_type") or "").upper()
        kind = _KIND_BY_MEDIA_TYPE.get(media_type, "image")
        children = (item.get("children") or {}).get("data", []) or []
        if kind == "multi_image":
            media_count = max(1, len(children))
        elif kind in ("image", "video", "reel"):
            media_count = 1 if item.get("media_url") else 0
        else:
            media_count = 0
        return PostSummary(
            account=self.alias,
            post_id=item.get("id", ""),
            kind=kind,
            permalink=item.get("permalink", ""),
            created_time=item.get("timestamp", ""),
            text=_truncate(item.get("caption", "") or "", 300),
            media_count=media_count,
            engagement={},
        )

    def _to_comment(self, post_id: str, raw: dict, *, thread_id: str) -> Comment:
        return Comment(
            account=self.alias,
            comment_id=raw.get("id", ""),
            post_id=post_id,
            thread_id=thread_id,
            created_time=raw.get("timestamp", ""),
            author=raw.get("username", "") or "",
            text=raw.get("text", "") or "",
            is_hidden=False,  # IG surfaces hidden-ness only on the ownership path
            reply_count=0,
        )


# ---------- module-level pure helpers (unit-testable without HTTP) ----------


def _metrics_for(media_type: str) -> list[str]:
    mt = (media_type or "").upper()
    if mt == "IMAGE":
        return list(IMAGE_METRICS)
    if mt == "CAROUSEL_ALBUM":
        return list(CAROUSEL_METRICS)
    if mt == "VIDEO":
        return list(VIDEO_METRICS)
    if mt == "REELS":
        return list(REEL_METRICS)
    # Unknown media type: fall back to the image set, which is the smallest
    # strict subset Meta accepts for every type.
    return list(IMAGE_METRICS)


def _extract_media(item: dict) -> list[dict]:
    children = (item.get("children") or {}).get("data", []) or []
    if children:
        return [_media_record(c) for c in children]
    if item.get("media_url") or item.get("thumbnail_url"):
        return [_media_record(item)]
    return []


def _media_record(child: dict) -> dict:
    media_type = (child.get("media_type") or "").lower()
    return {
        "type": media_type,
        "url": child.get("media_url", "") or "",
        "thumbnail": child.get("thumbnail_url", "") or child.get("media_url", "") or "",
    }


def _to_insights(alias: str, post_id: str, data: dict) -> Insights:
    """Collapse the IG insights response into the unified shape."""
    values: dict[str, int] = {}
    for entry in data.get("data", []):
        name = entry.get("name", "")
        points = entry.get("values", []) or []
        if not points:
            continue
        value = points[-1].get("value", 0)
        if isinstance(value, dict):
            value = sum(v for v in value.values() if isinstance(v, (int, float)))
        values[name] = int(value or 0)
    return Insights(
        account=alias,
        post_id=post_id,
        # Meta deprecated the media-level `impressions` metric; IG posts
        # expose reach + views instead.
        impressions=0,
        reach=values.get("reach", 0),
        views=values.get("views", 0),
        engagements=values.get("total_interactions", 0),
        # IG has one reaction type (like); map it so the shape is uniform.
        reactions=values.get("likes", 0),
        comments=values.get("comments", 0),
        shares=values.get("shares", 0),
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
