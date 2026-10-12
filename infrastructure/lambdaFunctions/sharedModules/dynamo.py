"""Core table access: account registry and token state (DESIGN.md section 10).

Account items are written by `scripts/admin.py connect`; the server reads
them per call and writes back only token-state changes (rotation,
revocation, last_used_at). Draft, approval, and audit item families land in
M2 and reuse the same table.
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
