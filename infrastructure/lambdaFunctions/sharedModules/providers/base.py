"""The unified provider interface (DESIGN.md section 5).

Each provider module implements the subset of this protocol its platform
supports, declared via `capabilities`. The tool layer refuses cleanly when a
provider lacks a capability rather than letting an unimplemented method raise.

Adding a provider: see `providers/stub.py` for an annotated starting point.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from sharedModules.models import Comment, Insights, Post, PostSummary


class Capability(StrEnum):
    TEXT = "text"
    LINK = "link"
    IMAGE = "image"
    MULTI_IMAGE = "multi_image"
    VIDEO = "video"
    REEL = "reel"
    SCHEDULE = "schedule"
    EDIT = "edit"
    DELETE = "delete"
    COMMENTS = "comments"
    INSIGHTS = "insights"


@dataclass
class ProviderLimits:
    max_text_length: int = 0
    max_media_per_post: int = 0
    max_hashtags: int = -1  # -1 = no cap enforced by provider
    extras: dict = field(default_factory=dict)


class Provider(Protocol):
    name: str
    capabilities: frozenset[Capability]
    limits: ProviderLimits

    # ------------- read ops (M1) -------------

    def list_posts(self, since_days: int, limit: int) -> list[PostSummary]:
        """Recent posts, newest first. since_days = 0 means no time filter."""
        ...

    def get_post(self, post_id: str) -> Post:
        """One post in full, including unified insights when available."""
        ...

    def list_comments(self, post_id: str, limit: int) -> list[Comment]:
        """Flat list; nested replies carry `thread_id = <top-level id>`."""
        ...

    def get_insights(self, post_id: str) -> Insights:
        """Post-level metrics mapped to the unified set (DESIGN.md section 8)."""
        ...

    # ------------- write ops (M2+) -------------

    def publish(self, draft, account) -> dict:
        """Called by the Publisher Lambda (M2); not called from the MCP server."""
        ...

    def edit(self, post_id: str, patch: dict) -> None: ...

    def delete(self, post_id: str) -> None: ...

    def reply_to_comment(self, comment_id: str, text: str) -> Comment: ...

    def hide_comment(self, comment_id: str) -> None: ...
