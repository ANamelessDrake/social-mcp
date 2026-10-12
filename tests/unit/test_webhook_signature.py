"""Signature verification on the Meta webhook ingest (DESIGN.md M7).

The handshake path is quick; the HMAC-SHA256 path with constant-time
compare is the one we test exhaustively here.
"""

import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEBHOOK_APP_PATH = (
    ROOT / "infrastructure" / "lambdaFunctions" / "webhookIngest" / "app.py"
)


def _load_webhook_app():
    """Load webhookIngest/app.py under a distinct module name so it does
    not collide with the mcpServer app on the same sys.path."""
    spec = importlib.util.spec_from_file_location(
        "webhook_ingest_app", str(WEBHOOK_APP_PATH)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["webhook_ingest_app"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def webhook_app(monkeypatch, aws):
    import boto3

    sm = boto3.client("secretsmanager")
    sm.create_secret(Name="verify", SecretString="verify-me")
    sm.create_secret(
        Name="meta-app",
        SecretString=json.dumps({"app_id": "1", "app_secret": "shh"}),
    )
    monkeypatch.setenv("WEBHOOK_VERIFY_TOKEN_SECRET", "verify")
    monkeypatch.setenv("META_APP_SECRET", "meta-app")

    sys.modules.pop("webhook_ingest_app", None)
    mod = _load_webhook_app()
    mod._VERIFY_TOKEN_CACHE.clear()
    mod._META_APP_SECRET_CACHE.clear()
    mod._CORE_TABLE = None
    return mod


def _sign(body: bytes, secret: str = "shh") -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_handshake_echoes_challenge(webhook_app):
    from starlette.testclient import TestClient

    client = TestClient(webhook_app.app)
    resp = client.get(
        "/",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "42",
        },
    )
    assert resp.status_code == 200
    assert resp.text == "42"


def test_handshake_wrong_token_is_forbidden(webhook_app):
    from starlette.testclient import TestClient

    client = TestClient(webhook_app.app)
    resp = client.get(
        "/",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "nope",
            "hub.challenge": "42",
        },
    )
    assert resp.status_code == 403


def test_post_rejects_missing_signature(webhook_app):
    from starlette.testclient import TestClient

    client = TestClient(webhook_app.app)
    resp = client.post("/", json={"entry": []})
    assert resp.status_code == 401


def test_post_rejects_wrong_signature(webhook_app):
    from starlette.testclient import TestClient

    client = TestClient(webhook_app.app)
    body = b'{"entry": []}'
    resp = client.post(
        "/",
        content=body,
        headers={
            "x-hub-signature-256": "sha256=deadbeef",
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 401


def test_post_accepts_valid_signature_empty_entry(webhook_app):
    from starlette.testclient import TestClient

    client = TestClient(webhook_app.app)
    body = b'{"entry": []}'
    resp = client.post(
        "/",
        content=body,
        headers={
            "x-hub-signature-256": _sign(body),
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "stored": 0}


def test_post_rejects_when_app_secret_is_placeholder(webhook_app, monkeypatch):
    import boto3

    sm = boto3.client("secretsmanager")
    sm.put_secret_value(SecretId="meta-app", SecretString="placeholder-no-json")
    webhook_app._META_APP_SECRET_CACHE.clear()

    from starlette.testclient import TestClient

    client = TestClient(webhook_app.app)
    body = b'{"entry": []}'
    resp = client.post(
        "/",
        content=body,
        headers={
            "x-hub-signature-256": _sign(body),
            "content-type": "application/json",
        },
    )
    assert resp.status_code == 401
