from sharedModules import approval, dynamo


def discard_draft(draft_id: str) -> dict:
    """Mark a draft discarded. Reversible by `admin.py` only.

    The draft row stays (status = 'discarded') so the audit trail is
    intact; approval records are deleted so no stale approval survives.
    """
    draft = dynamo.read_draft(draft_id)
    if draft is None:
        return {"ok": False, "error": f"draft '{draft_id}' not found"}
    if draft.get("status") == "published":
        return {
            "ok": False,
            "error": f"draft '{draft_id}' is already published; discard does not retract",
        }
    dynamo.discard_draft(draft_id)
    approval.invalidate_approval(draft_id)
    dynamo.append_audit(
        {
            "account": draft["account"],
            "kind": "reject",
            "actor_identity": draft.get("created_by", "mcp_client"),
            "draft_id": draft_id,
            "result": "discarded",
        }
    )
    return {"ok": True, "draft_id": draft_id, "status": "discarded"}
