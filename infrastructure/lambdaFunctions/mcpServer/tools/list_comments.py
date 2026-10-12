from dataclasses import asdict

from ._common import provider_action


def list_comments(post_id: str, account: str = "", limit: int = 25) -> dict:
    """List comments on one post, oldest first.

    Pass the `post_id` from `list_posts` or `get_post`. `account` can be
    omitted when only one account is configured. `limit` caps the page size
    (default 25).

    Each comment carries its own `comment_id`, the `thread_id` (the
    top-level comment id if this is a reply, otherwise the comment's own
    id, so replies group next to their parent), the author's display name,
    the text, and whether the comment is hidden. Comment replies and hide
    actions land in milestone 3.
    """
    bounded = max(1, min(int(limit or 25), 100))
    return provider_action(
        account,
        lambda p, _: {
            "comments": [asdict(c) for c in p.list_comments(post_id, bounded)],
        },
    )
