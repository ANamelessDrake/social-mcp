"""The unified data model (DESIGN.md section 6).

Every provider maps its native shapes into these dataclasses, so tools (and
the models driving them) see one schema regardless of provider. Tools return
dataclasses.asdict() of these.
"""

from dataclasses import dataclass, field


@dataclass
class Account:
    """Static account roster entry; dynamic grant state is in DynamoDB.

    The DynamoDB ACCOUNT item carries two KMS-encrypted token fields whose
    meaning depends on the provider:

    - `facebook` / `instagram`: `page_token_ct` is the long-lived Page
      token (used for both FB and the linked IG account); `user_token_ct`
      is the long-lived user token on the primary account for the eventual
      user-token refresh path.
    - `bluesky`: `page_token_ct` is the short-lived access JWT and
      `user_token_ct` is the refresh JWT. The names are kept across
      providers so `crypto.encrypt` / `dynamo.update_tokens` stay provider-
      agnostic; the comment at the dataclass is the one source of truth
      for the overload.
    """

    alias: str
    provider: str  # "facebook" | "instagram" | "bluesky"
    publish_enabled: bool = False
    publish_mode: str = "manual"  # "manual" | "auto" | "auto_for"
    auto_for_kinds: list[str] = field(default_factory=list)
    policy: dict = field(default_factory=dict)


@dataclass
class PostSummary:
    """One post as shown in list_posts; keep context-cheap."""

    account: str
    post_id: str  # provider-native, never parsed
    kind: str  # text | link | photo | multi_photo | video | reel
    permalink: str
    created_time: str  # ISO 8601 UTC
    text: str  # caption or message, truncated by the tool
    media_count: int = 0
    engagement: dict = field(default_factory=dict)  # likes, comments, shares when exposed


@dataclass
class Post(PostSummary):
    """Full post, returned by get_post."""

    full_text: str = ""
    media: list[dict] = field(default_factory=list)  # [{type, url, thumbnail}]
    insights: dict = field(default_factory=dict)  # see Insights


@dataclass
class Comment:
    account: str
    comment_id: str
    post_id: str
    thread_id: str  # the top-level comment id for a reply; comment_id otherwise
    created_time: str
    author: str
    text: str
    is_hidden: bool = False
    reply_count: int = 0


@dataclass
class Insights:
    """Unified insights shape; provider mapping documented per field.

    The source Graph metric name used for each field is recorded in
    `source_metrics` so a reader can audit the mapping (DESIGN.md section 8).
    """

    account: str
    post_id: str
    impressions: int = 0
    reach: int = 0
    # IG video / reel plays. Meta deprecated the media-level `impressions`
    # metric for Instagram on 2025-01-21; reach + views carry that signal now.
    views: int = 0
    engagements: int = 0
    reactions: int = 0
    comments: int = 0
    shares: int = 0
    source_metrics: dict = field(default_factory=dict)


# ---------- M2+ shapes, declared early so sharedModules does not grow a
# second models file when drafts land. Nothing in M1 instantiates these. ----------


@dataclass
class MediaRef:
    source: str  # "s3" | "url"
    key: str
    mime: str
    etag: str = ""


@dataclass
class Draft:
    draft_id: str  # ULID
    account: str
    kind: str
    text: str = ""
    link: str = ""
    media: list[MediaRef] = field(default_factory=list)
    schedule: str = ""  # ISO 8601
    is_ai_generated: bool = False
    created_at: str = ""
    created_by: str = ""  # authenticated agent name
    status: str = "drafted"
    policy_warnings: list[str] = field(default_factory=list)


@dataclass
class Approval:
    draft_id: str
    content_hash: str
    approved_by: str
    approved_at: str
    # one of: publish | schedule | edit:<post_id> | reply:<comment_id> | hide:<account>
    scope: str


@dataclass
class AuditRow:
    """One entry in the append-only audit log (DESIGN.md section 10).

    `actor_identity` is the agent name from the bearer token on tool paths,
    or the operator's OS user on admin CLI paths. `result` is 'ok' or
    'error:<meta_code>'. Tokens and raw bodies never land in audit rows.
    """

    account: str
    kind: str  # draft | approve | reject | publish | schedule | edit | delete | reply | hide
    actor_identity: str
    result: str = "ok"
    draft_id: str = ""
    post_id: str = ""
    comment_id: str = ""
    created_at: str = ""
    request_ms: int = 0


@dataclass
class PublishResult:
    """Return shape from Provider.publish and the Publisher Lambda."""

    account: str
    post_id: str
    permalink: str = ""
    kind: str = ""
