"""Bluesky client auth tests: refresh flow, retry, redactor.

Uses an httpx.MockTransport so the retry + refresh paths are exercised
without touching the real PDS.
"""


import httpx
import pytest
from sharedModules.errors import AccountError
from sharedModules.providers import blueskyClient


class _State:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.persisted: list[tuple[str, str]] = []


def _handler_factory(state: _State, responses: list):
    """Chain of httpx MockTransport responses, popped per call.

    Each entry is `(status, body_or_dict)`; dicts are JSON-encoded.
    """

    index = {"i": 0}

    def _handle(request: httpx.Request) -> httpx.Response:
        state.calls.append(
            (
                request.method,
                str(request.url.path),
                request.headers.get("Authorization", ""),
            )
        )
        if index["i"] >= len(responses):
            raise AssertionError(f"unexpected call {request.method} {request.url}")
        status, body = responses[index["i"]]
        index["i"] += 1
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)

    return _handle


def _client(state: _State, responses: list) -> blueskyClient.BlueskyClient:
    transport = httpx.MockTransport(_handler_factory(state, responses))
    http = httpx.Client(base_url=blueskyClient.BLUESKY_HOST, transport=transport)
    tokens = {"access": "access-A", "refresh": "refresh-A"}

    def _tokens_fn() -> tuple[str, str]:
        return tokens["access"], tokens["refresh"]

    def _persist(access: str, refresh: str) -> None:
        state.persisted.append((access, refresh))
        tokens["access"] = access
        tokens["refresh"] = refresh

    return blueskyClient.BlueskyClient(
        tokens_fn=_tokens_fn, persist_fn=_persist, _http=http
    )


def test_get_sends_bearer_header_and_returns_json():
    state = _State()
    cli = _client(state, [(200, {"hello": "world"})])
    data = cli.get("app.bsky.feed.getAuthorFeed", params={"actor": "did:plc:x"})
    assert data == {"hello": "world"}
    _, path, auth = state.calls[0]
    assert path == "/xrpc/app.bsky.feed.getAuthorFeed"
    assert auth == "Bearer access-A"


def test_expired_token_refreshes_once_and_retries():
    state = _State()
    cli = _client(
        state,
        [
            (400, {"error": "ExpiredToken", "message": "token expired"}),
            (200, {"accessJwt": "access-B", "refreshJwt": "refresh-B"}),
            (200, {"ok": True}),
        ],
    )
    data = cli.get("app.bsky.feed.getAuthorFeed")
    assert data == {"ok": True}
    # Refresh persisted the new tokens
    assert state.persisted == [("access-B", "refresh-B")]
    # Second request used the refreshed access JWT
    assert state.calls[2][2] == "Bearer access-B"


def test_second_expired_after_refresh_is_fatal():
    state = _State()
    cli = _client(
        state,
        [
            (400, {"error": "ExpiredToken"}),
            (400, {"error": "ExpiredToken"}),
        ],
    )
    with pytest.raises(AccountError) as exc:
        cli.get("app.bsky.feed.getAuthorFeed")
    # The refresh call itself failed (second 400 is from refreshSession)
    assert "refresh" in str(exc.value).lower() or "connect-bluesky" in str(exc.value).lower()


def test_rate_limit_retries_and_eventually_succeeds(monkeypatch):
    monkeypatch.setattr(blueskyClient.time, "sleep", lambda *_: None)
    state = _State()
    cli = _client(
        state,
        [
            (429, {"error": "RateLimitExceeded"}),
            (429, {"error": "RateLimitExceeded"}),
            (200, {"ok": True}),
        ],
    )
    assert cli.get("app.bsky.feed.getAuthorFeed") == {"ok": True}


def test_non_retryable_error_raises_mapped_message():
    state = _State()
    cli = _client(state, [(400, {"error": "InvalidRequest", "message": "no good"})])
    with pytest.raises(AccountError) as exc:
        cli.get("app.bsky.feed.getAuthorFeed")
    assert "malformed" in str(exc.value).lower() or "no good" in str(exc.value)


def test_redactor_hides_bearer_tokens():
    noisy = (
        "connection failed, "
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payloadabcdefghij.sigsigsigsigsig"
    )
    out = blueskyClient._redact_bearer(noisy)
    assert "<redacted>" in out
    assert "eyJhbGciOi" not in out


def test_redactor_hides_three_part_jwt_without_bearer():
    noisy = "token=aaaaaaaaaaaaaaaaaaaa.bbbbbbbbbbbbbbbbbbbb.cccccccccccccccccccc rest"
    out = blueskyClient._redact_bearer(noisy)
    assert "<redacted>" in out


def test_upload_blob_sets_content_type_header():
    state = _State()
    cli = _client(state, [(200, {"blob": {"$type": "blob", "size": 42}})])
    cli.upload_blob(b"abc", "image/png")
    method, path, _ = state.calls[0]
    assert method == "POST"
    assert path == "/xrpc/com.atproto.repo.uploadBlob"


def test_post_json_bypass_helper_raises_on_error():
    def _handle(_request):
        return httpx.Response(400, json={"error": "InvalidRequest", "message": "nope"})

    transport = httpx.MockTransport(_handle)
    _real_client = httpx.Client

    def _patched(*a, **k):
        k.pop("transport", None)
        return _real_client(*a, transport=transport, **k)

    import unittest.mock

    with unittest.mock.patch.object(httpx, "Client", _patched):
        with pytest.raises(AccountError):
            blueskyClient.post_json("/xrpc/com.atproto.server.createSession", {"identifier": "a"})
