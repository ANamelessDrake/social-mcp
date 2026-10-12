"""Stage an edit to an existing Page post (DESIGN.md section 7.1).

Edit touches a post that is already live, so by default `publish_mode:
auto` does not cover it: every edit needs an explicit operator approval
(`admin.py approve-edit`).

Per-account `auto_verbs` (M10) opts an account into one-shot auto-approve
for selected verbs. When `"edit"` is in `auto_verbs`, calling this tool
with `auto_publish=1` records the implicit approval, fires the Publisher
synchronously, and returns the Publisher's envelope. The hash is still
computed, policy still re-runs, and the audit log is unchanged; only the
human step is skipped. `delete` and `profile_edit` are never eligible.

This tool stages the pending text as an `EDIT#<post_id>` item; the
Publisher reads that item at approve-time, so what reaches Meta is the
text the operator (or auto-approve) saw, not whatever the agent re-sent
afterward. The tool itself never calls Meta.
"""

from __future__ import annotations

import os

from sharedModules import approval, dynamo, registry
from sharedModules.errors import AccountError, CapabilityError
from sharedModules.providers import get_provider
from sharedModules.providers.base import Capability

from ._common import invoke_publisher


def edit_post(post_id: str, text: str, account: str = "", auto_publish: int = 0) -> dict:
    """Stage an edit to a Page post for operator approval.

    - `post_id` (required): the post's Meta-native id (e.g.
      '100000000000001_900000000000099'), from `list_posts` or
      `get_post`.
    - `text` (required): the new message text. Must be non-empty; to
      remove content, delete the post instead.
    - `account`: alias from `list_accounts`. Omit when only one is
      configured.
    - `auto_publish`: 1 to record implicit approval and fire the
      Publisher immediately. Requires `"edit"` in the account's
      `auto_verbs`; otherwise returns a specific error without staging.

    Returns `{ok, post_id, staged_change, next}` on manual flow, or the
    Publisher's envelope on auto flow (`{ok, post_id}` on success). The
    next step for the manual flow is `python scripts/admin.py
    approve-edit --post-id <id>`; nothing reaches Meta until the operator
    approves. Facebook rejects edits on posts the deployment's Meta app
    did not create, which the Publisher reports with a specific error
    when it tries.
    """
    try:
        alias = registry.resolve_alias(account)
    except AccountError as e:
        return {"ok": False, "error": str(e)}

    if not post_id:
        return {"ok": False, "error": "post_id is required"}
    if not (text or "").strip():
        return {"ok": False, "error": "text is required; use delete_post to remove a post"}

    provider = get_provider(alias)
    if Capability.EDIT not in provider.capabilities:
        return {"ok": False, "error": f"provider '{provider.name}' does not support edits"}

    entry = registry.configured_accounts()[alias]
    auto_verbs = entry.get("auto_verbs") or []
    want_auto = bool(int(auto_publish or 0))
    if want_auto and "edit" not in auto_verbs:
        return {
            "ok": False,
            "error": (
                f"edit auto-publish not enabled on account '{alias}'; "
                "add 'edit' to auto_verbs in config and redeploy, or run "
                f"`python scripts/admin.py approve-edit --post-id {post_id}`"
            ),
        }

    actor = os.environ.get("AGENT_IDENTITY", "mcp_client")
    try:
        dynamo.write_staged_edit(
            post_id=post_id,
            account=alias,
            text=text,
            created_by=actor,
        )
    except CapabilityError as e:
        return {"ok": False, "error": str(e)}

    # Replace any prior approval record for this post (edit text just changed).
    dynamo.delete_approval_record(f"edit:{post_id}")

    dynamo.append_audit(
        {
            "account": alias,
            "kind": "edit",
            "actor_identity": actor,
            "post_id": post_id,
            "result": "staged",
        }
    )

    if want_auto:
        approval.record_edit_approval(post_id, text, approved_by=f"server (auto_verbs:{actor})")
        dynamo.append_audit(
            {
                "account": alias,
                "kind": "approve-edit",
                "actor_identity": actor,
                "post_id": post_id,
                "result": "auto",
            }
        )
        return invoke_publisher(
            {
                "kind": "edit",
                "post_id": post_id,
                "actor": f"mcp:{actor}",
            }
        )

    return {
        "ok": True,
        "post_id": post_id,
        "staged_change": "edit",
        "preview": text if len(text) <= 300 else text[:299] + "…",
        "next": (
            "operator must run `python scripts/admin.py approve-edit "
            f"--post-id {post_id}` before the edit reaches Meta"
        ),
    }
