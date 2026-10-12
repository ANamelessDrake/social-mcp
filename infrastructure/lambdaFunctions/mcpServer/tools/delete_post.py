"""Stage a delete of an existing Page post (DESIGN.md section 7.1).

Delete needs an explicit operator approval of the specific post, same
as edit. The optional `reason` is persisted on the staged change so the
audit row records *why* the post went away. The tool does not call
Meta.
"""

from __future__ import annotations

import os

from sharedModules import dynamo, registry
from sharedModules.errors import AccountError
from sharedModules.providers import get_provider
from sharedModules.providers.base import Capability


def delete_post(post_id: str, reason: str = "", account: str = "") -> dict:
    """Stage a delete of a Page post for operator approval.

    - `post_id` (required): the post's Meta-native id.
    - `reason`: one short sentence captured in the audit row ("spam
      report escalated", "wrong link"). Optional.
    - `account`: alias from `list_accounts`. Omit when only one is
      configured.

    Returns `{ok, post_id, staged_change, next}`. The next step is
    `python scripts/admin.py approve-delete --post-id <id>`; nothing
    reaches Meta until the operator approves.
    """
    try:
        alias = registry.resolve_alias(account)
    except AccountError as e:
        return {"ok": False, "error": str(e)}

    if not post_id:
        return {"ok": False, "error": "post_id is required"}

    provider = get_provider(alias)
    if Capability.DELETE not in provider.capabilities:
        return {"ok": False, "error": f"provider '{provider.name}' does not support delete"}

    dynamo.write_staged_delete(
        post_id=post_id,
        account=alias,
        reason=reason or "",
        created_by=os.environ.get("AGENT_IDENTITY", "mcp_client"),
    )
    dynamo.delete_approval_record(f"delete:{post_id}")

    dynamo.append_audit(
        {
            "account": alias,
            "kind": "delete",
            "actor_identity": os.environ.get("AGENT_IDENTITY", "mcp_client"),
            "post_id": post_id,
            "result": "staged",
        }
    )

    return {
        "ok": True,
        "post_id": post_id,
        "staged_change": "delete",
        "reason": reason or "",
        "next": (
            "operator must run `python scripts/admin.py approve-delete "
            f"--post-id {post_id}` before the delete reaches Meta"
        ),
    }
