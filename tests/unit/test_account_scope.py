"""Account scoping middleware (auth/accountScope.py)."""

import json

import pytest
from auth import accountScope
from sharedModules import scope
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def _set_roster(monkeypatch, aliases):
    from sharedModules import registry

    entries = [
        {
            "alias": a,
            "provider": "facebook",
            "publish_enabled": False,
            "publish_mode": "manual",
            "auto_for_kinds": [],
            "policy": {},
        }
        for a in aliases
    ]
    monkeypatch.setenv("ACCOUNTS", json.dumps(entries))
    registry.all_accounts.cache_clear()


@pytest.fixture
def client(monkeypatch):
    _set_roster(monkeypatch, ["main-page", "news-ig"])

    async def who(_request):
        return PlainTextResponse(json.dumps(scope.allowed_accounts() or ()))

    app = Starlette(routes=[Route("/mcp", who, methods=["POST"])])
    app.add_middleware(accountScope.AccountScopeMiddleware)
    with TestClient(app) as c:
        yield c


def test_no_header_leaves_scope_open(client):
    assert client.post("/mcp").json() == []


def test_header_one_account_narrows(client):
    resp = client.post("/mcp", headers={"X-Social-Accounts": "main-page"})
    assert resp.json() == ["main-page"]


def test_header_several_narrows(client):
    resp = client.post(
        "/mcp", headers={"X-Social-Accounts": "main-page, news-ig"}
    )
    assert sorted(resp.json()) == ["main-page", "news-ig"]


def test_header_unknown_alias_400s(client):
    resp = client.post("/mcp", headers={"X-Social-Accounts": "no-such-page"})
    assert resp.status_code == 400
    body = resp.json()
    assert "no-such-page" in body["detail"]
    assert "main-page" in body["detail"]  # names the known ones
