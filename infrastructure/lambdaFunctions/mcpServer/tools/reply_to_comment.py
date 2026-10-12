"""Stage a reply to a comment on a Page post (DESIGN.md section 7.1).

Replies reach the public immediately, so by default a prompt-injected
agent must not be able to send one on its own. This tool stages the
pending text as a `REPLY#<comment_id>` item; the Publisher reads it
after the operator approves (`admin.py approve-reply`). Policy is
checked here so the operator sees warnings at stage time, and re-run in
the Publisher so a config change between approve and publish is
honored.

Per-account `auto_verbs` (M10) opts an account into one-shot auto-approve
for selected verbs. When `"reply"` is in `auto_verbs`, calling this tool
with `auto_publish=1` records the implicit approval and fires the
Publisher synchronously. Policy still runs at both stage and publish
time; only the human step is skipped.
"""

from __future__ import annotations

import os

from sharedModules import approval, dynamo, policy, registry
from sharedModules.errors import AccountError
from sharedModules.providers import get_provider
from sharedModules.providers.base import Capability

from ._common import invoke_publisher


def reply_to_comment(
    comment_id: str,
    text: str,
    account: str = "",
    auto_publish: int = 0,
) -> dict:
    """Stage a reply under an existing comment for operator approval.

    - `comment_id` (required): the comment's Meta-native id from
      `list_comments`.
    - `text` (required): the reply text.
    - `account`: alias from `list_accounts`. Omit when only one is
      configured.
    - `auto_publish`: 1 to record implicit approval and fire the
      Publisher immediately. Requires `"reply"` in the account's
      `auto_verbs`; otherwise returns a specific error without staging.

    Returns `{ok, comment_id, staged_change, warnings, next}` on manual
    flow, or the Publisher's envelope on auto flow. The manual next step
    is `python scripts/admin.py approve-reply --comment-id <id>`.
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

    entry = registry.configured_accounts()[alias]
    auto_verbs = entry.get("auto_verbs") or []
    want_auto = bool(int(auto_publish or 0))
    if want_auto and "reply" not in auto_verbs:
        return {
            "ok": False,
            "error": (
                f"reply auto-publish not enabled on account '{alias}'; "
                "add 'reply' to auto_verbs in config and redeploy, or run "
                f"`python scripts/admin.py approve-reply --comment-id {comment_id}`"
            ),
        }

    # Policy is enforced at stage time so a blocked reply never
    # consumes operator attention. The Publisher re-runs it.
    from types import SimpleNamespace

    draft_like = SimpleNamespace(
        account=alias, kind="text", text=text, link="", media=[], schedule=""
    )
    pol = entry.get("policy") or {}
    try:
        policy.enforce(draft_like, pol)
    except policy.PolicyError as e:
        return {"ok": False, "error": f"policy: {e}"}
    warnings = policy.warnings_from(policy.validate(draft_like, pol))

    actor = os.environ.get("AGENT_IDENTITY", "mcp_client")
    dynamo.write_staged_reply(
        comment_id=comment_id,
        account=alias,
        text=text,
        created_by=actor,
    )
    dynamo.delete_approval_record(f"reply:{comment_id}")

    dynamo.append_audit(
        {
            "account": alias,
            "kind": "reply",
            "actor_identity": actor,
            "comment_id": comment_id,
            "result": "staged",
        }
    )

    if want_auto:
        approval.record_reply_approval(comment_id, text, approved_by=f"server (auto_verbs:{actor})")
        dynamo.append_audit(
            {
                "account": alias,
                "kind": "approve-reply",
                "actor_identity": actor,
                "comment_id": comment_id,
                "result": "auto",
            }
        )
        return invoke_publisher(
            {
                "kind": "reply",
                "comment_id": comment_id,
                "actor": f"mcp:{actor}",
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
