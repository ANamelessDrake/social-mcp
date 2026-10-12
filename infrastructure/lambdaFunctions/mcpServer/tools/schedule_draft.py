"""Schedule a draft to publish at a future time.

Scheduling is server-side (EventBridge Scheduler creates a one-shot
schedule in the publish group), not Meta-native, so the approval
re-check and policy re-run both run at publish time rather than at
approval time (DESIGN.md section 7.3). Facebook's native
`scheduled_publish_time` is deliberately not used.

For Facebook, Meta's own window for native scheduling is 10 minutes to
30 days ahead; we enforce the same at this layer for behavioral parity.
For Instagram (M5), any future time is allowed.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import boto3
from sharedModules import approval as approval_mod
from sharedModules import dynamo, policy, registry
from sharedModules.errors import AccountError

from ._common import SCHEDULE_SCOPE

MIN_LEAD = timedelta(minutes=10)
MAX_LEAD_FB = timedelta(days=30)


def schedule_draft(draft_id: str, scheduled_time: str) -> dict:
    """Schedule a draft for a future publish.

    `scheduled_time` is ISO 8601 UTC (for example '2026-12-31T15:00:00Z').
    """
    draft = dynamo.read_draft(draft_id)
    if draft is None:
        return {"ok": False, "error": f"draft '{draft_id}' not found"}
    if draft.get("status") == "discarded":
        return {"ok": False, "error": f"draft '{draft_id}' is discarded"}

    try:
        when = _parse_iso(scheduled_time)
    except ValueError as e:
        return {"ok": False, "error": f"scheduled_time: {e}"}

    now = datetime.now(UTC)
    lead = when - now
    if lead < MIN_LEAD:
        minutes = int(MIN_LEAD.total_seconds() // 60)
        return {
            "ok": False,
            "error": f"scheduled_time must be at least {minutes} minutes in the future",
        }

    entry = registry.configured_accounts().get(draft["account"])
    if entry is None:
        return {"ok": False, "error": f"account '{draft['account']}' is no longer configured"}

    if entry.get("provider") == "facebook" and lead > MAX_LEAD_FB:
        return {
            "ok": False,
            "error": "scheduled_time is more than 30 days ahead; Facebook rejects those",
        }

    # Approval and policy re-check locally (Publisher re-runs at publish time)
    try:
        approval_mod.verify_approval(draft_id, scope=SCHEDULE_SCOPE)
    except approval_mod.ApprovalError:
        # Fall back to a publish-scope approval; schedule reuses publish
        # approval unless the operator approved specifically for schedule.
        try:
            approval_mod.verify_approval(draft_id, scope="publish")
        except approval_mod.ApprovalError as e2:
            return {"ok": False, "error": str(e2)}
    except AccountError as e:
        return {"ok": False, "error": str(e)}
    try:
        policy.enforce(_ns(draft), entry.get("policy") or {})
    except policy.PolicyError as e:
        return {"ok": False, "error": f"policy: {e}"}

    arn = os.environ.get("PUBLISHER_FUNCTION_ARN")
    group = os.environ.get("SCHEDULE_GROUP_NAME")
    role_arn = os.environ.get("SCHEDULER_ROLE_ARN")
    if not (arn and group and role_arn):
        return {
            "ok": False,
            "error": (
                "scheduler environment is incomplete "
                "(PUBLISHER_FUNCTION_ARN, SCHEDULE_GROUP_NAME, SCHEDULER_ROLE_ARN)"
            ),
        }

    name = _schedule_name(draft_id)
    # EventBridge Scheduler expects at() format in UTC with no suffix.
    at = when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    client = _scheduler()
    try:
        client.create_schedule(
            Name=name,
            GroupName=group,
            ScheduleExpression=f"at({at})",
            ScheduleExpressionTimezone="UTC",
            FlexibleTimeWindow={"Mode": "OFF"},
            Target={
                "Arn": arn,
                "RoleArn": role_arn,
                "Input": json.dumps({"draft_id": draft_id, "scope": SCHEDULE_SCOPE}),
                "RetryPolicy": {"MaximumRetryAttempts": 0},
            },
            ActionAfterCompletion="DELETE",
        )
    except client.exceptions.ConflictException:
        return {
            "ok": False,
            "error": f"draft '{draft_id}' is already scheduled; discard it or wait for it to fire",
        }

    dynamo.update_draft_fields(
        draft_id,
        {"status": "scheduled", "schedule": when.isoformat(), "schedule_name": name},
    )
    dynamo.append_audit(
        {
            "account": draft["account"],
            "kind": "schedule",
            "actor_identity": draft.get("created_by", "mcp_client"),
            "draft_id": draft_id,
            "result": name,
        }
    )
    return {
        "ok": True,
        "draft_id": draft_id,
        "scheduled_time": when.isoformat(),
        "schedule_name": name,
    }


def _parse_iso(s: str) -> datetime:
    if not s:
        raise ValueError("required")
    # Accept trailing Z
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f"not an ISO 8601 timestamp: {s}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _schedule_name(draft_id: str) -> str:
    # EventBridge Scheduler names are [A-Za-z0-9_-]+ up to 64 chars
    safe = "".join(c if (c.isalnum() or c in "_-") else "-" for c in draft_id)
    return f"publish-{safe}"[:64]


@lru_cache(maxsize=1)
def _scheduler():
    return boto3.client("scheduler")


def _ns(draft: dict):
    from types import SimpleNamespace

    return SimpleNamespace(**draft)
