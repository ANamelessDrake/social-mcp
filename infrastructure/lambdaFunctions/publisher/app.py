"""Publisher Lambda: the only path that calls Meta's publish endpoints.

Invoked two ways:

- Synchronously by the MCP server (`publish_draft` tool) via
  `lambda:InvokeFunction` with `InvocationType=RequestResponse`.
- Asynchronously by EventBridge Scheduler at `scheduled_time`
  (`schedule_draft` tool) with the same payload shape.

Both deliver `{"draft_id": "<id>"}` and get back a result envelope. The
Publisher is isolated from the MCP server deliberately (DESIGN.md
section 2) so the one path that posts to Meta has a dedicated role, a
dedicated review surface, and no exposure to MCP client traffic.

Flow:

1. Load the draft; refuse unless status is approved or scheduled.
2. Re-verify the approval hash (DESIGN.md section 7.3). This is the
   centerpiece: in `auto` mode the record was written at draft time, in
   `manual` by the operator; the Publisher does not care which, it cares
   that the current draft hash still matches.
3. Re-run the per-account policy. A config edit between approval and
   publish must not slip stale content past the fence.
4. Check the global kill switch and the per-account `publish_enabled` flag.
5. Dispatch to the provider's `publish(draft)`.
6. Audit the result (`result: ok` on success; `result: error:<meta_code>`
   on failure, with the mapped message, never a token).
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
    """Entry point for both synchronous and scheduled invocations."""
    draft_id = (event or {}).get("draft_id") or ""
    scope = (event or {}).get("scope") or "publish"
    actor = (event or {}).get("actor") or "publisher"
    if not draft_id:
        return _error("missing draft_id in event")

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
        _audit_failure(draft, draft_id, actor, "approval_mismatch", str(e))
        return _error(str(e))
    except policy.PolicyError as e:
        _audit_failure(draft, draft_id, actor, "policy", str(e))
        return _error(f"policy: {e}")
    except CapabilityError as e:
        _audit_failure(draft, draft_id, actor, "capability", str(e))
        return _error(str(e))
    except NotImplementedError as e:
        _audit_failure(draft, draft_id, actor, "not_implemented", str(e))
        return _error(str(e) or "that kind is not yet supported on this provider")
    except AccountError as e:
        _audit_failure(draft, draft_id, actor, "provider", str(e))
        return _error(str(e))
    except Exception as e:  # noqa: BLE001 - last-ditch safety
        log.exception("unexpected publisher failure")
        _audit_failure(draft, draft_id, actor, "unexpected", "internal error")
        return _error(f"unexpected failure: {type(e).__name__}")


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


def _audit_failure(draft, draft_id: str, actor: str, kind: str, message: str) -> None:
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


def _error(message: str) -> dict:
    return {"ok": False, "error": message}


# Local test entrypoint; EventBridge Scheduler and lambda:Invoke both call
# handler() directly.
if __name__ == "__main__":  # pragma: no cover - manual smoke only
    print(json.dumps(handler(json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}, None)))
