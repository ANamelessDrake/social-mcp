from sharedModules import approval as approval_mod
from sharedModules import dynamo


def get_draft(draft_id: str) -> dict:
    """Fetch one draft with its current approval state.

    Returns the full draft dict (account, kind, text, link, media,
    schedule, status, policy_warnings, created_at, created_by) plus
    `approval`: 'approved' when a matching approval record exists,
    'pending' otherwise. The `content_hash` is included so an operator
    can confirm by eye that the approved content is the content they
    intend to approve of.
    """
    draft = dynamo.read_draft(draft_id)
    if draft is None:
        return {"ok": False, "error": f"draft '{draft_id}' not found"}
    rec = dynamo.read_approval(draft_id)
    envelope = {"ok": True, "draft": draft}
    if rec is None:
        envelope["approval"] = {"state": "pending"}
    else:
        current = approval_mod.hash_draft(_as_ns(draft))
        envelope["approval"] = {
            "state": "approved" if rec["content_hash"] == current else "stale",
            "approved_by": rec.get("approved_by", ""),
            "approved_at": rec.get("approved_at", ""),
            "scope": rec.get("scope", ""),
            "content_hash": rec.get("content_hash", ""),
            "current_hash": current,
        }
    return envelope


def _as_ns(draft: dict):
    from types import SimpleNamespace

    return SimpleNamespace(**draft)
