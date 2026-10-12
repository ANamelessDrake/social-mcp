from sharedModules import dynamo, registry
from sharedModules.errors import AccountError


def list_drafts(account: str = "", status: str = "", limit: int = 20) -> dict:
    """List drafts, newest first.

    - `account`: filter to one alias; omit to sweep every configured
      account (same semantics as `list_posts`).
    - `status`: optional filter; typically 'drafted', 'approved',
      'scheduled', 'published', 'failed', 'discarded'.
    - `limit`: per-account cap, default 20, max 100.

    Each row is a short preview, not the full draft; call `get_draft`
    for the complete body.
    """
    try:
        if account:
            aliases = [registry.resolve_alias(account)]
        else:
            aliases = sorted(registry.configured_accounts())
    except AccountError as e:
        return {"ok": False, "error": str(e)}

    rows: list[dict] = []
    per = max(1, min(int(limit or 20), 100))
    for alias in aliases:
        for d in dynamo.list_drafts(alias, status=status, limit=per):
            rows.append(
                {
                    "draft_id": d.get("draft_id", ""),
                    "account": d.get("account", ""),
                    "kind": d.get("kind", ""),
                    "status": d.get("status", ""),
                    "created_at": d.get("created_at", ""),
                    "created_by": d.get("created_by", ""),
                    "text_preview": _preview_text(d.get("text", "")),
                    "media_count": len(d.get("media", []) or []),
                    "schedule": d.get("schedule", ""),
                }
            )
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return {"ok": True, "drafts": rows}


def _preview_text(text: str) -> str:
    text = text or ""
    return text if len(text) <= 120 else text[:119] + "…"
