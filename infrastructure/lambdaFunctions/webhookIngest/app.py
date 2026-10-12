"""Meta Webhook ingest (DESIGN.md section 15, M7).

Receives comment events for Facebook Pages and Instagram professional
accounts. The DynamoDB write is the durability guarantee; AppSync Events
publish is best-effort fan-out to live `wait_for_comments` sessions.
Comments the server has not seen before get a one-shot full-body fetch
via the stored Page token so the event carries the text, not just an id.

Self-contained Starlette app on a Function URL behind the Lambda Web
Adapter, mirroring mcpServer and oauthCallback.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from functools import lru_cache

import boto3
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

log = logging.getLogger("webhookIngest")
logging.basicConfig(level=logging.INFO)


_CORE_TABLE = None
_META_APP_SECRET_CACHE: dict[str, str] = {}
_VERIFY_TOKEN_CACHE: dict[str, str] = {}


def _core_table():
    global _CORE_TABLE
    if _CORE_TABLE is None:
        _CORE_TABLE = boto3.resource("dynamodb").Table(os.environ["CORE_TABLE"])
    return _CORE_TABLE


@lru_cache(maxsize=1)
def _secrets_client():
    return boto3.client("secretsmanager")


def _verify_token() -> str:
    if "value" not in _VERIFY_TOKEN_CACHE:
        name = os.environ["WEBHOOK_VERIFY_TOKEN_SECRET"]
        _VERIFY_TOKEN_CACHE["value"] = _secrets_client().get_secret_value(
            SecretId=name
        )["SecretString"]
    return _VERIFY_TOKEN_CACHE["value"]


def _app_secret() -> str:
    if "value" not in _META_APP_SECRET_CACHE:
        raw = _secrets_client().get_secret_value(
            SecretId=os.environ["META_APP_SECRET"]
        )["SecretString"]
        try:
            parsed = json.loads(raw)
            _META_APP_SECRET_CACHE["value"] = parsed["app_secret"]
        except (ValueError, KeyError):
            # Secret still carries the deploy-time placeholder; refuse rather
            # than compare against the empty string.
            _META_APP_SECRET_CACHE["value"] = ""
    return _META_APP_SECRET_CACHE["value"]


def _valid_signature(headers: dict, body: bytes) -> bool:
    """Meta signs with HMAC-SHA256 using the app secret as the key.

    Header value is `sha256=<hex>`. Constant-time compare, and bail
    immediately if the secret is unset (placeholder deploy) so an
    unconfigured app does not accept arbitrary posts.
    """
    sig = headers.get("x-hub-signature-256", "")
    if not sig.startswith("sha256="):
        return False
    secret = _app_secret()
    if not secret:
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, sig)


def _lookup_alias_by_account_id(account_id: str) -> dict | None:
    """Scan the ACCOUNT roster for a provider id match.

    The MCP server pins each account to its provider-native id at
    `admin.py connect` time (`account_id` on the ACCOUNT item). The ingest
    Lambda sees a webhook `entry.id` (a Page id or an IG user id) and
    needs the alias to route the stored event. The roster is small so a
    scan is cheap at personal scale; `admin.py` touches it at human
    speed. If a result is paginated we take the first page: a 100-row
    deployment is far beyond the design target.
    """
    try:
        resp = _core_table().scan(
            FilterExpression="begins_with(PK, :p) AND account_id = :a",
            ExpressionAttributeValues={":p": "ACCOUNT#", ":a": account_id},
            Limit=50,
        )
    except Exception:  # noqa: BLE001
        log.exception("account roster scan failed")
        return None
    items = resp.get("Items") or []
    return items[0] if items else None


def _fetch_comment_body(alias: str, provider: str, comment_id: str) -> dict:
    """One-shot GET /{comment_id}?fields=... with the stored Page token.

    Imported lazily so the ingest cold start does not need to drag in the
    provider HTTP client when a non-comment webhook arrives.
    """
    from sharedModules.providers import facebook, instagram

    if provider == "instagram":
        return instagram.InstagramProvider(alias).fetch_comment(comment_id)
    return facebook.FacebookProvider(alias).fetch_comment(comment_id)


def _publish_event(channel: str, event: dict) -> None:
    """Best-effort publish to AppSync Events for live subscribers.

    Runs only after the durable DynamoDB write; failure here is logged
    and swallowed because offline delivery via check_comments is
    unaffected.
    """
    endpoint = os.environ.get("EVENTS_HTTP_ENDPOINT", "")
    if not endpoint:
        return
    api_key = os.environ.get("EVENTS_API_KEY", "")
    if not api_key:
        return
    try:
        httpx.post(
            endpoint,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
            },
            json={"channel": channel, "events": [json.dumps(event)]},
            timeout=3.0,
        )
    except Exception:  # noqa: BLE001
        log.warning("events publish failed on %s", channel, exc_info=True)


def _comment_item(
    alias: str, provider: str, post_id: str, raw: dict, body: dict
) -> dict:
    """Build the stored shape; `raw` is the inner webhook value, `body` the
    one-shot comment fetch (or an empty dict if the fetch failed).
    """
    comment_id = raw.get("comment_id") or body.get("comment_id") or ""
    event_time = raw.get("created_time") or body.get("created_time") or ""
    ts = _event_sk(event_time)
    text = body.get("text") or raw.get("message", "")
    author = body.get("author", "")
    is_hidden = bool(body.get("is_hidden", False))
    parent_id = raw.get("parent_id") or body.get("thread_id") or comment_id
    return {
        "PK": f"COMMENT_EVENT#{alias}",
        "SK": f"TS#{ts}#{comment_id}",
        "account": alias,
        "provider": provider,
        "post_id": post_id,
        "comment_id": comment_id,
        "thread_id": parent_id,
        "created_time": event_time,
        "author": author,
        "text": text,
        "is_hidden": is_hidden,
        "body_fetched": bool(body),
        "ingested_at": _now(),
    }


def _event_sk(created_time: str) -> str:
    """Lexicographic sort key. Falls back to ingest-time clock if Meta
    didn't carry a `created_time` on the webhook value (rare for comments
    but defensive)."""
    return created_time or _now()


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def _handle_change(entry_id: str, change: dict) -> int:
    """Returns the count of events stored and published from this change.

    One webhook entry can carry multiple comment events; each is deduped
    on (account, comment_id, created_time) and the body is fetched once
    on first sight."""
    value = change.get("value") or {}
    account = _lookup_alias_by_account_id(entry_id)
    if account is None:
        log.info("entry %s has no matching ACCOUNT; dropping", entry_id)
        return 0
    alias = account["PK"].split("#", 1)[1]
    provider = account["provider"]
    post_id = (value.get("post_id") or (value.get("media") or {}).get("id") or "")
    comment_id = value.get("comment_id") or ""
    if not comment_id:
        return 0

    try:
        body = _fetch_comment_body(alias, provider, comment_id)
    except Exception:  # noqa: BLE001
        log.warning(
            "fetch_comment failed for %s/%s; storing event with placeholder body",
            alias,
            comment_id,
            exc_info=True,
        )
        body = {}

    item = _comment_item(alias, provider, post_id, value, body or {})
    stored = _store_event(item)
    if stored:
        channel = f"social-mcp/comments/{alias}/{post_id}" if post_id else (
            f"social-mcp/comments/{alias}"
        )
        _publish_event(
            channel,
            {k: v for k, v in item.items() if k not in {"PK", "SK"}},
        )
    return 1 if stored else 0


def _store_event(item: dict) -> bool:
    """Conditional put: dedupe on the full composite key.

    Meta re-delivers webhooks aggressively on 2xx latency and on failure;
    the (COMMENT_EVENT#<alias>, TS#<time>#<comment_id>) tuple is idempotent
    across retries and across the two subscribed products (Pages and
    Instagram) because an IG account's own Page token is used for both
    paths so the same comment never arrives on two subscriptions."""
    try:
        _core_table().put_item(
            Item=item, ConditionExpression="attribute_not_exists(PK)"
        )
        return True
    except Exception as exc:  # noqa: BLE001
        if "ConditionalCheckFailed" in type(exc).__name__:
            log.info(
                "duplicate comment event %s %s ignored",
                item["account"],
                item.get("comment_id", ""),
            )
            return False
        log.exception("DDB put_item failed")
        raise


async def handshake(request: Request):
    """Meta sends ?hub.mode=subscribe&hub.verify_token=...&hub.challenge=..."""
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == _verify_token()
    ):
        return PlainTextResponse(params.get("hub.challenge", ""))
    return PlainTextResponse("forbidden", status_code=403)


async def receive(request: Request):
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not _valid_signature(headers, body):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        payload = json.loads(body)
    except ValueError:
        return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

    stored = 0
    for entry in payload.get("entry", []) or []:
        entry_id = str(entry.get("id", ""))
        for change in entry.get("changes") or []:
            if change.get("field") != "comments":
                continue
            try:
                stored += _handle_change(entry_id, change)
            except Exception:  # noqa: BLE001
                log.exception("change handler failed")
                continue
        # Instagram's shape nests comment changes under `messaging` or
        # inside `value` without the `field` discriminator in some
        # legacy payloads; keep the Pages-shaped path above and accept
        # the newer canonical shape (`changes[].field == "comments"`)
        # as the only route.

    return JSONResponse({"ok": True, "stored": stored})


async def health(_request: Request):
    return PlainTextResponse("ok")


app = Starlette(
    routes=[
        Route("/", handshake, methods=["GET"]),
        Route("/", receive, methods=["POST"]),
        Route("/webhook", handshake, methods=["GET"]),
        Route("/webhook", receive, methods=["POST"]),
        Route("/health", health, methods=["GET"]),
    ]
)
