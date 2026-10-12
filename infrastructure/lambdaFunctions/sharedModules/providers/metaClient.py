"""Graph API HTTP client, shared by both providers.

Centralizes:

- The Graph API version pin (DESIGN.md "API versions pinned": v26.0).
- Retry on the backoff-worthy codes (4/17/32/613 and BUC 80001/80002),
  honoring `X-Business-Use-Case-Usage.estimated_time_to_regain_access`.
- Refresh on `190` (token expired/invalid) via a caller-provided refresher.
- Hard failure on `100` and other 4xx bodies that are not retryable.
"""

import json
import logging
import random
import time
from collections.abc import Callable

import httpx

from sharedModules.errors import AccountError

from .metaErrors import RETRYABLE_CODES, TOKEN_INVALID_CODES, plain_message, redact_tokens

GRAPH_API_VERSION = "v26.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0

log = logging.getLogger(__name__)


class GraphClient:
    """Thin wrapper around httpx.Client with Meta-aware retry.

    `access_token_fn` returns the current access token for the account; when
    `refresh_fn` is set it is called with the stale token after a `190` and
    expected to return a new one (and to persist it; the client only cares
    about retrying the request).
    """

    def __init__(
        self,
        access_token_fn: Callable[[], str],
        refresh_fn: Callable[[str], str] | None = None,
        *,
        base_url: str = GRAPH_BASE,
        timeout: float = 30.0,
        _http: httpx.Client | None = None,
    ) -> None:
        self._token = access_token_fn
        self._refresh = refresh_fn
        self._base = base_url
        self._http = _http or httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    # ---------------- public API ----------------

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._request("GET", path, params=params)

    def post(self, path: str, data: dict | None = None, params: dict | None = None) -> dict:
        return self._request("POST", path, data=data, params=params)

    def delete(self, path: str, params: dict | None = None) -> dict:
        return self._request("DELETE", path, params=params)

    # ---------------- internals ----------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
    ) -> dict:
        attempt = 0
        refreshed = False
        while True:
            attempt += 1
            token = self._token()
            query = dict(params or {})
            query["access_token"] = token
            try:
                resp = self._http.request(method, path, params=query, data=data)
            except httpx.HTTPError as e:
                redacted = redact_tokens(str(e))
                raise AccountError(f"network error talking to the Meta API: {redacted}") from e

            if resp.status_code < 400:
                return resp.json() if resp.content else {}

            error = _extract_error(resp)
            code = error.get("code", -1)
            subcode = error.get("error_subcode")

            if code in TOKEN_INVALID_CODES and self._refresh and not refreshed:
                # One shot: refresh, mark, retry once. A second 190 after a
                # refresh means a dead grant and we surface it.
                self._refresh(token)
                refreshed = True
                continue

            if code in RETRYABLE_CODES and attempt < MAX_ATTEMPTS:
                delay = _retry_delay(resp, attempt)
                log.info("meta retry: code=%s attempt=%s delay=%.1fs", code, attempt, delay)
                time.sleep(delay)
                continue

            raise AccountError(plain_message(code, subcode, error.get("message", "")))


def _extract_error(resp: httpx.Response) -> dict:
    try:
        body = resp.json() or {}
    except ValueError:
        return {"code": -1, "message": redact_tokens(resp.text or "")}
    err = body.get("error") or {}
    return {
        "code": err.get("code", -1),
        "error_subcode": err.get("error_subcode"),
        "message": err.get("message", ""),
    }


def _retry_delay(resp: httpx.Response, attempt: int) -> float:
    """Honor Meta's hint when present, otherwise exponential with jitter."""
    header = resp.headers.get("X-Business-Use-Case-Usage")
    if header:
        try:
            parsed = json.loads(header)
            # The header is a dict keyed by business id, each value a list;
            # pull the max minutes across all entries.
            minutes = 0
            for entries in parsed.values():
                for entry in entries or []:
                    minutes = max(minutes, entry.get("estimated_time_to_regain_access", 0))
            if minutes > 0:
                return min(float(minutes * 60), MAX_BACKOFF_SECONDS)
        except (ValueError, AttributeError, TypeError):
            pass
    delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
    delay = min(delay, MAX_BACKOFF_SECONDS)
    return delay + random.uniform(0, 0.25 * delay)
