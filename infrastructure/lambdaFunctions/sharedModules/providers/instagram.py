"""Instagram provider over Graph API v26.0.

Instagram business and creator accounts are reached via the Page they are
linked to; the Page token is reused as the IG access token on the same
Graph endpoint. See DESIGN.md section 5 Instagram for the capability set
and section 3.3 for the connect flow that stores both the Page and IG
account items.

M4 covers the read half: list_posts, get_post, list_comments, get_insights.
M5 lands publishing (container flow + S3 media staging), delete, and the
`content_publishing_limit` precheck.
"""

import logging
import time

from sharedModules import crypto, dynamo, registry
from sharedModules.errors import AccountError, CapabilityError
from sharedModules.models import Comment, Insights, Post, PostSummary, PublishResult

from . import mediaStaging
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
# `publish` has different container flows for the two.
_KIND_BY_MEDIA_TYPE = {
    "IMAGE": "image",
    "VIDEO": "video",
    "CAROUSEL_ALBUM": "multi_image",
    "REELS": "reel",
}

# Container-flow polling schedule: 1s, 2s, 4s, 8s, ..., 60s capped,
# 600 seconds total. Status transitions are typically seconds for images,
# tens of seconds for video; reel can take longer when Meta transcodes.
_POLL_INITIAL_SECONDS = 1.0
_POLL_MAX_SECONDS = 60.0
_POLL_TOTAL_SECONDS = 600.0

# `content_publishing_limit` is cached in memory per account for one minute
# so a batch publish does not hit the limit endpoint on every call.
_LIMIT_CACHE_TTL = 60.0
_limit_cache: dict[str, tuple[float, dict]] = {}

# Safe assumption when the live quota is ambiguous (DESIGN.md section 5).
# Meta's docs list 50 and 100 in different sections of the same page.
_SAFE_DAILY_CAP = 50
_LOW_QUOTA_WARN = 2  # warn when remaining quota drops below this


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

    def fetch_comment(self, comment_id: str) -> dict:
        """One-shot GET /{comment_id}?fields=... for webhookIngest.

        Returns a dict shaped like what list_comments's inner mapping
        produces. IG comments webhooks fire on new comments; the
        standing scope is `instagram_manage_comments` (added at M3), so
        this call hits the Graph with the stored Page token.
        """
        registry.require_scope(self.alias, "instagram_manage_comments")
        params = {"fields": "id,text,username,timestamp,like_count"}
        raw = self._client.get(f"/{comment_id}", params=params) or {}
        return {
            "comment_id": raw.get("id", ""),
            "created_time": raw.get("timestamp", ""),
            "author": raw.get("username", ""),
            "text": raw.get("text", ""),
            "is_hidden": False,
            "thread_id": raw.get("id", ""),
            "reply_count": 0,
        }

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
        """Container-flow publish (DESIGN.md section 5, Instagram).

        1. Build one media container per media item (carousel = N child
           containers plus one parent).
        2. Poll `GET /{container_id}?fields=status_code,status` until
           FINISHED or until the schedule is exhausted.
        3. `POST /{ig_user_id}/media_publish` with the creation id.

        Media URLs come from the draft's `media` list, which the tool
        layer has already restricted to `s3://` references (presigned
        here) or https URLs (passed through). The Publisher drains S3
        bytes on success; on failure the bucket's 24-hour lifecycle rule
        is the backstop.
        """
        registry.require_scope(self.alias, "instagram_content_publish")
        record = registry.account_record(self.alias)
        ig_user_id = record["account_id"]
        if not ig_user_id:
            raise AccountError(
                f"account '{self.alias}' is not connected; "
                f"run `python scripts/admin.py connect --alias {self.alias}`"
            )
        kind = (getattr(draft, "kind", "") or "").lower()
        caption = getattr(draft, "text", "") or ""
        is_ai = bool(getattr(draft, "is_ai_generated", False))
        urls = _media_urls_for_publish(draft)

        if kind == "image":
            if not urls:
                raise AccountError("image draft has no media")
            container_id = self._create_container(
                ig_user_id,
                {"image_url": urls[0], "caption": caption},
                is_ai,
            )
        elif kind == "video":
            if not urls:
                raise AccountError("video draft has no media")
            container_id = self._create_container(
                ig_user_id,
                {"video_url": urls[0], "media_type": "VIDEO", "caption": caption},
                is_ai,
            )
        elif kind == "reel":
            if not urls:
                raise AccountError("reel draft has no media")
            container_id = self._create_container(
                ig_user_id,
                {"video_url": urls[0], "media_type": "REELS", "caption": caption},
                is_ai,
            )
        elif kind == "multi_image":
            if len(urls) < 2:
                raise AccountError("multi_image needs two or more media items")
            if len(urls) > 10:
                raise AccountError(
                    f"instagram carousel caps at 10 items; got {len(urls)}"
                )
            child_ids: list[str] = []
            for url in urls:
                child = self._create_container(
                    ig_user_id,
                    {"image_url": url, "is_carousel_item": "true"},
                    False,
                )
                self._poll_container(child)
                child_ids.append(child)
            container_id = self._create_container(
                ig_user_id,
                {
                    "media_type": "CAROUSEL",
                    "children": ",".join(child_ids),
                    "caption": caption,
                },
                is_ai,
            )
        elif kind == "text":
            raise CapabilityError(
                "instagram does not support text-only posts; "
                "add media or use a different account"
            )
        elif kind == "link":
            raise CapabilityError(
                "instagram does not render captions as link posts; "
                "use a different account for link posts"
            )
        else:
            raise CapabilityError(
                f"kind '{kind}' is not supported on instagram; "
                "use image, multi_image, video, or reel"
            )

        self._poll_container(container_id)
        pub = self._client.post(
            f"/{ig_user_id}/media_publish",
            data={"creation_id": container_id},
        )
        new_id = str(pub.get("id") or "")
        permalink = ""
        try:
            node = self._client.get(f"/{new_id}", params={"fields": "permalink"})
            permalink = node.get("permalink", "") or ""
        except AccountError as e:
            log.info("instagram permalink lookup failed for %s: %s", new_id, e)
        return PublishResult(
            account=self.alias,
            post_id=new_id,
            permalink=permalink,
            kind=kind,
        )

    def edit(self, post_id: str, patch: dict) -> None:
        raise NotImplementedError("Instagram does not support post edits")

    def edit_post(self, post_id: str, text: str) -> None:
        raise NotImplementedError("Instagram does not support post edits")

    def delete(self, post_id: str) -> None:
        self.delete_post(post_id)

    def delete_post(self, post_id: str) -> None:
        """Delete an IG media node.

        Meta added `instagram_manage_contents` as a required scope for
        media delete on 2025-12-03. The admin CLI's `delete-contents`
        tier adds it to the Facebook Login consent dialog.
        """
        registry.require_scope(self.alias, "instagram_manage_contents")
        self._client.delete(f"/{post_id}")

    def reply_to_comment(self, comment_id: str, text: str) -> Comment:
        """Post a reply under an IG comment and return the created one."""
        registry.require_scope(self.alias, "instagram_manage_comments")
        resp = self._client.post(f"/{comment_id}/replies", data={"message": text or ""})
        new_id = str(resp.get("id") or "")
        return Comment(
            account=self.alias,
            comment_id=new_id,
            post_id="",
            thread_id=comment_id,
            created_time="",
            author="",
            text=text or "",
            is_hidden=False,
            reply_count=0,
        )

    def hide_comment(self, comment_id: str) -> None:
        """Hide (not delete) an IG comment; reversible from Meta's own UI."""
        registry.require_scope(self.alias, "instagram_manage_comments")
        self._client.post(f"/{comment_id}", data={"hide": "true"})

    # ---------- publishing precheck + validation ----------

    def publishing_limit(self) -> dict:
        """Live content_publishing_limit, cached per account for 60s."""
        registry.require_scope(self.alias, "instagram_content_publish")
        now = time.monotonic()
        cached = _limit_cache.get(self.alias)
        if cached and (now - cached[0]) < _LIMIT_CACHE_TTL:
            return cached[1]
        record = registry.account_record(self.alias)
        ig_user_id = record["account_id"] or "me"
        data = self._client.get(
            f"/{ig_user_id}/content_publishing_limit",
            params={"fields": "quota_usage,config"},
        )
        row = (data.get("data") or [{}])[0]
        usage = int(row.get("quota_usage") or 0)
        config = row.get("config") or {}
        quota_total = int(config.get("quota_total") or _SAFE_DAILY_CAP)
        info = {
            "quota_total": quota_total,
            "quota_usage": usage,
            "quota_remaining": max(0, quota_total - usage),
            "config": config,
        }
        _limit_cache[self.alias] = (now, info)
        return info

    def validate_draft(self, draft) -> list[str]:
        """Non-blocking warnings surfaced on draft_post (DESIGN.md 5)."""
        warnings: list[str] = []
        try:
            limit = self.publishing_limit()
        except (AccountError, CapabilityError) as e:
            log.info("publishing_limit precheck skipped: %s", e)
            return warnings
        remaining = int(limit.get("quota_remaining") or 0)
        if remaining <= _LOW_QUOTA_WARN:
            warnings.append(
                f"instagram publishing quota is low: {remaining} of "
                f"{limit.get('quota_total')} remaining in this 24h window"
            )
        return warnings

    # ---------- publish helpers ----------

    def _create_container(self, ig_user_id: str, params: dict, is_ai: bool) -> str:
        payload = {k: v for k, v in params.items() if v != "" and v is not None}
        if is_ai:
            payload["is_ai_generated"] = "true"
        resp = self._client.post(f"/{ig_user_id}/media", data=payload)
        container_id = str(resp.get("id") or "")
        if not container_id:
            raise AccountError("Meta returned no container id for the media creation")
        return container_id

    def _poll_container(self, container_id: str) -> None:
        """Block until the container reaches FINISHED or raises on ERROR/EXPIRED.

        Exponential schedule (1s, 2s, 4s, 8s, ..., capped at 60s between
        polls, 600s total). The container flow is deliberately synchronous
        inside the Publisher Lambda; a 10-minute cap is well within the
        Publisher's own timeout and keeps the whole publish close enough
        to realtime that a human waiting in the admin loop sees the result.
        """
        deadline = time.monotonic() + _POLL_TOTAL_SECONDS
        delay = _POLL_INITIAL_SECONDS
        while True:
            data = self._client.get(
                f"/{container_id}",
                params={"fields": "status_code,status"},
            )
            status = (data.get("status_code") or "").upper()
            if status == "FINISHED" or status == "PUBLISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                detail = data.get("status") or status
                raise AccountError(
                    f"instagram container '{container_id}' failed: {detail}"
                )
            if time.monotonic() + delay > deadline:
                raise AccountError(
                    f"instagram container '{container_id}' did not finish "
                    f"within {int(_POLL_TOTAL_SECONDS)}s"
                )
            time.sleep(delay)
            delay = min(delay * 2, _POLL_MAX_SECONDS)

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


def _media_urls_for_publish(draft) -> list[str]:
    """Return a list of https URLs for Meta to fetch at publish time.

    `s3://` references are presigned to 15-minute GET URLs; `https://`
    references pass through unchanged. The staging prefix check (that
    the key sits under `staging/<account>/`) is enforced at draft_post
    time; this helper trusts the ref and only converts the source.
    """
    out: list[str] = []
    for m in getattr(draft, "media", []) or []:
        if hasattr(m, "source") and hasattr(m, "key"):
            source = getattr(m, "source", "url") or "url"
            key = getattr(m, "key", "") or ""
        elif isinstance(m, dict):
            source = m.get("source", "url") or "url"
            key = m.get("key", "") or m.get("url", "") or ""
        else:
            source = "url"
            key = str(m)
        if source == "s3":
            out.append(mediaStaging.presign_get(key))
        elif source == "url" and key.startswith("https://"):
            out.append(key)
        else:
            raise CapabilityError(
                f"instagram media must be an https URL or an s3:// staged ref; got {key!r}"
            )
    return out


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
