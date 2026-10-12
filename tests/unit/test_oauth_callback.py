"""Hosted OAuth callback (M6).

TestClient over the Starlette app, moto-backed DynamoDB + KMS +
Secrets Manager. The Graph calls are patched out so no network reaches
Meta during unit tests.
"""

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import boto3
import pytest
from starlette.testclient import TestClient


def _load_app():
    """Load oauthCallback/app.py as its own module.

    conftest adds mcpServer and publisher to sys.path; the oauth callback
    sits beside them and has not been added, so `import app` would pick
    whichever landed first. Loading by file path keeps this test
    independent of the other `app` modules.
    """
    root = Path(__file__).resolve().parents[2]
    path = root / "infrastructure" / "lambdaFunctions" / "oauthCallback" / "app.py"
    spec = importlib.util.spec_from_file_location("oauth_callback_app", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules["oauth_callback_app"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def oauth(aws, monkeypatch):
    # Standalone Secrets Manager secret for the Meta app credentials
    boto3.client("secretsmanager").create_secret(
        Name="test-social-meta-app",
        SecretString=json.dumps({"app_id": "123", "app_secret": "shh"}),
    )
    monkeypatch.setenv("META_APP_SECRET", "test-social-meta-app")

    module = _load_app()

    # Patch Graph helpers so no network traffic reaches Meta.
    def fake_exchange_code(creds, code, redirect_uri):
        assert code == "the-code"
        return "short-user-token"

    def fake_long_lived(creds, short_token):
        assert short_token == "short-user-token"
        return "long-lived-user-token"

    monkeypatch.setattr(module, "_exchange_code", fake_exchange_code)
    monkeypatch.setattr(module, "_long_lived", fake_long_lived)

    client = TestClient(module.app)
    return client, module


def _write_state(state: str, **overrides) -> int:
    """Write a pending CONNECT_STATE row. Returns the ttl epoch."""
    table_name = os.environ["CORE_TABLE"]
    expires_at = int(time.time()) + 900
    item = {
        "PK": f"CONNECT_STATE#{state}",
        "SK": "META",
        "status": "pending",
        "alias_prefix": "main-page",
        "scopes_tier": "readonly",
        "operator_identity": "tester",
        "page_id": "",
        "ig_alias": "",
        "requested_at": "2026-10-11T00:00:00+00:00",
        "message": "",
        "aliases": [],
        "ttl": expires_at,
    }
    item.update(overrides)
    boto3.resource("dynamodb").Table(table_name).put_item(Item=item)
    return expires_at


def _patch_pages(monkeypatch, module, pages: list[dict]) -> None:
    monkeypatch.setattr(module, "_enumerate_pages", lambda token: pages)


def test_success_single_page_writes_account_and_marks_completed(oauth, monkeypatch):
    client, module = oauth
    _write_state("abc")
    _patch_pages(
        monkeypatch,
        module,
        [
            {
                "id": "1000000000000001",
                "name": "Test Page",
                "access_token": "page-token",
                "instagram_business_account": {},
            }
        ],
    )

    resp = client.get("/callback", params={"code": "the-code", "state": "abc"})
    assert resp.status_code == 200
    assert "Authorization complete" in resp.text

    row = boto3.resource("dynamodb").Table(os.environ["CORE_TABLE"]).get_item(
        Key={"PK": "CONNECT_STATE#abc", "SK": "META"}
    )["Item"]
    assert row["status"] == "completed"
    assert row["aliases"] == ["main-page"]

    account = boto3.resource("dynamodb").Table(os.environ["CORE_TABLE"]).get_item(
        Key={"PK": "ACCOUNT#main-page", "SK": "META"}
    )["Item"]
    assert account["provider"] == "facebook"
    assert account["display_name"] == "Test Page"
    assert account["status"] == "active"


def test_success_with_linked_ig_writes_both_accounts(oauth, monkeypatch):
    client, module = oauth
    _write_state("ig-state", ig_alias="main-page-ig")
    _patch_pages(
        monkeypatch,
        module,
        [
            {
                "id": "1000000000000001",
                "name": "Test Page",
                "access_token": "page-token",
                "instagram_business_account": {"id": "17841000000000001"},
            }
        ],
    )

    resp = client.get("/callback", params={"code": "the-code", "state": "ig-state"})
    assert resp.status_code == 200

    tbl = boto3.resource("dynamodb").Table(os.environ["CORE_TABLE"])
    row = tbl.get_item(Key={"PK": "CONNECT_STATE#ig-state", "SK": "META"})["Item"]
    assert row["status"] == "completed"
    assert set(row["aliases"]) == {"main-page", "main-page-ig"}

    ig = tbl.get_item(Key={"PK": "ACCOUNT#main-page-ig", "SK": "META"})["Item"]
    assert ig["provider"] == "instagram"
    assert ig["account_id"] == "17841000000000001"
    assert ig["linked_page"] == "main-page"

    fb = tbl.get_item(Key={"PK": "ACCOUNT#main-page", "SK": "META"})["Item"]
    assert fb["linked_ig_account"] == "main-page-ig"


def test_unknown_state_returns_404(oauth):
    client, _ = oauth
    resp = client.get("/callback", params={"code": "the-code", "state": "nope"})
    assert resp.status_code == 404
    assert "not recognized" in resp.text or "expired" in resp.text


def test_expired_state_is_rejected(oauth, monkeypatch):
    client, _ = oauth
    _write_state("stale", ttl=int(time.time()) - 10)
    resp = client.get("/callback", params={"code": "the-code", "state": "stale"})
    assert resp.status_code == 410

    row = boto3.resource("dynamodb").Table(os.environ["CORE_TABLE"]).get_item(
        Key={"PK": "CONNECT_STATE#stale", "SK": "META"}
    )["Item"]
    assert row["status"] == "error"


def test_meta_error_query_param_updates_state_without_touching_meta(oauth):
    client, _ = oauth
    _write_state("denied")
    resp = client.get("/callback", params={"state": "denied", "error": "access_denied"})
    assert resp.status_code == 400

    row = boto3.resource("dynamodb").Table(os.environ["CORE_TABLE"]).get_item(
        Key={"PK": "CONNECT_STATE#denied", "SK": "META"}
    )["Item"]
    assert row["status"] == "error"
    assert "access_denied" in row["message"]


def test_replay_is_rejected(oauth, monkeypatch):
    client, module = oauth
    _write_state("once")
    _patch_pages(
        monkeypatch,
        module,
        [
            {
                "id": "1000000000000001",
                "name": "Test Page",
                "access_token": "page-token",
                "instagram_business_account": {},
            }
        ],
    )

    first = client.get("/callback", params={"code": "the-code", "state": "once"})
    assert first.status_code == 200

    # Replay with the same state must not re-touch the record.
    second = client.get("/callback", params={"code": "the-code", "state": "once"})
    assert second.status_code == 409

    row = boto3.resource("dynamodb").Table(os.environ["CORE_TABLE"]).get_item(
        Key={"PK": "CONNECT_STATE#once", "SK": "META"}
    )["Item"]
    assert row["status"] == "completed"


def test_ambiguous_pages_without_page_id_errors_cleanly(oauth, monkeypatch):
    client, module = oauth
    _write_state("ambig")
    _patch_pages(
        monkeypatch,
        module,
        [
            {
                "id": "1",
                "name": "Alpha",
                "access_token": "t1",
                "instagram_business_account": {},
            },
            {
                "id": "2",
                "name": "Beta",
                "access_token": "t2",
                "instagram_business_account": {},
            },
        ],
    )
    resp = client.get("/callback", params={"code": "the-code", "state": "ambig"})
    assert resp.status_code == 400

    row = boto3.resource("dynamodb").Table(os.environ["CORE_TABLE"]).get_item(
        Key={"PK": "CONNECT_STATE#ambig", "SK": "META"}
    )["Item"]
    assert row["status"] == "error"


def test_specific_page_id_picks_the_right_page(oauth, monkeypatch):
    client, module = oauth
    _write_state("pick", page_id="2")
    _patch_pages(
        monkeypatch,
        module,
        [
            {
                "id": "1",
                "name": "Alpha",
                "access_token": "t1",
                "instagram_business_account": {},
            },
            {
                "id": "2",
                "name": "Beta",
                "access_token": "t2",
                "instagram_business_account": {},
            },
        ],
    )
    resp = client.get("/callback", params={"code": "the-code", "state": "pick"})
    assert resp.status_code == 200

    account = boto3.resource("dynamodb").Table(os.environ["CORE_TABLE"]).get_item(
        Key={"PK": "ACCOUNT#main-page", "SK": "META"}
    )["Item"]
    assert account["account_id"] == "2"
    assert account["display_name"] == "Beta"


def test_health_endpoint_is_open(oauth):
    client, _ = oauth
    assert client.get("/health").status_code == 200
