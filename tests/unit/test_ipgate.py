"""Source-IP allowlist middleware (auth/ipgate.py)."""

import json

import pytest
from auth import ipgate
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

ALLOWED_IP = "198.51.100.5"
OTHER_IP = "203.0.113.7"


def _context(ip: str) -> dict:
    return {"x-amzn-request-context": json.dumps({"http": {"sourceIp": ip}})}


@pytest.fixture
def client(monkeypatch):
    async def ok(_request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[
        Route("/mcp", ok, methods=["POST"]),
        Route("/health", ok, methods=["GET"]),
    ])
    app.add_middleware(ipgate.IpAllowlistMiddleware)

    monkeypatch.setenv("ALLOWED_CLIENT_HOSTNAMES", "vpn.example.com")
    monkeypatch.setattr(ipgate, "_resolve", lambda hostname: {ALLOWED_IP})
    with TestClient(app) as c:
        yield c


def test_disabled_when_no_hostnames(client, monkeypatch):
    monkeypatch.setenv("ALLOWED_CLIENT_HOSTNAMES", "")
    assert client.post("/mcp").status_code == 200


def test_allowed_source_ip_passes(client):
    assert client.post("/mcp", headers=_context(ALLOWED_IP)).status_code == 200


def test_other_source_ip_is_rejected(client):
    resp = client.post("/mcp", headers=_context(OTHER_IP))
    assert resp.status_code == 403
    assert resp.json() == {"error": "forbidden_source"}


def test_x_forwarded_for_cannot_spoof(client):
    resp = client.post(
        "/mcp",
        headers={**_context(OTHER_IP), "X-Forwarded-For": ALLOWED_IP},
    )
    assert resp.status_code == 403


def test_missing_context_fails_closed(client):
    assert client.post("/mcp").status_code == 403


def test_garbled_context_fails_closed(client):
    resp = client.post("/mcp", headers={"x-amzn-request-context": "not-json"})
    assert resp.status_code == 403


def test_unresolvable_hostnames_fail_closed(client, monkeypatch):
    monkeypatch.setattr(ipgate, "_resolve", lambda hostname: set())
    assert client.post("/mcp", headers=_context(ALLOWED_IP)).status_code == 403


def test_health_stays_public(client):
    assert client.get("/health").status_code == 200
