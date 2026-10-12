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


def _permitted_accounts(requested: list[str]) -> list[str]:
    """Resolve requested aliases against the per-connection account scope
    (DESIGN.md section 4.2). An unscoped caller sees every alias
    `configured_accounts` returns; a pinned caller sees only its pinned
    set, intersected with what it asked for. An out-of-scope alias raises
    AccountError via resolve_alias, so a scoped agent cannot talk itself
    into another account's inbox by naming it in the comma-separated form.
    """
    visible = set(registry.configured_accounts().keys())
    if not requested:
        return sorted(visible)
    aliases = [registry.resolve_alias(a) for a in requested]
    return [a for a in aliases if a in visible]


def check_comments(
    account: str = "",
    post_id: str = "",
    limit: int = 20,
) -> dict:
    """Return comments on your posts that arrived since your last check.

    Leave `account` empty to sweep every configured account the server
    has ever ingested a comment for (narrowed to the scope your
    connection is pinned to, if any). Pass `post_id` to narrow to one
    post's comments. Each (account, post_id) filter keeps its own read
    position so a filtered check never consumes an unfiltered one.
    Returns up to `limit` comments (max 100), oldest first.
    """
    requested = [a.strip() for a in account.split(",") if a.strip()]
    try:
        permitted = _permitted_accounts(requested)
    except AccountError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"check_comments failed: {e}"}

    if requested:
        accounts = permitted
    else:
        # Only sweep accounts that have ever ingested a comment AND are
        # visible to this request. The intersection, not the DB list, is
        # the source of truth; the scope header is still not a security
        # boundary (DESIGN.md 4.2), but a pinned agent must not be able
        # to sweep accounts it is not pinned to.
        with_events = set(dynamo.list_comment_accounts())
        accounts = [a for a in permitted if a in with_events]

    identity = _identity_from_scope()
    out = []
    for alias in accounts:
        cursor_scope = dynamo.comment_cursor_scope(alias, post_id)
        cursor = dynamo.get_comment_cursor(identity, cursor_scope)
        rows = dynamo.comments_after(alias, cursor, limit, post_id=post_id)
        if not rows:
            continue
        dynamo.set_comment_cursor(identity, cursor_scope, rows[-1]["_sk"])
        for row in rows:
            row.pop("_sk", None)
            out.append(row)
    return {"ok": True, "comments": out}
