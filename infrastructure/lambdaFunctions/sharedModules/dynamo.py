"""Core table access: accounts, drafts, approvals, staged changes, audit.

One table (`{env}-socialmcp-core`) holds every item family (DESIGN.md
section 10 + 7):

- `ACCOUNT#<alias>`, SK=`META` -> registry + encrypted tokens.
- `DRAFT#<draft_id>`, SK=`META` -> drafts; GSI1 for list_drafts.
- `APPROVAL#<key>`, SK=`META` -> approval records. `<key>` is a draft_id
  for `publish`/`schedule` scopes; for M3 scopes it is the targeted id
  prefixed by the scope family: `edit:<post_id>`, `delete:<post_id>`,
  `reply:<comment_id>`, or `hide:<account>` for the standing per-account
  hide approval.
- `EDIT#<post_id>`, `DELETE#<post_id>`, `REPLY#<comment_id>`,
  `HIDE_REQUEST#<account>`#`<comment_id>`,
  `PROFILE_EDIT#<account>` (M9), SK=`META` -> staged changes
  the MCP server writes; the Publisher reads these and the matching
  approval record, so the operator-approved payload is what reaches
  Meta, not whatever the agent re-sent after approval.
- `HIDE#<account>`, SK=`META` -> standing per-account hide approval
  (DESIGN.md section 7.1); reversible from admin.py.
- `AUDIT#<account>`, SK=`TS#<iso8601>#<ulid>` -> append-only audit log.
- `KILL#`, SK=`global` -> global publishing kill switch (M2).
- `CONNECT_STATE#<state>`, SK=`META` -> short-lived (15 minute TTL) record
  the admin CLI writes before opening the browser so the hosted OAuth
  callback can match the returning code to the right alias and scope
  tier (DESIGN.md section 3 / M6). Single-use: a status transition from
  `pending` to `completed` or `error` is final; the TTL cleans the row.

Account items are written by `scripts/admin.py connect`; drafts and
staged changes are written by the MCP server; approvals by `admin.py
approve*` or implicitly by `draft_post` on auto-mode accounts; audit
rows are written by the Publisher and the admin CLI.
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
    """Draft-scoped approval (publish / schedule). PK=`APPROVAL#<draft_id>`."""
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


def write_approval_record(
    *, key: str, content_hash: str, approved_by: str, scope: str, payload: dict | None = None
) -> None:
    """Non-draft approval (M3 scopes: edit / delete / reply / hide).

    `key` is `edit:<post_id>` / `delete:<post_id>` / `reply:<comment_id>`
    / `hide:<account>`. `payload` is optional context (e.g. the delete
    reason) persisted alongside the hash, for the admin CLI to display.
    """
    item = {
        "PK": f"APPROVAL#{key}",
        "SK": "META",
        "approval_key": key,
        "content_hash": content_hash,
        "approved_by": approved_by,
        "approved_at": _now(),
        "scope": scope,
    }
    if payload:
        item["payload"] = payload
    core_table().put_item(Item=item)


def read_approval_record(key: str) -> dict | None:
    resp = core_table().get_item(Key={"PK": f"APPROVAL#{key}", "SK": "META"})
    item = resp.get("Item")
    if item is None:
        return None
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def delete_approval_record(key: str) -> None:
    core_table().delete_item(Key={"PK": f"APPROVAL#{key}", "SK": "META"})


# -------------------- M3 staged changes (DESIGN.md section 7.1) --------------------
#
# edit / delete / reply / hide each have a staged change written by the
# tool and read by the Publisher. The staged content is the thing the
# approval record hashes, so what the operator approved is what reaches
# Meta, not whatever the agent re-sent after approval.


def write_staged_edit(post_id: str, account: str, text: str, created_by: str) -> dict:
    item = {
        "PK": f"EDIT#{post_id}",
        "SK": "META",
        "post_id": post_id,
        "account": account,
        "text": text or "",
        "created_by": created_by,
        "created_at": _now(),
    }
    core_table().put_item(Item=item)
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def read_staged_edit(post_id: str) -> dict | None:
    resp = core_table().get_item(Key={"PK": f"EDIT#{post_id}", "SK": "META"})
    item = resp.get("Item")
    if item is None:
        return None
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def delete_staged_edit(post_id: str) -> None:
    core_table().delete_item(Key={"PK": f"EDIT#{post_id}", "SK": "META"})


def write_staged_delete(post_id: str, account: str, reason: str, created_by: str) -> dict:
    item = {
        "PK": f"DELETE#{post_id}",
        "SK": "META",
        "post_id": post_id,
        "account": account,
        "reason": reason or "",
        "created_by": created_by,
        "created_at": _now(),
    }
    core_table().put_item(Item=item)
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def read_staged_delete(post_id: str) -> dict | None:
    resp = core_table().get_item(Key={"PK": f"DELETE#{post_id}", "SK": "META"})
    item = resp.get("Item")
    if item is None:
        return None
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def delete_staged_delete(post_id: str) -> None:
    core_table().delete_item(Key={"PK": f"DELETE#{post_id}", "SK": "META"})


def write_staged_reply(comment_id: str, account: str, text: str, created_by: str) -> dict:
    item = {
        "PK": f"REPLY#{comment_id}",
        "SK": "META",
        "comment_id": comment_id,
        "account": account,
        "text": text or "",
        "created_by": created_by,
        "created_at": _now(),
    }
    core_table().put_item(Item=item)
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def read_staged_reply(comment_id: str) -> dict | None:
    resp = core_table().get_item(Key={"PK": f"REPLY#{comment_id}", "SK": "META"})
    item = resp.get("Item")
    if item is None:
        return None
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def delete_staged_reply(comment_id: str) -> None:
    core_table().delete_item(Key={"PK": f"REPLY#{comment_id}", "SK": "META"})


def write_hide_request(account: str, comment_id: str, created_by: str) -> dict:
    item = {
        "PK": f"HIDE_REQUEST#{account}",
        "SK": comment_id,
        "account": account,
        "comment_id": comment_id,
        "created_by": created_by,
        "created_at": _now(),
    }
    core_table().put_item(Item=item)
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def list_hide_requests(account: str) -> list[dict]:
    resp = core_table().query(
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": f"HIDE_REQUEST#{account}"},
    )
    return [{k: v for k, v in item.items() if k not in {"PK"}} for item in resp.get("Items", [])]


def delete_hide_request(account: str, comment_id: str) -> None:
    core_table().delete_item(Key={"PK": f"HIDE_REQUEST#{account}", "SK": comment_id})


# -------------------- M9 staged profile edit --------------------
#
# PROFILE_EDIT#<alias> items hold the operator-approved profile patch;
# the Publisher reads this at publish time so the applied edit is what
# was approved, not what the agent re-sent.


def write_staged_profile_edit(account: str, patch: dict, created_by: str) -> dict:
    """Write (or overwrite) a staged profile edit for one account."""
    row = {k: v for k, v in (patch or {}).items() if v not in ("", None, [])}
    item = {
        "PK": f"PROFILE_EDIT#{account}",
        "SK": "META",
        "account": account,
        "patch": row,
        "created_by": created_by,
        "created_at": _now(),
    }
    core_table().put_item(Item=item)
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def read_staged_profile_edit(account: str) -> dict | None:
    resp = core_table().get_item(Key={"PK": f"PROFILE_EDIT#{account}", "SK": "META"})
    item = resp.get("Item")
    if item is None:
        return None
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def delete_staged_profile_edit(account: str) -> None:
    core_table().delete_item(Key={"PK": f"PROFILE_EDIT#{account}", "SK": "META"})


def write_hide_standing(account: str, approved_by: str) -> None:
    """Standing per-account hide approval (reversible from admin.py)."""
    core_table().put_item(
        Item={
            "PK": f"HIDE#{account}",
            "SK": "META",
            "account": account,
            "approved_by": approved_by,
            "approved_at": _now(),
        }
    )


def read_hide_standing(account: str) -> dict | None:
    resp = core_table().get_item(Key={"PK": f"HIDE#{account}", "SK": "META"})
    item = resp.get("Item")
    if item is None:
        return None
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def delete_hide_standing(account: str) -> None:
    core_table().delete_item(Key={"PK": f"HIDE#{account}", "SK": "META"})


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
        rows = sorted(resp.get("Items", []), key=lambda r: r.get("created_at", ""), reverse=True)[
            :limit
        ]
    return [{k: v for k, v in row.items() if k not in {"PK", "SK"}} for row in rows]


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


# -------------------- M6 hosted OAuth connect state --------------------

# One record per outstanding `admin.py connect --remote` invocation. The
# admin CLI writes it (status=pending), then prints a callback URL carrying
# ?state=<random> and polls this record every 2 seconds; the hosted
# callback (oauthCallback/app.py) updates it to `completed` or `error`
# once Meta returns.

CONNECT_STATE_TTL_SECONDS = 900  # 15 minutes


def write_connect_state(
    state: str,
    *,
    alias_prefix: str,
    scopes_tier: str,
    operator_identity: str,
    page_id: str = "",
    ig_alias: str = "",
) -> dict:
    import time

    expires_at = int(time.time()) + CONNECT_STATE_TTL_SECONDS
    item = {
        "PK": f"CONNECT_STATE#{state}",
        "SK": "META",
        "status": "pending",
        "alias_prefix": alias_prefix,
        "scopes_tier": scopes_tier,
        "operator_identity": operator_identity,
        "page_id": page_id,
        "ig_alias": ig_alias,
        "requested_at": _now(),
        "message": "",
        "aliases": [],
        "ttl": expires_at,
    }
    core_table().put_item(Item=item)
    return item


def read_connect_state(state: str) -> dict | None:
    resp = core_table().get_item(Key={"PK": f"CONNECT_STATE#{state}", "SK": "META"})
    return resp.get("Item")


def update_connect_state(
    state: str,
    *,
    status: str,
    message: str = "",
    aliases: list[str] | None = None,
) -> None:
    """Transition a state record. Guards against overwriting a terminal row.

    The callback and the admin CLI's error path both call this, but a row
    already in `completed` or `error` is final: the second caller's write
    is rejected by a condition expression. That stops a replayed Meta
    callback from re-overwriting a completed record with an error, and
    stops a late CLI timeout from clobbering a succeeded record.
    """
    expr_values = {
        ":s": status,
        ":m": message,
        ":a": list(aliases) if aliases is not None else [],
        ":now": _now(),
        ":pending": "pending",
    }
    try:
        core_table().update_item(
            Key={"PK": f"CONNECT_STATE#{state}", "SK": "META"},
            UpdateExpression=("SET #s = :s, message = :m, aliases = :a, completed_at = :now"),
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=expr_values,
        )
    except Exception as exc:  # noqa: BLE001 - ConditionalCheckFailed is expected
        name = type(exc).__name__
        if "ConditionalCheckFailed" not in name:
            raise


# -------------------- M7 comment event store --------------------
#
# webhookIngest writes one COMMENT_EVENT#<alias> item per comment, SK
# `TS#<created_time>#<comment_id>`, so the alias partition sorts
# chronologically for check_comments and the composite key dedupes
# Meta's retry storms. Per-identity read cursors live on CURSOR#<identity>
# rows (SK=`SCOPE#<account>#<post_id>`), matching slack-mcp's per-scope
# cursor pattern so a filtered check never consumes an unfiltered one.


def comments_after(
    account: str, after_sk: str = "", limit: int = 20, post_id: str = ""
) -> list[dict]:
    """Return stored comment events newer than `after_sk`, oldest first.

    `after_sk` is the SK (`TS#<time>#<comment_id>`) of the last comment
    the caller consumed; a post_id filter is applied after the query
    because the partition is per-account, not per-post."""
    kwargs = {
        "KeyConditionExpression": "PK = :pk",
        "ExpressionAttributeValues": {":pk": f"COMMENT_EVENT#{account}"},
        "ScanIndexForward": True,
        "Limit": max(1, min(int(limit or 20), 100)),
    }
    if after_sk:
        kwargs["KeyConditionExpression"] += " AND SK > :sk"
        kwargs["ExpressionAttributeValues"][":sk"] = after_sk
    resp = core_table().query(**kwargs)
    rows = [
        {k: v for k, v in item.items() if k not in {"PK", "SK"}} | {"_sk": item["SK"]}
        for item in resp.get("Items", [])
    ]
    if post_id:
        rows = [r for r in rows if r.get("post_id") == post_id]
    return rows


def list_comment_accounts() -> list[str]:
    """Aliases known to have received at least one comment event.

    Powers `check_comments` with an empty `account`: a scan hop that
    costs nothing at personal scale (and only called by the admin / tool
    code paths, not the per-webhook hot path)."""
    try:
        resp = core_table().scan(
            FilterExpression="begins_with(PK, :p)",
            ExpressionAttributeValues={":p": "COMMENT_EVENT#"},
            ProjectionExpression="PK",
            Limit=1000,
        )
    except Exception:  # noqa: BLE001
        return []
    aliases = {item["PK"].split("#", 1)[1] for item in resp.get("Items", [])}
    return sorted(aliases)


def get_comment_cursor(identity: str, scope: str) -> str:
    resp = core_table().get_item(Key={"PK": f"CURSOR#{identity}", "SK": f"COMMENT#{scope}"})
    item = resp.get("Item")
    if item is None:
        return ""
    return item.get("cursor", "")


def set_comment_cursor(identity: str, scope: str, cursor: str) -> None:
    core_table().put_item(
        Item={
            "PK": f"CURSOR#{identity}",
            "SK": f"COMMENT#{scope}",
            "cursor": cursor,
            "updated_at": _now(),
        }
    )


def comment_cursor_scope(account: str, post_id: str) -> str:
    """A filtered check keeps its own cursor so skipping a post never
    consumes the comment for a wider check later."""
    return f"{account}#{post_id or '*'}"
