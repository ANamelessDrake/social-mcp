from ._common import provider_action, serialize


def get_insights(post_id: str, account: str = "") -> dict:
    """Fetch unified insights for one post.

    Pass the `post_id` from `list_posts` or `get_post`. `account` can be
    omitted when only one account is configured.

    Returns a provider-agnostic shape (`impressions`, `reach`,
    `engagements`, `reactions`, `comments`, `shares`) plus
    `source_metrics`, which records the raw Graph metric name each unified
    field was mapped from. Metrics Meta deprecated at v26 (the `_unique`
    family and the bare `post_impressions*`) are not requested.
    """
    return provider_action(
        account,
        lambda p, _: {"insights": serialize(p.get_insights(post_id))},
    )
