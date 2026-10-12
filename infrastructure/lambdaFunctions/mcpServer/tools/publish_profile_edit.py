"""Apply a staged, approved profile edit via the Publisher Lambda.

The MCP server never calls the provider's profile endpoints directly;
it re-checks the approval + policy locally for a fast fail, then
synchronously invokes the Publisher Lambda, which re-checks and does
the provider call.
"""

from __future__ import annotations

import os

from sharedModules import approval as approval_mod
from sharedModules import dynamo, policy, registry
from sharedModules.errors import AccountError

from ._common import invoke_publisher


def publish_profile_edit(account: str = "") -> dict:
    """Apply an approved profile edit.

    - `account`: alias from `list_accounts`; omit when only one account
      is configured.

    Returns `{ok, account}` on success. Fails if there is no staged
    change, no approval record, or if the staged patch no longer matches
    the approval (edited after approval). Policy runs again on the
    Publisher side so a config change between approval and publish is
    honored.
    """
    try:
        alias = registry.resolve_alias(account)
    except AccountError as e:
        return {"ok": False, "error": str(e)}

    staged = dynamo.read_staged_profile_edit(alias)
    if staged is None:
        return {
            "ok": False,
            "error": (
                f"profile edit of '{alias}' has no staged change; call draft_profile_edit first"
            ),
        }
    patch = staged.get("patch") or {}

    try:
        approval_mod.verify_profile_approval(alias, patch)
    except approval_mod.ApprovalError as e:
        return {"ok": False, "error": str(e)}

    entry = registry.configured_accounts().get(alias)
    if entry is None:
        return {"ok": False, "error": f"account '{alias}' is no longer configured"}
    try:
        policy.enforce_profile_patch(patch, entry.get("policy") or {})
    except policy.PolicyError as e:
        return {"ok": False, "error": f"policy: {e}"}

    actor = f"mcp:{os.environ.get('AGENT_IDENTITY', 'client')}"
    return invoke_publisher({"kind": "profile_edit", "account": alias, "actor": actor})
