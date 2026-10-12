from sharedModules import approval, dynamo, policy, registry
from sharedModules.models import MediaRef

from . import draft_post as dp


def update_draft(
    draft_id: str,
    text: str = "",
    link: str = "",
    media: str = "",
    scheduled_time: str = "",
) -> dict:
    """Edit a draft before it has been published.

    Any field left empty is kept as-is; pass the string you want it to
    become. `media` is comma-separated https URLs (same shape as
    `draft_post`); passing `media=''` keeps the current media.

    **Any edit silently voids an existing approval record** (DESIGN.md
    section 7.2). The next `publish_draft` will fail with a specific
    error naming the drift, and the operator will have to re-run
    `admin.py approve`. This is the binding promise: an approval binds
    to the exact text and media.
    """
    existing = dynamo.read_draft(draft_id)
    if existing is None:
        return {"ok": False, "error": f"draft '{draft_id}' not found"}
    if existing.get("status") in ("published", "discarded"):
        return {
            "ok": False,
            "error": f"draft '{draft_id}' is {existing['status']}; cannot edit",
        }

    patch: dict = {}
    if text:
        patch["text"] = text
    if link:
        patch["link"] = link
    if scheduled_time:
        patch["schedule"] = scheduled_time
    if media:
        patch["media"] = [
            {"source": m.source, "key": m.key, "mime": m.mime, "etag": m.etag}
            for m in dp._parse_media(media)
        ]
    if not patch:
        return {"ok": True, "draft_id": draft_id, "changed": False}

    # Re-run policy on the merged shape before persisting
    account = existing["account"]
    merged = {**existing, **patch}
    entry = registry.configured_accounts().get(account)
    if entry is not None:
        try:
            policy.enforce(_ns(merged), entry.get("policy") or {})
        except policy.PolicyError as e:
            return {"ok": False, "error": f"policy: {e}"}

    # Any content edit invalidates the approval (DESIGN.md section 7.2)
    approval.invalidate_approval(draft_id)
    patch["status"] = "drafted"
    patch["policy_warnings"] = policy.warnings_from(
        policy.validate(_ns(merged), (entry or {}).get("policy") or {})
    )

    updated = dynamo.update_draft_fields(draft_id, patch)
    dynamo.append_audit(
        {
            "account": account,
            "kind": "draft",
            "actor_identity": existing.get("created_by", "mcp_client"),
            "draft_id": draft_id,
            "result": "updated",
        }
    )
    return {
        "ok": True,
        "draft_id": draft_id,
        "changed": True,
        "draft": {k: v for k, v in updated.items() if k != "media"},
        "approval": "voided",
    }


def _ns(draft: dict):
    from types import SimpleNamespace

    return SimpleNamespace(**draft)


# Reference the dataclass so ruff recognizes the import path at module load
_ = MediaRef
