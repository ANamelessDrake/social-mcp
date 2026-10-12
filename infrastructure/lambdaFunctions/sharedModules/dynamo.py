"""Core table access: accounts, drafts, approvals, audit, kill switches.

One table (`{env}-socialmcp-core`) holds every item family (DESIGN.md
section 10):

- `ACCOUNT#<alias>`, SK=`META` -> registry + encrypted tokens.
- `DRAFT#<draft_id>`, SK=`META` -> drafts; GSI1 for list_drafts.
- `APPROVAL#<draft_id>`, SK=`META` -> approval records.
- `AUDIT#<account>`, SK=`TS#<iso8601>#<ulid>` -> append-only audit log.
- `KILL#`, SK=`global` -> global publishing kill switch (M2).

Account items are written by `scripts/admin.py connect`; drafts are written
by the MCP server; approvals by `admin.py approve` or implicitly by
`draft_post` on auto-mode accounts; audit rows are written by the Publisher
and the admin CLI.
"""

import os
from datetime import UTC, datetime
from functools import lru_cache

import boto3


@lru_cache(maxsize=1)
def core_table():
    return boto3.resource("dynamodb").Table(os.environ["CORE_TABLE"])


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _ulid_like() -> str:
    """Short, lexicographic, UTC-ordered id; good enough for audit SKs."""
    import secrets as pysecrets

    return f"{int(datetime.now(UTC).timestamp() * 1000):013d}-{pysecrets.token_hex(4)}"


def get_account(alias: str) -> dict | None:
    resp = core_table().get_item(Key={"PK": f"ACCOUNT#{alias}", "SK": "META"})
    return resp.get("Item")


def put_account(alias: str, item: dict) -> None:
    """Full write of an ACCOUNT item; called by `admin.py connect`."""
    core_table().put_item(
        Item={
            "PK": f"ACCOUNT#{alias}",
            "SK": "META",
            **item,
        }
    )


def update_tokens(alias: str, page_token_ct: bytes, user_token_ct: bytes | None = None) -> None:
    """Persist rotated tokens immediately (DESIGN.md 3.4)."""
    expr = "SET page_token_ct = :p, token_minted_at = :now, last_used_at = :now"
    values = {":p": page_token_ct, ":now": _now()}
    if user_token_ct is not None:
        expr += ", user_token_ct = :u"
        values[":u"] = user_token_ct
    core_table().update_item(
        Key={"PK": f"ACCOUNT#{alias}", "SK": "META"},
        UpdateExpression=expr,
        ExpressionAttributeValues=values,
    )


def touch_last_used(alias: str) -> None:
    core_table().update_item(
        Key={"PK": f"ACCOUNT#{alias}", "SK": "META"},
        UpdateExpression="SET last_used_at = :now",
        ExpressionAttributeValues={":now": _now()},
    )


def mark_needs_reconnect(alias: str) -> None:
    """The grant is dead (revoked/expired); no retry will fix it (DESIGN.md 3.4)."""
    core_table().update_item(
        Key={"PK": f"ACCOUNT#{alias}", "SK": "META"},
        UpdateExpression="SET #s = :s",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "needs_reconnect"},
    )


# -------------------- drafts (DESIGN.md section 7) --------------------


def write_draft(draft: dict) -> None:
    """Full write of a DRAFT item (initial insert).

    Shape matches the Draft dataclass; `media` is persisted as a list of
    dicts (dataclasses are not DynamoDB-friendly).
    """
    account = draft["account"]
    draft_id = draft["draft_id"]
    created_at = draft.get("created_at") or _now()
    item = {
        "PK": f"DRAFT#{draft_id}",
        "SK": "META",
        "GSI1PK": f"DRAFTS#{account}",
        "GSI1SK": f"CREATED#{created_at}#{draft.get('status', 'drafted')}",
        "created_at": created_at,
        **draft,
    }
    core_table().put_item(Item=item)


def read_draft(draft_id: str) -> dict | None:
    """Load a draft and return a plain dict (status, text, media[], ...)."""
    resp = core_table().get_item(Key={"PK": f"DRAFT#{draft_id}", "SK": "META"})
    item = resp.get("Item")
    if item is None:
        return None
    item = {k: v for k, v in item.items() if k not in {"PK", "SK", "GSI1PK", "GSI1SK"}}
    return item


def update_draft_fields(draft_id: str, patch: dict) -> dict:
    """Partial update to text / link / media / schedule / status / warnings.

    Rewrites the GSI1 sort key when `status` changes so list_drafts filtering
    stays accurate.
    """
    existing = read_draft(draft_id)
    if existing is None:
        return {}
    merged = {**existing, **patch}
    created_at = merged.get("created_at") or _now()
    status = merged.get("status", "drafted")
    core_table().put_item(
        Item={
            "PK": f"DRAFT#{draft_id}",
            "SK": "META",
            "GSI1PK": f"DRAFTS#{merged['account']}",
            "GSI1SK": f"CREATED#{created_at}#{status}",
            **merged,
        }
    )
    return merged


def discard_draft(draft_id: str) -> None:
    """Soft-delete via status change; the audit row references the id."""
    existing = read_draft(draft_id)
    if existing is None:
        return
    update_draft_fields(draft_id, {"status": "discarded"})


def list_drafts(account: str, status: str = "", limit: int = 20) -> list[dict]:
    """List drafts for an account, newest first, optionally filtered by status."""
    kwargs = {
        "IndexName": "GSI1",
        "KeyConditionExpression": "GSI1PK = :pk",
        "ExpressionAttributeValues": {":pk": f"DRAFTS#{account}"},
        "ScanIndexForward": False,  # newest first
        "Limit": max(1, min(int(limit or 20), 100)),
    }
    resp = core_table().query(**kwargs)
    items = resp.get("Items", [])
    rows = [
        {k: v for k, v in item.items() if k not in {"PK", "SK", "GSI1PK", "GSI1SK"}}
        for item in items
    ]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    return rows


# -------------------- approvals (DESIGN.md section 7.2) --------------------


def write_approval(*, draft_id: str, content_hash: str, approved_by: str, scope: str) -> None:
    core_table().put_item(
        Item={
            "PK": f"APPROVAL#{draft_id}",
            "SK": "META",
            "draft_id": draft_id,
            "content_hash": content_hash,
            "approved_by": approved_by,
            "approved_at": _now(),
            "scope": scope,
        }
    )


def read_approval(draft_id: str) -> dict | None:
    resp = core_table().get_item(Key={"PK": f"APPROVAL#{draft_id}", "SK": "META"})
    item = resp.get("Item")
    if item is None:
        return None
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def delete_approval(draft_id: str) -> None:
    core_table().delete_item(Key={"PK": f"APPROVAL#{draft_id}", "SK": "META"})


# -------------------- audit (DESIGN.md section 10) --------------------


def append_audit(row: dict) -> None:
    """Append-only; one row per state transition."""
    account = row.get("account", "unknown")
    ts = row.get("created_at") or _now()
    sk = f"TS#{ts}#{_ulid_like()}"
    core_table().put_item(
        Item={
            "PK": f"AUDIT#{account}",
            "SK": sk,
            "created_at": ts,
            **row,
        }
    )


def list_audit(account: str = "", since: str = "", limit: int = 50) -> list[dict]:
    """Recent audit rows; empty account scans across accounts (operator CLI)."""
    rows: list[dict] = []
    limit = max(1, min(int(limit or 50), 200))
    if account:
        kwargs = {
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": f"AUDIT#{account}"},
            "ScanIndexForward": False,
            "Limit": limit,
        }
        if since:
            kwargs["KeyConditionExpression"] += " AND SK >= :sk"
            kwargs["ExpressionAttributeValues"][":sk"] = f"TS#{since}"
        resp = core_table().query(**kwargs)
        rows = resp.get("Items", [])
    else:
        # scan is cheap at personal scale and only called from admin.py
        resp = core_table().scan(
            FilterExpression="begins_with(PK, :p)",
            ExpressionAttributeValues={":p": "AUDIT#"},
            Limit=limit * 4,
        )
        rows = sorted(
            resp.get("Items", []), key=lambda r: r.get("created_at", ""), reverse=True
        )[:limit]
    return [
        {k: v for k, v in row.items() if k not in {"PK", "SK"}} for row in rows
    ]


# -------------------- global kill switch (DESIGN.md section 7.6) --------------------


def publishing_disabled() -> bool:
    """Read the global kill switch, overriding any config-level flag.

    `admin.py disable-publishing` writes a KILL#global item; the Publisher
    consults this before every Meta call. On DynamoDB failure we fail closed
    (treat as disabled), since a kill switch that silently stops working is
    worse than one that is briefly over-active.
    """
    try:
        resp = core_table().get_item(Key={"PK": "KILL#", "SK": "global"})
    except Exception:  # noqa: BLE001 - fail closed
        return True
    item = resp.get("Item")
    if item is None:
        return False
    return bool(item.get("disabled", False))


def set_publishing_disabled(disabled: bool, actor: str = "operator") -> None:
    core_table().put_item(
        Item={
            "PK": "KILL#",
            "SK": "global",
            "disabled": bool(disabled),
            "set_by": actor,
            "set_at": _now(),
        }
    )
