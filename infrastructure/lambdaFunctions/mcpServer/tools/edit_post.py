"""Stage an edit to an existing Page post (DESIGN.md section 7.1).

Edit, delete, and reply all touch posts that are already live, so
`publish_mode: auto` does not cover them: every edit needs an explicit
operator approval of the specific change (`admin.py approve-edit`).

This tool stages the pending text as an `EDIT#<post_id>` item; the
Publisher reads that item at approve-time, so what reaches Meta is the
text the operator saw, not whatever the agent re-sent afterward. The
tool does not call Meta.
"""

from __future__ import annotations

import os

from sharedModules import dynamo, registry
from sharedModules.errors import AccountError, CapabilityError
from sharedModules.providers import get_provider
from sharedModules.providers.base import Capability


def edit_post(post_id: str, text: str, account: str = "") -> dict:
    """Stage an edit to a Page post for operator approval.

    - `post_id` (required): the post's Meta-native id (e.g.
      '100000000000001_900000000000099'), from `list_posts` or
      `get_post`.
    - `text` (required): the new message text. Must be non-empty; to
      remove content, delete the post instead.
    - `account`: alias from `list_accounts`. Omit when only one is
      configured.

    Returns `{ok, post_id, staged_change, next}` on success. The next
    step is `python scripts/admin.py approve-edit --post-id <id>`;
    nothing reaches Meta until the operator approves. Facebook rejects
    edits on posts the deployment's Meta app did not create, which the
    Publisher reports with a specific error when it tries.
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

    try:
        dynamo.write_staged_edit(
            post_id=post_id,
            account=alias,
            text=text,
            created_by=os.environ.get("AGENT_IDENTITY", "mcp_client"),
        )
    except CapabilityError as e:
        return {"ok": False, "error": str(e)}

    # Replace any prior approval record for this post (edit text just changed).
    dynamo.delete_approval_record(f"edit:{post_id}")

    dynamo.append_audit(
        {
            "account": alias,
            "kind": "edit",
            "actor_identity": os.environ.get("AGENT_IDENTITY", "mcp_client"),
            "post_id": post_id,
            "result": "staged",
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
