from ._common import provider_action, serialize


def get_post(post_id: str, account: str = "") -> dict:
    """Fetch one post in full, with its text, media, and insights.

    Pass the `post_id` from `list_posts` (an opaque provider-native string;
    do not parse or modify it). `account` can be omitted when only one
    account is configured; otherwise pass the alias that owns the post.

    Returns the full body, every media attachment (type, URL, thumbnail),
    and the current insights snapshot. The insights are the unified
    reach/engagement shape; the raw Graph metric names are preserved under
    `insights.source_metrics` for traceability.
    """
    return provider_action(account, lambda p, _: {"post": serialize(p.get_post(post_id))})
