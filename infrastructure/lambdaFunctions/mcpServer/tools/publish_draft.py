"""Publish an approved draft via the Publisher Lambda.

The MCP server never calls Meta's publish endpoints directly (DESIGN.md
section 2); it re-checks the approval + policy locally for a fast fail
when something is wrong, then synchronously invokes the Publisher
Lambda, which re-checks and does the Meta POST.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache

import boto3
from sharedModules import approval as approval_mod
from sharedModules import dynamo, policy, registry
from sharedModules.errors import AccountError

from ._common import PUBLISH_SCOPE


def publish_draft(draft_id: str) -> dict:
    """Publish an approved draft.

    - The account's `publish_mode` controls who created the approval
      record (operator in 'manual', server implicitly in 'auto'); this
      tool only checks that the record exists AND still matches the
      current draft.
    - The Publisher Lambda re-runs the same check immediately before
      the Meta POST, so a race between approve-and-edit cannot slip
      past the gate.

    Returns the published `post_id` + permalink on success.
    """
    draft = dynamo.read_draft(draft_id)
    if draft is None:
        return {"ok": False, "error": f"draft '{draft_id}' not found"}
    if draft.get("status") == "discarded":
        return {"ok": False, "error": f"draft '{draft_id}' is discarded"}
    if draft.get("status") == "published":
        post_id = draft.get("post_id", "")
        return {
            "ok": False,
            "error": f"draft '{draft_id}' is already published (post_id={post_id})",
        }

    # Local approval re-check for a fast fail (the Publisher also checks).
    try:
        approval_mod.verify_approval(draft_id, scope=PUBLISH_SCOPE)
    except approval_mod.ApprovalError as e:
        return {"ok": False, "error": str(e)}
    except AccountError as e:
        return {"ok": False, "error": str(e)}

    # Local policy re-run (the Publisher also runs this).
    entry = registry.configured_accounts().get(draft["account"])
    if entry is None:
        return {"ok": False, "error": f"account '{draft['account']}' is no longer configured"}
    try:
        policy.enforce(_ns(draft), entry.get("policy") or {})
    except policy.PolicyError as e:
        return {"ok": False, "error": f"policy: {e}"}

    arn = os.environ.get("PUBLISHER_FUNCTION_ARN")
    if not arn:
        return {"ok": False, "error": "PUBLISHER_FUNCTION_ARN is unset (M2 deploy incomplete)"}

    resp = _lambda_client().invoke(
        FunctionName=arn,
        InvocationType="RequestResponse",
        Payload=json.dumps({"draft_id": draft_id, "scope": PUBLISH_SCOPE}).encode(),
    )
    body = resp.get("Payload").read().decode() if resp.get("Payload") else "{}"
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"ok": False, "error": "publisher returned invalid JSON"}


@lru_cache(maxsize=1)
def _lambda_client():
    return boto3.client("lambda")


def _ns(draft: dict):
    from types import SimpleNamespace

    return SimpleNamespace(**draft)
