"""Bearer middleware (auth/bearer.py).

Tested on a bare Starlette app rather than the FastMCP app, whose streamable
HTTP lifespan cannot be started twice per process.
"""

import pytest
from auth import bearer
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("DEV_BEARER_TOKEN", "secret-token")
    bearer.expected_token.cache_clear()

    async def ok(_request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[
        Route("/mcp", ok, methods=["POST"]),
        Route("/health", ok, methods=["GET"]),
    ])
    app.add_middleware(bearer.BearerAuthMiddleware)
    with TestClient(app) as c:
        yield c


def test_health_needs_no_auth(client):
    assert client.get("/health").status_code == 200


def test_missing_header_401s_with_www_authenticate(client):
    resp = client.post("/mcp")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_wrong_token_401s(client):
    resp = client.post("/mcp", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_correct_token_passes(client):
    resp = client.post("/mcp", headers={"Authorization": "Bearer secret-token"})
    assert resp.status_code == 200


def test_non_bearer_scheme_401s(client):
    resp = client.post("/mcp", headers={"Authorization": "Basic secret-token"})
    assert resp.status_code == 401


def test_constant_time_compare_rejects_prefix(client):
    # hmac.compare_digest returns False when lengths differ even if a prefix
    # matches; proof is 401, not a 200.
    resp = client.post("/mcp", headers={"Authorization": "Bearer secret"})
    assert resp.status_code == 401
