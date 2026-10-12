"""Stage a reply to a comment on a Page post (DESIGN.md section 7.1).

Replies reach the public immediately, so a prompt-injected agent must
not be able to send one on its own. This tool stages the pending text
as a `REPLY#<comment_id>` item; the Publisher reads it after the
operator approves (`admin.py approve-reply`). Policy is checked here so
the operator sees warnings at stage time, and re-run in the Publisher
so a config change between approve and publish is honored.
"""

from __future__ import annotations

import os

from sharedModules import dynamo, policy, registry
from sharedModules.errors import AccountError
from sharedModules.providers import get_provider
from sharedModules.providers.base import Capability


def reply_to_comment(comment_id: str, text: str, account: str = "") -> dict:
    """Stage a reply under an existing comment for operator approval.

    - `comment_id` (required): the comment's Meta-native id from
      `list_comments`.
    - `text` (required): the reply text.
    - `account`: alias from `list_accounts`. Omit when only one is
      configured.

    Returns `{ok, comment_id, staged_change, warnings, next}`. The next
    step is `python scripts/admin.py approve-reply --comment-id <id>`.
    """
    try:
        alias = registry.resolve_alias(account)
    except AccountError as e:
        return {"ok": False, "error": str(e)}

    if not comment_id:
        return {"ok": False, "error": "comment_id is required"}
    if not (text or "").strip():
        return {"ok": False, "error": "text is required for a reply"}

    provider = get_provider(alias)
    if Capability.COMMENTS not in provider.capabilities:
        return {"ok": False, "error": f"provider '{provider.name}' does not support comments"}

    # Policy is enforced at stage time so a blocked reply never
    # consumes operator attention. The Publisher re-runs it.
    from types import SimpleNamespace

    draft_like = SimpleNamespace(
        account=alias, kind="text", text=text, link="", media=[], schedule=""
    )
    entry = registry.configured_accounts()[alias]
    pol = entry.get("policy") or {}
    try:
        policy.enforce(draft_like, pol)
    except policy.PolicyError as e:
        return {"ok": False, "error": f"policy: {e}"}
    warnings = policy.warnings_from(policy.validate(draft_like, pol))

    dynamo.write_staged_reply(
        comment_id=comment_id,
        account=alias,
        text=text,
        created_by=os.environ.get("AGENT_IDENTITY", "mcp_client"),
    )
    dynamo.delete_approval_record(f"reply:{comment_id}")

    dynamo.append_audit(
        {
            "account": alias,
            "kind": "reply",
            "actor_identity": os.environ.get("AGENT_IDENTITY", "mcp_client"),
            "comment_id": comment_id,
            "result": "staged",
        }
    )

    return {
        "ok": True,
        "comment_id": comment_id,
        "staged_change": "reply",
        "preview": text if len(text) <= 300 else text[:299] + "…",
        "warnings": warnings,
        "next": (
            "operator must run `python scripts/admin.py approve-reply "
            f"--comment-id {comment_id}` before the reply reaches Meta"
        ),
    }
