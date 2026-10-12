"""AT Protocol XRPC client, Bluesky's answer to Graph API.

Different auth model from Meta: a short-lived access JWT plus a refresh JWT
minted by `com.atproto.server.createSession`, carried as a Bearer header
rather than Meta's `?access_token=` query parameter. The two tokens live in
the same ACCOUNT item as Meta's `page_token_ct` (access) and
`user_token_ct` (refresh); the comment at the Account dataclass documents
the overload.

The client transparently refreshes on an `ExpiredToken` response (400/401
with `error == "ExpiredToken"`), the same single-shot pattern `metaClient`
uses for Meta's `190`.
"""

import logging
import os
import random
import time
from collections.abc import Callable

import httpx

from sharedModules.errors import AccountError

BLUESKY_HOST = os.environ.get("BLUESKY_HOST", "https://bsky.social")

# atproto XRPC lives under /xrpc/<nsid>; the base URL points at the PDS and
# the NSID is passed to each call. Rate limits are per-PDS with no central
# retry hint header, so the backoff schedule mirrors metaClient's.
MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0

log = logging.getLogger(__name__)

# atproto errors with these names in the JSON body are the token-refresh
# triggers; everything else is non-retryable 4xx.
TOKEN_INVALID_ERRORS = {"ExpiredToken", "InvalidToken", "AuthenticationRequired"}
# atproto's rate-limit error names (plus a few historical ones that still
# appear from older PDS deployments).
RETRYABLE_ERRORS = {"RateLimitExceeded", "UpstreamFailure", "InternalServerError"}


class BlueskyClient:
    """Thin httpx wrapper that speaks XRPC and refreshes JWTs.

    `tokens_fn` returns `(access_jwt, refresh_jwt)` as decrypted strings.
    `persist_fn` is called with a new `(access_jwt, refresh_jwt)` pair after
    a successful refresh so the caller re-encrypts and stores both. The
    client only cares about retrying the current request once.
    """

    def __init__(
        self,
        tokens_fn: Callable[[], tuple[str, str]],
        persist_fn: Callable[[str, str], None] | None = None,
        *,
        base_url: str = BLUESKY_HOST,
        timeout: float = 30.0,
        _http: httpx.Client | None = None,
    ) -> None:
        self._tokens = tokens_fn
        self._persist = persist_fn
        self._base = base_url
        self._http = _http or httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    # ---------------- public API ----------------

    def get(self, nsid: str, params: dict | None = None) -> dict:
        return self._request("GET", f"/xrpc/{nsid}", params=params)

    def post(self, nsid: str, body: dict | None = None) -> dict:
        return self._request("POST", f"/xrpc/{nsid}", json_body=body or {})

    def upload_blob(self, data: bytes, mime: str) -> dict:
        """com.atproto.repo.uploadBlob: POST raw bytes, no JSON envelope.

        Returns the response unchanged; the caller reads `blob` for the
        atproto blob ref ($type, ref, mimeType, size).
        """
        return self._request(
            "POST", "/xrpc/com.atproto.repo.uploadBlob", content=data, content_type=mime
        )

    # ---------------- internals ----------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
    ) -> dict:
        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            access, _refresh = self._tokens()
            headers = {"Authorization": f"Bearer {access}"}
            if content_type:
                headers["Content-Type"] = content_type
            try:
                resp = self._http.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    content=content,
                    headers=headers,
                )
            except httpx.HTTPError as e:
                raise AccountError(
                    f"network error talking to the Bluesky PDS: {_redact_bearer(str(e))}"
                ) from e

            if resp.status_code < 400:
                return resp.json() if resp.content else {}

            error = _extract_error(resp)
            name = error.get("error", "")

            if name in TOKEN_INVALID_ERRORS and not refreshed:
                _access, _refresh2 = self._tokens()
                new_access, new_refresh = self._refresh_session(_refresh2)
                if self._persist:
                    self._persist(new_access, new_refresh)
                refreshed = True
                continue

            if name in RETRYABLE_ERRORS and attempt < MAX_ATTEMPTS:
                delay = _retry_delay(attempt)
                log.info("bluesky retry: error=%s attempt=%s delay=%.1fs", name, attempt, delay)
                time.sleep(delay)
                continue

            raise AccountError(_plain_message(name, error.get("message", "")))

    def _refresh_session(self, refresh_jwt: str) -> tuple[str, str]:
        """com.atproto.server.refreshSession: use the refresh JWT as bearer."""
        try:
            resp = self._http.post(
                "/xrpc/com.atproto.server.refreshSession",
                headers={"Authorization": f"Bearer {refresh_jwt}"},
            )
        except httpx.HTTPError as e:
            raise AccountError(
                f"network error refreshing the Bluesky session: {_redact_bearer(str(e))}"
            ) from e
        if resp.status_code >= 400:
            raise AccountError(
                "Bluesky refresh JWT is no longer valid; "
                "run `python scripts/admin.py connect-bluesky` to re-authorize"
            )
        body = resp.json() or {}
        access = body.get("accessJwt") or ""
        refresh = body.get("refreshJwt") or ""
        if not access or not refresh:
            raise AccountError("Bluesky refresh returned no accessJwt or refreshJwt")
        return access, refresh


# ---------------- module helpers ----------------


# atproto JWTs are long base64url-ish strings with two dots. Redact anything
# that looks token-shaped so the Lambda logs never carry a bearer.
_BEARER_RE_PARTS = [
    r"Bearer\s+[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+",
    r"\b[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\b",
]
import re as _re  # noqa: E402  (kept local so the public namespace stays tight)

_BEARER_RE = _re.compile("|".join(_BEARER_RE_PARTS))


def _redact_bearer(text: str) -> str:
    return _BEARER_RE.sub("<redacted>", text or "")


def _extract_error(resp: httpx.Response) -> dict:
    try:
        body = resp.json() or {}
    except ValueError:
        return {"error": "", "message": _redact_bearer(resp.text or "")}
    return {
        "error": body.get("error", "") or "",
        "message": body.get("message", "") or "",
    }


def _retry_delay(attempt: int) -> float:
    delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
    delay = min(delay, MAX_BACKOFF_SECONDS)
    return delay + random.uniform(0, 0.25 * delay)


def _plain_message(name: str, raw: str) -> str:
    mapped = {
        "ExpiredToken": "Bluesky access token expired; refresh JWT also failed",
        "InvalidToken": "Bluesky access token invalid; the session needs reconnecting",
        "AuthenticationRequired": "Bluesky request was rejected without credentials",
        "RateLimitExceeded": "Bluesky rate limit tripped; backing off",
        "RecordNotFound": "The requested record does not exist on this PDS",
        "InvalidRequest": "Bluesky rejected the request as malformed",
    }
    if name in mapped:
        return mapped[name]
    if name and raw:
        return f"{name}: {_redact_bearer(raw)}"
    if name:
        return name
    return _redact_bearer(raw) or "Bluesky returned an unspecified error"


# Expose for tests/admin.py so the connect flow uses the same JSON parser.
def post_json(path: str, body: dict, *, headers: dict | None = None) -> dict:
    """One-shot JSON POST without a BlueskyClient; used by `connect-bluesky`.

    Returns the parsed response. Raises AccountError on any non-2xx.
    """
    import json as _json

    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    with httpx.Client(base_url=BLUESKY_HOST, timeout=30.0) as http:
        resp = http.post(path, content=_json.dumps(body).encode(), headers=hdrs)
    if resp.status_code >= 400:
        err = _extract_error(resp)
        raise AccountError(_plain_message(err.get("error", ""), err.get("message", "")))
    return resp.json() if resp.content else {}
