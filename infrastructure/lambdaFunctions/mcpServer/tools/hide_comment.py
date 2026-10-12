"""Hide a comment on a Page post (DESIGN.md section 7.1).

Hiding is reversible from Meta's own UI, so one standing operator
approval per account covers every hide the agent asks for. On the
first hide, the tool stages a `HIDE_REQUEST#<account>#<comment_id>`
item and asks the operator to run `admin.py approve-hide --account
<alias>` once. From then on, this tool invokes the Publisher
synchronously and the hide reaches Meta in-call.

The per-comment audit requirement still holds: every hide, standing or
not, writes an `AUDIT#<account>` row, so the trail shows which comment
on which post went away.
"""

from __future__ import annotations

import os

from sharedModules import dynamo, registry
from sharedModules.errors import AccountError
from sharedModules.providers import get_provider
from sharedModules.providers.base import Capability

from ._common import invoke_publisher


def hide_comment(comment_id: str, account: str = "") -> dict:
    """Hide a comment on a Page post.

    - `comment_id` (required): the comment's Meta-native id.
    - `account`: alias from `list_accounts`. Omit when only one is
      configured.

    First call without a standing hide approval on this account: writes
    a HIDE_REQUEST and tells the operator what to run. Subsequent
    calls on an account with a standing approval: hide proceeds
    immediately via the Publisher and the result is returned here.
    """
    try:
        alias = registry.resolve_alias(account)
    except AccountError as e:
        return {"ok": False, "error": str(e)}

    if not comment_id:
        return {"ok": False, "error": "comment_id is required"}

    provider = get_provider(alias)
    if Capability.COMMENTS not in provider.capabilities:
        return {"ok": False, "error": f"provider '{provider.name}' does not support comments"}

    # Always record a HIDE_REQUEST so the audit trail is complete and
    # the approve-hide sweep can process queued items.
    dynamo.write_hide_request(
        account=alias,
        comment_id=comment_id,
        created_by=os.environ.get("AGENT_IDENTITY", "mcp_client"),
    )

    standing = dynamo.read_hide_standing(alias)
    if standing is None:
        dynamo.append_audit(
            {
                "account": alias,
                "kind": "hide",
                "actor_identity": os.environ.get("AGENT_IDENTITY", "mcp_client"),
                "comment_id": comment_id,
                "result": "queued",
            }
        )
        return {
            "ok": True,
            "staged_change": "hide",
            "account": alias,
            "comment_id": comment_id,
            "next": (
                f"operator must run `python scripts/admin.py approve-hide --account {alias}` "
                "once to allow hides on this account. Any already-queued hide requests "
                "are swept in that call. Future hides on this account then proceed "
                "immediately without re-approval."
            ),
        }

    # Standing approval exists: invoke the Publisher synchronously.
    return invoke_publisher(
        {
            "kind": "hide",
            "account": alias,
            "comment_id": comment_id,
            "actor": os.environ.get("AGENT_IDENTITY", "mcp_client"),
        }
    )
