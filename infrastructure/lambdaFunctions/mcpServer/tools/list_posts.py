from dataclasses import asdict

import httpx
from sharedModules.errors import AccountError, CapabilityError
from sharedModules.providers import get_provider
from sharedModules.registry import resolve_sweep_aliases


def list_posts(account: str = "", since_days: int = 0, limit: int = 10) -> dict:
    """List recent posts for an account, newest first.

    Pass `account` to look at one account; leave it empty to sweep every
    configured account at once (useful for watching several pages in one
    call). `since_days` filters to posts newer than N days; 0 means no time
    filter. `limit` caps the per-account page size (default 10).

    Each returned post is a summary: id, kind (text, link, image,
    multi_image, video, reel), permalink, created_time, a truncated text
    preview, and the media count. Call `get_post` to fetch the full body
    and insights for one post.

    When sweeping multiple accounts, a provider failure on one account
    reports in `errors` while the others succeed; one dead account does not
    blind you to the rest.
    """
    try:
        aliases = resolve_sweep_aliases(account)
    except AccountError as e:
        return {"ok": False, "error": str(e)}

    posts: list[dict] = []
    errors: dict[str, str] = {}
    per_account_limit = max(1, min(int(limit or 10), 50))
    since = max(0, int(since_days or 0))
    for alias in aliases:
        try:
            provider = get_provider(alias)
            summaries = provider.list_posts(since_days=since, limit=per_account_limit)
            posts.extend(asdict(s) for s in summaries)
        except (AccountError, CapabilityError, NotImplementedError) as e:
            errors[alias] = str(e) or e.__class__.__name__
        except httpx.HTTPError as e:
            errors[alias] = f"network error: {e}"

    # Newest-first merge across accounts; ISO 8601 sorts lexically.
    posts.sort(key=lambda p: p.get("created_time", ""), reverse=True)

    envelope = {"ok": True, "posts": posts}
    if errors:
        envelope["errors"] = errors
    return envelope
