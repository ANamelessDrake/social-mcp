"""Publisher Lambda: the only path that calls Meta's write endpoints.

Invoked two ways:

- Synchronously by the MCP server (`publish_draft`, `edit_post`,
  `delete_post`, `reply_to_comment`, `hide_comment`) via
  `lambda:InvokeFunction` with `InvocationType=RequestResponse`.
- Asynchronously by EventBridge Scheduler at `scheduled_time`
  (`schedule_draft` tool) with the same payload shape.

The event discriminator is `kind` (default `publish` for M2 compat):

- `{"kind": "publish",  "draft_id": "..."}`  (M2)
- `{"kind": "schedule", "draft_id": "..."}`  (M2, scheduled)
- `{"kind": "edit",     "post_id": "..."}`   (M3)
- `{"kind": "delete",   "post_id": "..."}`   (M3)
- `{"kind": "reply",    "comment_id": "..."}` (M3)
- `{"kind": "hide",     "account": "...", "comment_id": "..."}` (M3)

Flow is kind-specific. For every path:
- re-verify the matching per-item approval (hash, or standing for hide),
- re-run policy on any text that will reach Meta,
- read the operator-approved payload from the staged change item,
- check kill switches,
- dispatch to the provider,
- audit the result (`result: ok` on success; `result: error:<class>` on
  failure, with the mapped message, never a token).
"""

from __future__ import annotations

import json
import logging
import os
import sys

# When running on Lambda, sharedModules is bundled under /var/task/sharedModules;
# in tests the import path is configured by conftest.py.
LAMBDA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)

from sharedModules import approval as approval_mod  # noqa: E402
from sharedModules import dynamo, policy, registry  # noqa: E402
from sharedModules.errors import AccountError, CapabilityError  # noqa: E402
from sharedModules.providers import get_provider  # noqa: E402

log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, context):
    """Entry point for every synchronous and scheduled invocation."""
    event = event or {}
    kind = (event.get("kind") or "publish").lower()
    actor = event.get("actor") or "publisher"

    if kind in ("publish", "schedule"):
        return _handle_publish(event, actor)
    if kind == "edit":
        return _handle_edit(event, actor)
    if kind == "delete":
        return _handle_delete(event, actor)
    if kind == "reply":
        return _handle_reply(event, actor)
    if kind == "hide":
        return _handle_hide(event, actor)
    return _error(f"unknown publisher event kind '{kind}'")


# ---------------- publish / schedule (M2) ----------------


def _handle_publish(event: dict, actor: str) -> dict:
    draft_id = event.get("draft_id") or ""
    scope = event.get("scope") or "publish"
    if not draft_id:
        return _error("missing draft_id in event")

    draft = None
    try:
        draft = dynamo.read_draft(draft_id)
        if draft is None:
            return _error(f"draft '{draft_id}' not found")

        if draft.get("status") == "discarded":
            return _error(f"draft '{draft_id}' is discarded")

        # 1. Approval re-check (the gate, DESIGN.md section 7.3)
        approval_mod.verify_approval(draft_id, scope=scope)

        account = draft["account"]

        # 2. Policy re-run against the current config
        entry = registry.configured_accounts().get(account)
        if entry is None:
            return _error(f"account '{account}' is no longer configured")

        _enforce_policy(draft, entry.get("policy") or {})

        # 3. Kill switches (DESIGN.md section 7.6)
        kill = _kill_switch_check(entry, account)
        if kill is not None:
            return kill

        # 4. Dispatch
        dynamo.update_draft_fields(draft_id, {"status": "publishing"})
        provider = get_provider(account)
        result = provider.publish(_as_namespace(draft))
        permalink = getattr(result, "permalink", "") or ""
        post_id = getattr(result, "post_id", "") or ""
        dynamo.update_draft_fields(
            draft_id,
            {"status": "published", "post_id": post_id, "permalink": permalink},
        )

        dynamo.append_audit(
            {
                "account": account,
                "kind": "publish",
                "actor_identity": actor,
                "draft_id": draft_id,
                "post_id": post_id,
                "result": "ok",
            }
        )
        return {
            "ok": True,
            "draft_id": draft_id,
            "post_id": post_id,
            "permalink": permalink,
        }

    except approval_mod.ApprovalError as e:
        _audit_publish_failure(draft, draft_id, actor, "approval_mismatch", str(e))
        return _error(str(e))
    except policy.PolicyError as e:
        _audit_publish_failure(draft, draft_id, actor, "policy", str(e))
        return _error(f"policy: {e}")
    except CapabilityError as e:
        _audit_publish_failure(draft, draft_id, actor, "capability", str(e))
        return _error(str(e))
    except NotImplementedError as e:
        _audit_publish_failure(draft, draft_id, actor, "not_implemented", str(e))
        return _error(str(e) or "that kind is not yet supported on this provider")
    except AccountError as e:
        _audit_publish_failure(draft, draft_id, actor, "provider", str(e))
        return _error(str(e))
    except Exception as e:  # noqa: BLE001 - last-ditch safety
        log.exception("unexpected publisher failure")
        _audit_publish_failure(draft, draft_id, actor, "unexpected", "internal error")
        return _error(f"unexpected failure: {type(e).__name__}")


# ---------------- edit / delete / reply / hide (M3) ----------------


def _handle_edit(event: dict, actor: str) -> dict:
    post_id = event.get("post_id") or ""
    if not post_id:
        return _error("missing post_id in event")
    staged = dynamo.read_staged_edit(post_id)
    if staged is None:
        return _error(f"edit of '{post_id}' has no staged change")
    account = staged["account"]
    text = staged.get("text", "") or ""

    try:
        # 1. Approval re-check against the staged content
        approval_mod.verify_edit_approval(post_id, text)
        # 2. Policy re-run on the text
        entry = _entry_or_fail(account)
        if isinstance(entry, dict) and "ok" not in entry:  # entry is a config dict
            _enforce_text_policy(text, entry.get("policy") or {})
        else:
            return entry  # was an error envelope
        # 3. Kill switches
        kill = _kill_switch_check(entry, account)
        if kill is not None:
            return kill
        # 4. Dispatch
        provider = get_provider(account)
        provider.edit_post(post_id, text)
        # 5. Audit + cleanup
        dynamo.append_audit(
            {
                "account": account,
                "kind": "edit",
                "actor_identity": actor,
                "post_id": post_id,
                "result": "ok",
            }
        )
        dynamo.delete_approval_record(f"edit:{post_id}")
        dynamo.delete_staged_edit(post_id)
        return {"ok": True, "post_id": post_id}
    except approval_mod.ApprovalError as e:
        _audit_item_failure(account, "edit", actor, post_id, "approval_mismatch", str(e))
        return _error(str(e))
    except policy.PolicyError as e:
        _audit_item_failure(account, "edit", actor, post_id, "policy", str(e))
        return _error(f"policy: {e}")
    except AccountError as e:
        _audit_item_failure(account, "edit", actor, post_id, "provider", str(e))
        return _error(str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("unexpected edit failure")
        _audit_item_failure(account, "edit", actor, post_id, "unexpected", "internal error")
        return _error(f"unexpected failure: {type(e).__name__}")


def _handle_delete(event: dict, actor: str) -> dict:
    post_id = event.get("post_id") or ""
    if not post_id:
        return _error("missing post_id in event")
    staged = dynamo.read_staged_delete(post_id)
    if staged is None:
        return _error(f"delete of '{post_id}' has no staged change")
    account = staged["account"]
    reason = staged.get("reason", "") or ""

    try:
        approval_mod.verify_delete_approval(post_id, reason)
        entry = _entry_or_fail(account)
        if not isinstance(entry, dict) or "ok" in entry:
            return entry
        kill = _kill_switch_check(entry, account)
        if kill is not None:
            return kill
        provider = get_provider(account)
        provider.delete_post(post_id)
        dynamo.append_audit(
            {
                "account": account,
                "kind": "delete",
                "actor_identity": actor,
                "post_id": post_id,
                "reason": reason,
                "result": "ok",
            }
        )
        dynamo.delete_approval_record(f"delete:{post_id}")
        dynamo.delete_staged_delete(post_id)
        return {"ok": True, "post_id": post_id}
    except approval_mod.ApprovalError as e:
        _audit_item_failure(account, "delete", actor, post_id, "approval_mismatch", str(e))
        return _error(str(e))
    except AccountError as e:
        _audit_item_failure(account, "delete", actor, post_id, "provider", str(e))
        return _error(str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("unexpected delete failure")
        _audit_item_failure(account, "delete", actor, post_id, "unexpected", "internal error")
        return _error(f"unexpected failure: {type(e).__name__}")


def _handle_reply(event: dict, actor: str) -> dict:
    comment_id = event.get("comment_id") or ""
    if not comment_id:
        return _error("missing comment_id in event")
    staged = dynamo.read_staged_reply(comment_id)
    if staged is None:
        return _error(f"reply to '{comment_id}' has no staged change")
    account = staged["account"]
    text = staged.get("text", "") or ""

    try:
        approval_mod.verify_reply_approval(comment_id, text)
        entry = _entry_or_fail(account)
        if not isinstance(entry, dict) or "ok" in entry:
            return entry
        _enforce_text_policy(text, entry.get("policy") or {})
        kill = _kill_switch_check(entry, account)
        if kill is not None:
            return kill
        provider = get_provider(account)
        new_comment = provider.reply_to_comment(comment_id, text)
        new_id = getattr(new_comment, "comment_id", "") or ""
        dynamo.append_audit(
            {
                "account": account,
                "kind": "reply",
                "actor_identity": actor,
                "comment_id": comment_id,
                "post_id": new_id,
                "result": "ok",
            }
        )
        dynamo.delete_approval_record(f"reply:{comment_id}")
        dynamo.delete_staged_reply(comment_id)
        return {"ok": True, "comment_id": comment_id, "reply_id": new_id}
    except approval_mod.ApprovalError as e:
        _audit_item_failure(account, "reply", actor, comment_id, "approval_mismatch", str(e))
        return _error(str(e))
    except policy.PolicyError as e:
        _audit_item_failure(account, "reply", actor, comment_id, "policy", str(e))
        return _error(f"policy: {e}")
    except AccountError as e:
        _audit_item_failure(account, "reply", actor, comment_id, "provider", str(e))
        return _error(str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("unexpected reply failure")
        _audit_item_failure(account, "reply", actor, comment_id, "unexpected", "internal error")
        return _error(f"unexpected failure: {type(e).__name__}")


def _handle_hide(event: dict, actor: str) -> dict:
    account = event.get("account") or ""
    comment_id = event.get("comment_id") or ""
    if not account or not comment_id:
        return _error("missing account or comment_id in hide event")
    try:
        approval_mod.verify_hide_approval(account)
        entry = _entry_or_fail(account)
        if not isinstance(entry, dict) or "ok" in entry:
            return entry
        kill = _kill_switch_check(entry, account)
        if kill is not None:
            return kill
        provider = get_provider(account)
        provider.hide_comment(comment_id)
        dynamo.append_audit(
            {
                "account": account,
                "kind": "hide",
                "actor_identity": actor,
                "comment_id": comment_id,
                "result": "ok",
            }
        )
        dynamo.delete_hide_request(account, comment_id)
        return {"ok": True, "account": account, "comment_id": comment_id}
    except approval_mod.ApprovalError as e:
        _audit_item_failure(account, "hide", actor, comment_id, "approval_mismatch", str(e))
        return _error(str(e))
    except AccountError as e:
        _audit_item_failure(account, "hide", actor, comment_id, "provider", str(e))
        return _error(str(e))
    except Exception as e:  # noqa: BLE001
        log.exception("unexpected hide failure")
        _audit_item_failure(account, "hide", actor, comment_id, "unexpected", "internal error")
        return _error(f"unexpected failure: {type(e).__name__}")


# ---------------- shared helpers ----------------


def _entry_or_fail(account: str):
    entry = registry.configured_accounts().get(account)
    if entry is None:
        return _error(f"account '{account}' is no longer configured")
    return entry


def _kill_switch_check(entry: dict, account: str) -> dict | None:
    if dynamo.publishing_disabled():
        return _error(
            "publishing is globally disabled; "
            "re-enable with `python scripts/admin.py disable-publishing --enable`"
        )
    if os.environ.get("PUBLISHING_ENABLED", "false").lower() != "true":
        return _error(
            "publishing is disabled by configuration "
            "(set publishing_enabled: true in config and redeploy)"
        )
    if not entry.get("publish_enabled", False):
        return _error(
            f"account '{account}' has publish_enabled: false; "
            "enable it in config and redeploy"
        )
    return None


def _enforce_text_policy(text: str, pol: dict) -> None:
    """Policy for text-only verbs (reply, edit); media/link don't apply."""
    from types import SimpleNamespace

    draft_like = SimpleNamespace(
        account="",
        kind="text",
        text=text or "",
        link="",
        media=[],
        schedule="",
    )
    policy.enforce(draft_like, pol or {})


def _enforce_policy(draft: dict, pol: dict) -> None:
    policy.enforce(_as_namespace(draft), pol)


def _as_namespace(draft: dict):
    """Give the policy / provider code attribute access to a plain dict."""
    from types import SimpleNamespace

    return SimpleNamespace(
        account=draft.get("account", ""),
        kind=draft.get("kind", ""),
        text=draft.get("text", "") or "",
        link=draft.get("link", "") or "",
        media=draft.get("media", []) or [],
        schedule=draft.get("schedule", "") or "",
        draft_id=draft.get("draft_id", ""),
    )


def _audit_publish_failure(draft, draft_id: str, actor: str, kind: str, message: str) -> None:
    try:
        account = (draft or {}).get("account", "unknown")
        dynamo.update_draft_fields(draft_id, {"status": "failed", "last_error": message})
        dynamo.append_audit(
            {
                "account": account,
                "kind": "publish",
                "actor_identity": actor,
                "draft_id": draft_id,
                "result": f"error:{kind}",
            }
        )
    except Exception:  # noqa: BLE001 - we are already in an error path
        log.exception("failed to audit publisher failure")


def _audit_item_failure(
    account: str, kind: str, actor: str, item_id: str, error_class: str, _message: str
) -> None:
    """One audit row per failed edit/delete/reply/hide; staged + approval records left alone."""
    try:
        row = {
            "account": account or "unknown",
            "kind": kind,
            "actor_identity": actor,
            "result": f"error:{error_class}",
        }
        if kind in ("edit", "delete"):
            row["post_id"] = item_id
        else:
            row["comment_id"] = item_id
        dynamo.append_audit(row)
    except Exception:  # noqa: BLE001 - we are already in an error path
        log.exception("failed to audit %s failure", kind)


def _error(message: str) -> dict:
    return {"ok": False, "error": message}


# Local test entrypoint; EventBridge Scheduler and lambda:Invoke both call
# handler() directly.
if __name__ == "__main__":  # pragma: no cover - manual smoke only
    print(json.dumps(handler(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}, None)))
