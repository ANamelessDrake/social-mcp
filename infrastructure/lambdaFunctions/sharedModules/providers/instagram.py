"""Instagram provider (stub for M1).

Capabilities are declared so tools can refuse cleanly today; the read half
lands in M4 and publishing in M5 (DESIGN.md section 15). Every method
raises NotImplementedError with a message naming the milestone, so a
tool-level accident fails loudly rather than silently.
"""

from .base import Capability, ProviderLimits


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

    def __init__(self, alias: str) -> None:
        self.alias = alias

    def list_posts(self, since_days: int, limit: int):
        raise NotImplementedError("Instagram reads land in milestone 4")

    def get_post(self, post_id: str):
        raise NotImplementedError("Instagram reads land in milestone 4")

    def list_comments(self, post_id: str, limit: int):
        raise NotImplementedError("Instagram reads land in milestone 4")

    def get_insights(self, post_id: str):
        raise NotImplementedError("Instagram reads land in milestone 4")

    def publish(self, draft, account):
        raise NotImplementedError("Instagram publishing lands in milestone 5")

    def edit(self, post_id: str, patch: dict):
        raise NotImplementedError("Instagram does not support post edits")

    def delete(self, post_id: str):
        raise NotImplementedError("Instagram delete lands in milestone 5")

    def reply_to_comment(self, comment_id: str, text: str):
        raise NotImplementedError("Instagram comment replies land in milestone 5")

    def hide_comment(self, comment_id: str):
        raise NotImplementedError("Instagram comment hides land in milestone 5")
