"""Starting point for a new provider (Bluesky, X, Threads, LinkedIn, ...).

Copy this file to `<name>.py`, implement what the platform supports, and
narrow `capabilities` to the subset the implementation covers. The tool
layer uses `capabilities` to refuse unsupported verbs cleanly, so declaring
honestly is a feature and not a limitation.

Register the provider in `providers/__init__.py` and add the string value
of `name` as a possible value in `config/example.json` and in
`config/README.md`.
"""

from .base import Capability, ProviderLimits


class StubProvider:
    name = "stub"
    capabilities = frozenset(
        {
            # Narrow this to what the platform actually supports.
            Capability.TEXT,
        }
    )
    limits = ProviderLimits(max_text_length=0, max_media_per_post=0)

    def __init__(self, alias: str) -> None:
        self.alias = alias
        # Store whatever the provider's API client needs; prefer composition
        # with GraphClient-equivalents from `metaClient.py` style modules.

    # ----- read -----

    def list_posts(self, since_days: int, limit: int):
        raise NotImplementedError

    def get_post(self, post_id: str):
        raise NotImplementedError

    def list_comments(self, post_id: str, limit: int):
        raise NotImplementedError

    def get_insights(self, post_id: str):
        raise NotImplementedError

    # ----- write -----

    def publish(self, draft, account):
        raise NotImplementedError

    def edit(self, post_id: str, patch: dict):
        raise NotImplementedError

    def delete(self, post_id: str):
        raise NotImplementedError

    def reply_to_comment(self, comment_id: str, text: str):
        raise NotImplementedError

    def hide_comment(self, comment_id: str):
        raise NotImplementedError
