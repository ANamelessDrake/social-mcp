"""Return new comments since the caller's last check (DESIGN.md M7).

Pull-based variant of wait_for_comments: reads the stored comment event
inbox past the per-identity cursor, advances the cursor, filters, and
returns. Nothing is lost while agents are offline because webhookIngest
writes to DynamoDB before any publish.
"""

from sharedModules import dynamo, registry, scope
from sharedModules.errors import AccountError


def _identity_from_scope() -> str:
    """Each client token resolves to a scoped identity so filtered cursors
    stay per-caller. Falls back to the single-tenant default when no
    scope header is in play."""
    pinned = scope.allowed_accounts()
    return ",".join(pinned) if pinned else "default"


def check_comments(
    account: str = "",
    post_id: str = "",
    limit: int = 20,
) -> dict:
    """Return comments on your posts that arrived since your last check.

    Leave `account` empty to sweep every configured account the server
    has ever ingested a comment for. Pass `post_id` to narrow to one
    post's comments. Each (account, post_id) filter keeps its own read
    position so a filtered check never consumes an unfiltered one.
    Returns up to `limit` comments (max 100), oldest first.
    """
    requested = [a.strip() for a in account.split(",") if a.strip()]
    try:
        if requested:
            for alias in requested:
                registry.account_record(alias)  # validates alias exists
            accounts = requested
        else:
            accounts = dynamo.list_comment_accounts()
    except AccountError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"check_comments failed: {e}"}

    identity = _identity_from_scope()
    out = []
    for alias in accounts:
        scope = dynamo.comment_cursor_scope(alias, post_id)
        cursor = dynamo.get_comment_cursor(identity, scope)
        rows = dynamo.comments_after(alias, cursor, limit, post_id=post_id)
        if not rows:
            continue
        dynamo.set_comment_cursor(identity, scope, rows[-1]["_sk"])
        for row in rows:
            row.pop("_sk", None)
            out.append(row)
    return {"ok": True, "comments": out}
