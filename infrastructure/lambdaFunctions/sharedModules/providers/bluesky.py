"""Bluesky provider over the AT Protocol (XRPC).

M8 (DESIGN.md section 5, "Adding a provider later"). Pressure test for the
provider interface: a non-Meta backend with a different auth model (JWT
Bearer + refresh), a different id shape (AT URIs with a collection + rkey),
different text handling (byte-offset facets rather than caption fields),
and a different media model (blob refs uploaded first, then referenced in
the record).

Capabilities declared honestly: no VIDEO (Bluesky video is new and quirky;
land it in a later pass), no REEL (not a Bluesky concept), no EDIT (feed
post records are immutable; the user deletes and reposts). The protocol
still routes through the unified `post_id` which, for Bluesky, is the full
`at://did/collection/rkey` URI exactly as returned by `createRecord`.
"""

import logging
import re
from datetime import UTC, datetime

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
from .blueskyClient import BlueskyClient

log = logging.getLogger(__name__)

# AT URI parts: at://<did>/<collection>/<rkey>
_AT_URI_RE = re.compile(r"^at://([^/]+)/([^/]+)/([^/]+)$")

# atproto expects byte offsets, not character offsets, for facet ranges.
# `_find_url_facets` walks the UTF-8 bytes to produce them, with Unicode
# text handled correctly (a surrogate-pair emoji before a URL pushes the
# byte start past the character start).
_URL_RE = re.compile(r"https?://[^\s)]+", re.IGNORECASE)

POST_COLLECTION = "app.bsky.feed.post"
MAX_CAROUSEL_IMAGES = 4


class BlueskyProvider:
    name = "bluesky"
    capabilities = frozenset(
        {
            Capability.TEXT,
            Capability.LINK,
            Capability.IMAGE,
            Capability.MULTI_IMAGE,
            Capability.SCHEDULE,  # Server-side via EventBridge (same as Meta)
            Capability.DELETE,
            Capability.COMMENTS,
            Capability.INSIGHTS,
            # Profile is a mutable `app.bsky.actor.profile` record on the
            # user's own repo; both read and write are a single XRPC call.
            Capability.PROFILE_READ,
            Capability.PROFILE_EDIT,
            # Not declared:
            # - VIDEO: new and quirky, deferred
            # - REEL: not a concept on Bluesky
            # - EDIT: post records are immutable (delete + repost pattern)
        }
    )
    # Bluesky's hard cap is 300 graphemes per post. The policy engine counts
    # characters; the server-side cap is enforced here as 300 code points,
    # which is strict enough that any graphemed-under-300 input passes.
    limits = ProviderLimits(max_text_length=300, max_media_per_post=MAX_CAROUSEL_IMAGES)

    def __init__(self, alias: str, *, _client: BlueskyClient | None = None) -> None:
        self.alias = alias
        self._client = _client or BlueskyClient(
            tokens_fn=self._tokens,
            persist_fn=self._persist_tokens,
        )

    # ---------- read ops ----------

    def list_posts(self, since_days: int, limit: int) -> list[PostSummary]:
        record = registry.account_record(self.alias)
        did = record["account_id"] or ""
        params = {"actor": did, "limit": max(1, min(limit, 50))}
        data = self._client.get("app.bsky.feed.getAuthorFeed", params=params)
        feed = data.get("feed", []) or []
        posts: list[PostSummary] = []
        cutoff = _cutoff_iso(since_days) if since_days > 0 else None
        for entry in feed:
            post = entry.get("post") or {}
            summary = self._to_summary(post)
            if cutoff and summary.created_time and summary.created_time < cutoff:
                continue
            posts.append(summary)
        return posts

    def get_post(self, post_id: str) -> Post:
        thread = self._client.get("app.bsky.feed.getPostThread", params={"uri": post_id})
        node = (thread.get("thread") or {}).get("post") or {}
        if not node:
            raise AccountError(f"Bluesky post not found: {post_id}")
        summary = self._to_summary(node)
        summary.engagement = _engagement(node)
        insights = _node_insights(self.alias, post_id, node)
        return Post(
            account=summary.account,
            post_id=summary.post_id,
            kind=summary.kind,
            permalink=summary.permalink,
            created_time=summary.created_time,
            text=summary.text,
            media_count=summary.media_count,
            engagement=summary.engagement,
            full_text=_record_text(node),
            media=_extract_media(node),
            insights=_insights_dict(insights),
        )

    def fetch_comment(self, comment_id: str) -> dict:
        """Depth-0 thread lookup for a single comment uri; webhook parity."""
        data = self._client.get(
            "app.bsky.feed.getPostThread",
            params={"uri": comment_id, "depth": 0},
        )
        node = (data.get("thread") or {}).get("post") or {}
        if not node:
            raise AccountError(f"Bluesky comment not found: {comment_id}")
        parent_uri = _parent_uri(node) or comment_id
        return {
            "comment_id": node.get("uri", ""),
            "created_time": _record_created(node),
            "author": (node.get("author") or {}).get("handle", "") or "",
            "text": _record_text(node),
            "is_hidden": False,
            "thread_id": parent_uri,
            "reply_count": int(node.get("replyCount", 0) or 0),
        }

    def list_comments(self, post_id: str, limit: int) -> list[Comment]:
        """Flatten the reply tree under a post into one list.

        Bluesky's `replies` edge is recursive. We walk it depth-first with
        `thread_id` always pointing at the IMMEDIATE PARENT, which matches
        how Facebook and Instagram set thread_id for nested replies.
        """
        data = self._client.get(
            "app.bsky.feed.getPostThread",
            params={"uri": post_id, "depth": 6},  # a sane cap; most Bluesky threads are shallow
        )
        root = data.get("thread") or {}
        out: list[Comment] = []
        cap = max(1, min(limit, 200))

        def _walk(node: dict, parent_uri: str) -> None:
            if len(out) >= cap:
                return
            replies = node.get("replies") or []
            for reply in replies:
                r_node = reply.get("post") if "post" in reply else reply
                if not r_node:
                    continue
                out.append(self._to_comment(post_id, r_node, parent_uri=parent_uri))
                _walk(reply, r_node.get("uri", ""))

        _walk(root, post_id)
        return out

    def get_insights(self, post_id: str) -> Insights:
        thread = self._client.get("app.bsky.feed.getPostThread", params={"uri": post_id})
        node = (thread.get("thread") or {}).get("post") or {}
        if not node:
            raise AccountError(f"Bluesky post not found: {post_id}")
        return _node_insights(self.alias, post_id, node)

    # ---------- write ops ----------

    def publish(self, draft) -> PublishResult:
        """Publish a post via com.atproto.repo.createRecord.

        Facets (link ranges with byte offsets) are detected from the text
        itself. Images are uploaded as blobs first and attached via
        `embed: app.bsky.embed.images`. Scheduling is server-side as with
        Meta: a draft carrying a `schedule` is still published immediately
        when this method is called, because EventBridge Scheduler invokes
        the Publisher at the scheduled moment and re-checks the approval.
        """
        kind = (getattr(draft, "kind", "") or "").lower()
        text = getattr(draft, "text", "") or ""
        record = registry.account_record(self.alias)
        did = record["account_id"]
        handle = record.get("display_name") or did
        if not did:
            raise AccountError(
                f"account '{self.alias}' is not connected; "
                f"run `python scripts/admin.py connect-bluesky --alias {self.alias}`"
            )
        if len(text) > self.limits.max_text_length:
            raise AccountError(
                f"Bluesky post is {len(text)} chars; the cap is {self.limits.max_text_length}"
            )

        post_record = {
            "$type": POST_COLLECTION,
            "text": text,
            "createdAt": _now_iso(),
        }

        facets = _find_url_facets(text)
        if facets:
            post_record["facets"] = facets

        if kind == "text":
            pass  # text already assigned
        elif kind == "link":
            # Bluesky auto-renders a link embed when a URL appears in text; the
            # link is also a facet. For M8 we deliberately skip
            # app.bsky.embed.external (would need an OpenGraph fetch) and let
            # the client-side auto-render carry it.
            if getattr(draft, "link", ""):
                link = getattr(draft, "link", "")
                if link not in text:
                    # Append the link so it is present in both text and facets
                    post_record["text"] = f"{text} {link}".strip()
                    post_record["facets"] = _find_url_facets(post_record["text"])
        elif kind in ("image", "multi_image"):
            urls = _media_urls(draft)
            if not urls:
                raise AccountError(f"{kind} draft has no media")
            if len(urls) > MAX_CAROUSEL_IMAGES:
                raise AccountError(
                    f"bluesky caps image posts at {MAX_CAROUSEL_IMAGES}; got {len(urls)}"
                )
            images = []
            for url in urls:
                blob = self._upload_image(url)
                images.append({"alt": "", "image": blob})
            post_record["embed"] = {
                "$type": "app.bsky.embed.images",
                "images": images,
            }
        else:
            raise CapabilityError(
                f"kind '{kind}' is not supported on bluesky; use text, link, image, or multi_image"
            )

        resp = self._client.post(
            "com.atproto.repo.createRecord",
            body={
                "repo": did,
                "collection": POST_COLLECTION,
                "record": post_record,
            },
        )
        uri = resp.get("uri", "") or ""
        rkey = _rkey_from_uri(uri)
        permalink = f"https://bsky.app/profile/{handle}/post/{rkey}" if rkey else ""
        return PublishResult(
            account=self.alias,
            post_id=uri,
            permalink=permalink,
            kind=kind,
        )

    def edit(self, post_id: str, patch: dict) -> None:
        raise NotImplementedError("bluesky post records are immutable; delete and repost")

    def edit_post(self, post_id: str, text: str) -> None:
        raise NotImplementedError("bluesky post records are immutable; delete and repost")

    def delete(self, post_id: str) -> None:
        self.delete_post(post_id)

    def delete_post(self, post_id: str) -> None:
        """com.atproto.repo.deleteRecord against the collection + rkey."""
        parts = _AT_URI_RE.match(post_id)
        if not parts:
            raise AccountError(
                f"not a Bluesky AT URI: {post_id!r}; expected at://<did>/<collection>/<rkey>"
            )
        did, collection, rkey = parts.groups()
        self._client.post(
            "com.atproto.repo.deleteRecord",
            body={"repo": did, "collection": collection, "rkey": rkey},
        )

    def reply_to_comment(self, comment_id: str, text: str) -> Comment:
        """Reply by creating a feed post with the parent + root refs set."""
        record = registry.account_record(self.alias)
        did = record["account_id"]
        if not did:
            raise AccountError(
                f"account '{self.alias}' is not connected; "
                f"run `python scripts/admin.py connect-bluesky --alias {self.alias}`"
            )
        # Resolve the thread root for the reply refs; Bluesky needs both.
        thread = self._client.get(
            "app.bsky.feed.getPostThread",
            params={"uri": comment_id},
        )
        parent = (thread.get("thread") or {}).get("post") or {}
        if not parent:
            raise AccountError(f"Bluesky comment not found: {comment_id}")
        parent_ref = {"uri": parent.get("uri", ""), "cid": parent.get("cid", "")}
        root_ref = _root_ref(parent, parent_ref)

        post_record = {
            "$type": POST_COLLECTION,
            "text": text or "",
            "createdAt": _now_iso(),
            "reply": {"root": root_ref, "parent": parent_ref},
        }
        facets = _find_url_facets(post_record["text"])
        if facets:
            post_record["facets"] = facets

        resp = self._client.post(
            "com.atproto.repo.createRecord",
            body={
                "repo": did,
                "collection": POST_COLLECTION,
                "record": post_record,
            },
        )
        new_uri = resp.get("uri", "") or ""
        return Comment(
            account=self.alias,
            comment_id=new_uri,
            post_id=root_ref.get("uri", ""),
            thread_id=comment_id,
            created_time=_now_iso(),
            author="",
            text=text or "",
            is_hidden=False,
            reply_count=0,
        )

    def hide_comment(self, comment_id: str) -> None:
        raise NotImplementedError(
            "bluesky has no server-API comment hide; mute at the client is the user action"
        )

    # ---------- profile ops (M9) ----------

    PROFILE_COLLECTION = "app.bsky.actor.profile"
    PROFILE_RKEY = "self"

    def get_profile(self) -> ProfileInfo:
        """Return the actor's profile.

        Two XRPC calls:

        - `app.bsky.actor.getProfile` for the resolved handle, avatar,
          banner, and the counters Bluesky exposes publicly.
        - `com.atproto.repo.getRecord` on the `app.bsky.actor.profile`
          collection for the raw record (used by `update_profile` to
          preserve avatar/banner blob refs).

        If the profile record does not exist yet, `getRecord` fails; the
        actor data alone is still returned.
        """
        record = registry.account_record(self.alias)
        did = record["account_id"] or ""
        actor = self._client.get("app.bsky.actor.getProfile", params={"actor": did})
        try:
            rec = self._client.get(
                "com.atproto.repo.getRecord",
                params={
                    "repo": did,
                    "collection": self.PROFILE_COLLECTION,
                    "rkey": self.PROFILE_RKEY,
                },
            )
            rec_value = (rec or {}).get("value") or {}
        except AccountError:
            rec_value = {}
        return ProfileInfo(
            account=self.alias,
            handle=actor.get("handle", "") or "",
            display_name=rec_value.get("displayName", "") or actor.get("displayName", "") or "",
            about=rec_value.get("description", "") or actor.get("description", "") or "",
            description=rec_value.get("description", "") or actor.get("description", "") or "",
            website="",
            phone="",
            emails=[],
            picture_url=actor.get("avatar", "") or "",
            cover_url=actor.get("banner", "") or "",
            category="",
            extras={
                "followers_count": int(actor.get("followersCount", 0) or 0),
                "follows_count": int(actor.get("followsCount", 0) or 0),
                "posts_count": int(actor.get("postsCount", 0) or 0),
            },
        )

    def update_profile(self, patch: ProfilePatch) -> None:
        """Overwrite the profile record, preserving avatar and banner.

        atproto has no partial-update op for records: `putRecord` writes
        the whole record. The existing record is fetched first so avatar
        and banner blob refs are kept; only `displayName` and `description`
        are merged in from `patch`. If the record does not exist yet, a
        fresh one is created with just the patched fields.

        A `swapRecord` CID is passed on when the existing record carries
        one so concurrent client-side profile edits do not get clobbered;
        a mismatch surfaces as `InvalidSwap` from the PDS.
        """
        record = registry.account_record(self.alias)
        did = record["account_id"] or ""
        if not did:
            raise AccountError(
                f"account '{self.alias}' is not connected; "
                f"run `python scripts/admin.py connect-bluesky --alias {self.alias}`"
            )

        existing_value: dict = {}
        existing_cid: str = ""
        try:
            rec = self._client.get(
                "com.atproto.repo.getRecord",
                params={
                    "repo": did,
                    "collection": self.PROFILE_COLLECTION,
                    "rkey": self.PROFILE_RKEY,
                },
            )
            existing_value = (rec or {}).get("value") or {}
            existing_cid = (rec or {}).get("cid", "") or ""
        except AccountError:
            existing_value = {}
            existing_cid = ""

        new_value: dict = dict(existing_value)
        new_value["$type"] = self.PROFILE_COLLECTION
        if patch.display_name:
            new_value["displayName"] = patch.display_name
        if patch.description:
            new_value["description"] = patch.description
        elif patch.about and "description" not in new_value:
            new_value["description"] = patch.about

        body = {
            "repo": did,
            "collection": self.PROFILE_COLLECTION,
            "rkey": self.PROFILE_RKEY,
            "record": new_value,
        }
        if existing_cid:
            body["swapRecord"] = existing_cid
        self._client.post("com.atproto.repo.putRecord", body=body)

    # ---------- publish helpers ----------

    def _upload_image(self, url: str) -> dict:
        """Fetch the staged URL and upload the bytes as a Bluesky blob."""
        import httpx

        with httpx.Client(timeout=30.0) as http:
            resp = http.get(url)
        if resp.status_code >= 400:
            raise AccountError(f"could not fetch staged image at {url}: HTTP {resp.status_code}")
        mime = resp.headers.get("content-type", "image/jpeg").split(";", 1)[0].strip()
        resp_body = self._client.upload_blob(resp.content, mime)
        blob = resp_body.get("blob") or {}
        if not blob:
            raise AccountError("Bluesky uploadBlob returned no blob")
        return blob

    # ---------- token plumbing ----------

    def _tokens(self) -> tuple[str, str]:
        record = registry.account_record(self.alias)
        if record.get("status") != "active" or not record.get("page_token_ct"):
            raise AccountError(
                f"account '{self.alias}' is not connected; "
                f"run `python scripts/admin.py connect-bluesky --alias {self.alias}`"
            )
        access = crypto.decrypt(record["page_token_ct"])
        refresh_ct = record.get("user_token_ct")
        refresh = crypto.decrypt(refresh_ct) if refresh_ct else ""
        return access, refresh

    def _persist_tokens(self, access_jwt: str, refresh_jwt: str) -> None:
        access_ct = crypto.encrypt(access_jwt)
        refresh_ct = crypto.encrypt(refresh_jwt)
        dynamo.update_tokens(self.alias, access_ct, refresh_ct)

    # ---------- mapping ----------

    def _to_summary(self, node: dict) -> PostSummary:
        record = node.get("record") or {}
        embed = record.get("embed") or {}
        embed_type = embed.get("$type", "")
        kind = _kind_from_embed(embed_type, embed)
        media_count = _media_count_from_embed(embed_type, embed)
        return PostSummary(
            account=self.alias,
            post_id=node.get("uri", ""),
            kind=kind,
            permalink=_permalink(node),
            created_time=_record_created(node),
            text=_truncate(_record_text(node), 300),
            media_count=media_count,
            engagement={},
        )

    def _to_comment(self, post_id: str, node: dict, *, parent_uri: str) -> Comment:
        return Comment(
            account=self.alias,
            comment_id=node.get("uri", ""),
            post_id=post_id,
            thread_id=parent_uri or post_id,
            created_time=_record_created(node),
            author=(node.get("author") or {}).get("handle", "") or "",
            text=_record_text(node),
            is_hidden=False,
            reply_count=int(node.get("replyCount", 0) or 0),
        )


# ---------- module-level pure helpers (unit-testable without HTTP) ----------


def _media_urls(draft) -> list[str]:
    """Pull image URLs from a draft's media list.

    Bluesky's `uploadBlob` reads bytes directly, so the provider fetches
    whatever URL the draft carries (presigned s3://, direct https://) and
    streams the response body into the atproto upload. The staging/url
    distinction is handled by Instagram's helper for s3://; Bluesky
    treats both the same way (fetch, then upload) so we just want the
    URL string.
    """
    urls: list[str] = []
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
            # The Publisher presigns s3:// refs before invoking the provider
            # for Instagram today. For Bluesky, we expect the Publisher to
            # presign the same way; a raw s3:// here means the Publisher
            # was not updated for Bluesky and we fail loudly rather than
            # silently sending an unreachable URL to uploadBlob.
            raise CapabilityError(
                "bluesky media needs an https URL at the provider boundary; "
                "the Publisher must presign s3:// refs before calling publish"
            )
        if source == "url" and key.startswith("https://"):
            urls.append(key)
        else:
            raise CapabilityError(f"bluesky media must be an https URL; got {key!r}")
    return urls


def _find_url_facets(text: str) -> list[dict]:
    """Build app.bsky.richtext.facet entries for every URL in `text`.

    atproto's facet `byteStart`/`byteEnd` are UTF-8 BYTE offsets, not
    character offsets. A URL that follows a multi-byte character (an emoji,
    a non-ASCII letter) sits at a byte position several bytes past its
    character position, and getting that wrong makes the link face jump
    text client-side. The implementation walks `text.encode("utf-8")`
    and converts character spans from `re.finditer` to byte spans by
    counting the UTF-8 length of the prefix once per match.
    """
    if not text:
        return []
    out: list[dict] = []
    encoded = text.encode("utf-8")
    for match in _URL_RE.finditer(text):
        char_start, char_end = match.start(), match.end()
        byte_start = len(text[:char_start].encode("utf-8"))
        byte_end = len(text[:char_end].encode("utf-8"))
        # Guard: sanity-check byte offsets against the encoded buffer length.
        if byte_end > len(encoded) or byte_start > byte_end:
            continue
        out.append(
            {
                "index": {"byteStart": byte_start, "byteEnd": byte_end},
                "features": [
                    {
                        "$type": "app.bsky.richtext.facet#link",
                        "uri": match.group(0),
                    }
                ],
            }
        )
    return out


def _kind_from_embed(embed_type: str, embed: dict) -> str:
    if embed_type == "app.bsky.embed.images":
        count = len(embed.get("images", []) or [])
        return "multi_image" if count > 1 else "image"
    if embed_type == "app.bsky.embed.external":
        return "link"
    return "text"


def _media_count_from_embed(embed_type: str, embed: dict) -> int:
    if embed_type == "app.bsky.embed.images":
        return len(embed.get("images", []) or [])
    return 0


def _record_text(node: dict) -> str:
    return ((node.get("record") or {}).get("text")) or ""


def _record_created(node: dict) -> str:
    return ((node.get("record") or {}).get("createdAt")) or ""


def _extract_media(node: dict) -> list[dict]:
    """Return [{type, url, thumbnail}] for a post's embed, if any.

    Bluesky image embeds carry a blob ref rather than an https URL; the
    read surface returns the ref's $link so the caller can resolve it to
    `cdn.bsky.app/img/.../<link>@jpeg` if they need to display it. For
    unified-shape purposes, `url` is the best thing we can hand back
    without a second XRPC call.
    """
    record = node.get("record") or {}
    embed = record.get("embed") or {}
    out: list[dict] = []
    if embed.get("$type") == "app.bsky.embed.images":
        for img in embed.get("images", []) or []:
            blob = (img.get("image") or {}).get("ref") or {}
            link = blob.get("$link", "") or ""
            out.append(
                {
                    "type": (img.get("image") or {}).get("mimeType", "image"),
                    "url": link,
                    "thumbnail": link,
                }
            )
    return out


def _permalink(node: dict) -> str:
    uri = node.get("uri", "") or ""
    rkey = _rkey_from_uri(uri)
    author = node.get("author") or {}
    handle = author.get("handle", "") or ""
    if not (rkey and handle):
        return ""
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


def _rkey_from_uri(uri: str) -> str:
    m = _AT_URI_RE.match(uri or "")
    if not m:
        return ""
    return m.group(3)


def _engagement(node: dict) -> dict:
    return {
        "likes": int(node.get("likeCount", 0) or 0),
        "replies": int(node.get("replyCount", 0) or 0),
        "reposts": int(node.get("repostCount", 0) or 0),
        "quotes": int(node.get("quoteCount", 0) or 0),
    }


def _node_insights(alias: str, post_id: str, node: dict) -> Insights:
    """Pull the four engagement counters from a Bluesky post node."""
    like = int(node.get("likeCount", 0) or 0)
    reply = int(node.get("replyCount", 0) or 0)
    repost = int(node.get("repostCount", 0) or 0)
    quote = int(node.get("quoteCount", 0) or 0)
    return Insights(
        account=alias,
        post_id=post_id,
        # Bluesky does not expose impressions/reach/views via the public
        # XRPC surface; leave them zeroed so the unified shape stays honest.
        impressions=0,
        reach=0,
        views=0,
        engagements=like + reply + repost + quote,
        reactions=like,
        comments=reply,
        shares=repost + quote,
        source_metrics={
            "likeCount": like,
            "replyCount": reply,
            "repostCount": repost,
            "quoteCount": quote,
        },
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


def _parent_uri(node: dict) -> str:
    """Pull the parent URI from a reply record's `reply.parent.uri`, if any."""
    record = node.get("record") or {}
    reply = record.get("reply") or {}
    parent = reply.get("parent") or {}
    return parent.get("uri", "") or ""


def _root_ref(parent: dict, fallback_parent_ref: dict) -> dict:
    """Pick the thread root for a reply.

    If the parent is itself a reply, its own record.reply.root is the thread
    root; if the parent is a top-level post, the parent IS the root.
    """
    record = parent.get("record") or {}
    reply = record.get("reply") or {}
    root = reply.get("root") or {}
    if root.get("uri") and root.get("cid"):
        return {"uri": root["uri"], "cid": root["cid"]}
    return fallback_parent_ref


def _truncate(text: str, cap: int) -> str:
    if len(text) <= cap:
        return text
    return text[: cap - 1].rstrip() + "…"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _cutoff_iso(since_days: int) -> str:
    from datetime import timedelta

    return (
        (datetime.now(UTC) - timedelta(days=since_days))
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
