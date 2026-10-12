"""The unified data model (DESIGN.md section 6).

Every provider maps its native shapes into these dataclasses, so tools (and
the models driving them) see one schema regardless of provider. Tools return
dataclasses.asdict() of these.
"""

from dataclasses import dataclass, field


@dataclass
class Account:
    """Static account roster entry; dynamic grant state is in DynamoDB."""

    alias: str
    provider: str  # "facebook" | "instagram"
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
