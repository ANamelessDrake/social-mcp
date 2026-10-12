"""End-to-end webhook ingest tests: alias resolution, dedupe, body fetch,
AppSync publish (DESIGN.md M7).
"""

import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from conftest import load_fixture

ROOT = Path(__file__).resolve().parents[2]
WEBHOOK_APP_PATH = (
    ROOT / "infrastructure" / "lambdaFunctions" / "webhookIngest" / "app.py"
)


def _load_webhook_app():
    spec = importlib.util.spec_from_file_location(
        "webhook_ingest_app", str(WEBHOOK_APP_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["webhook_ingest_app"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _sign(body: bytes, secret: str = "shh") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def ingest(monkeypatch, aws):
    import boto3

    sm = boto3.client("secretsmanager")
    sm.create_secret(Name="verify", SecretString="verify-me")
    sm.create_secret(
        Name="meta-app",
        SecretString=json.dumps({"app_id": "1", "app_secret": "shh"}),
    )
    monkeypatch.setenv("WEBHOOK_VERIFY_TOKEN_SECRET", "verify")
    monkeypatch.setenv("META_APP_SECRET", "meta-app")
    monkeypatch.setenv("EVENTS_HTTP_ENDPOINT", "https://events.example/event")
    monkeypatch.setenv("EVENTS_API_KEY", "test-key")

    sys.modules.pop("webhook_ingest_app", None)
    mod = _load_webhook_app()
    mod._VERIFY_TOKEN_CACHE.clear()
    mod._META_APP_SECRET_CACHE.clear()
    mod._CORE_TABLE = None
    return mod


def _write_account(table, alias: str, provider: str, account_id: str) -> None:
    table.put_item(
        Item={
            "PK": f"ACCOUNT#{alias}",
            "SK": "META",
            "provider": provider,
            "account_id": account_id,
            "scopes": [
                "pages_read_engagement",
                "pages_manage_engagement",
                "instagram_manage_comments",
            ],
        }
    )


def test_fb_entry_stores_and_fetches_body(ingest, monkeypatch):
    import boto3

    table = boto3.resource("dynamodb").Table("test-socialmcp-core")
    _write_account(table, "main-page", "facebook", "100000000000000")

    # Patch the lazy-import fetch_comment path to return the fixture body
    def fake_fetch(alias, provider, comment_id):
        return load_fixture("fb_comment_fetch.json") | {
            "comment_id": comment_id,
            "text": "Looks great, where can I buy this?",
            "author": "Example Commenter",
            "created_time": "2026-10-01T12:00:30+0000",
            "thread_id": "100000000000000_777777777777777",
            "is_hidden": False,
        }

    monkeypatch.setattr(ingest, "_fetch_comment_body", fake_fetch)

    published = []
    monkeypatch.setattr(
        ingest, "_publish_event", lambda ch, ev: published.append((ch, ev))
    )

    from starlette.testclient import TestClient

    client = TestClient(ingest.app)
    body = json.dumps(load_fixture("fb_webhook_comment.json")).encode()
    resp = client.post(
        "/",
        content=body,
        headers={"x-hub-signature-256": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "stored": 1}
    assert published and published[0][0].startswith(
        "social-mcp/comments/main-page/"
    )
    assert published[0][1]["text"] == "Looks great, where can I buy this?"


def test_ig_entry_stores(ingest, monkeypatch):
    import boto3

    table = boto3.resource("dynamodb").Table("test-socialmcp-core")
    _write_account(table, "main-ig", "instagram", "17841000000000001")

    def fake_fetch(alias, provider, comment_id):
        return load_fixture("ig_comment_fetch.json") | {
            "comment_id": comment_id,
            "author": "another_user",
            "created_time": "2026-10-01T12:00:30+0000",
            "thread_id": comment_id,
            "is_hidden": False,
        }

    monkeypatch.setattr(ingest, "_fetch_comment_body", fake_fetch)
    monkeypatch.setattr(ingest, "_publish_event", lambda *a, **k: None)

    from starlette.testclient import TestClient

    client = TestClient(ingest.app)
    body = json.dumps(load_fixture("ig_webhook_comment.json")).encode()
    resp = client.post(
        "/",
        content=body,
        headers={"x-hub-signature-256": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "stored": 1}


def test_unknown_entry_id_dropped_silently(ingest, monkeypatch):
    # No ACCOUNT row for this id; ingest should log and drop.
    monkeypatch.setattr(
        ingest, "_fetch_comment_body", lambda *a: pytest.fail("should not fetch")
    )

    from starlette.testclient import TestClient

    client = TestClient(ingest.app)
    body = json.dumps(load_fixture("fb_webhook_comment.json")).encode()
    resp = client.post(
        "/",
        content=body,
        headers={"x-hub-signature-256": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "stored": 0}


def test_dedupe_on_second_delivery(ingest, monkeypatch):
    import boto3

    table = boto3.resource("dynamodb").Table("test-socialmcp-core")
    _write_account(table, "main-page", "facebook", "100000000000000")
    monkeypatch.setattr(
        ingest,
        "_fetch_comment_body",
        lambda a, p, c: {
            "comment_id": c,
            "text": "x",
            "author": "y",
            "created_time": "2026-10-01T12:00:30+0000",
            "thread_id": "p",
            "is_hidden": False,
        },
    )
    monkeypatch.setattr(ingest, "_publish_event", lambda *a, **k: None)

    from starlette.testclient import TestClient

    client = TestClient(ingest.app)
    body = json.dumps(load_fixture("fb_webhook_comment.json")).encode()
    sig = _sign(body)

    first = client.post("/", content=body, headers={"x-hub-signature-256": sig})
    second = client.post("/", content=body, headers={"x-hub-signature-256": sig})
    assert first.json() == {"ok": True, "stored": 1}
    assert second.json() == {"ok": True, "stored": 0}


def test_fetch_failure_stores_with_placeholder(ingest, monkeypatch):
    import boto3

    table = boto3.resource("dynamodb").Table("test-socialmcp-core")
    _write_account(table, "main-page", "facebook", "100000000000000")

    def failing_fetch(*args, **kwargs):
        raise RuntimeError("network")

    monkeypatch.setattr(ingest, "_fetch_comment_body", failing_fetch)
    published = []
    monkeypatch.setattr(
        ingest, "_publish_event", lambda ch, ev: published.append((ch, ev))
    )

    from starlette.testclient import TestClient

    client = TestClient(ingest.app)
    body = json.dumps(load_fixture("fb_webhook_comment.json")).encode()
    resp = client.post(
        "/",
        content=body,
        headers={"x-hub-signature-256": _sign(body)},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "stored": 1}
    stored_event = published[0][1]
    # Placeholder body means we fell back to the webhook's inline message.
    assert stored_event["text"] == "Looks great, where can I buy this?"
    assert stored_event["body_fetched"] is False
