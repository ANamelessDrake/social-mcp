"""Optional source-IP allowlist, in front of bearer auth (DESIGN.md section 4.3).

When ALLOWED_CLIENT_HOSTNAMES is set (comma-separated DNS names, e.g. a
DDNS record tracking the operator's network), requests must originate from
an IP those names currently resolve to. The source IP comes from the
Function URL request context (Lambda Web Adapter's x-amzn-request-context
header), which clients cannot forge; X-Forwarded-For is deliberately
ignored. DNS answers are cached briefly, so a DDNS IP change is honored
within about a minute. Fails closed: unresolvable hostnames allow nothing.
"""

import json
import logging
import os
import socket
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .bearer import PUBLIC_PATHS

log = logging.getLogger(__name__)

DNS_CACHE_SECONDS = 60

_dns_cache: dict[str, tuple[set[str], float]] = {}


def _resolve(hostname: str) -> set[str]:
    cached = _dns_cache.get(hostname)
    if cached and cached[1] > time.time():
        return cached[0]
    try:
        infos = socket.getaddrinfo(hostname, None)
        ips = {info[4][0] for info in infos}
    except OSError:
        log.warning("could not resolve allowlist hostname %s; failing closed", hostname)
        ips = set()
    _dns_cache[hostname] = (ips, time.time() + DNS_CACHE_SECONDS)
    return ips


def allowed_ips() -> set[str] | None:
    """None = allowlist disabled; otherwise the currently allowed source IPs."""
    raw = os.environ.get("ALLOWED_CLIENT_HOSTNAMES", "")
    hostnames = [h.strip() for h in raw.split(",") if h.strip()]
    if not hostnames:
        return None
    ips: set[str] = set()
    for hostname in hostnames:
        ips |= _resolve(hostname)
    return ips


def source_ip(request: Request) -> str:
    """The connection's real source IP, per the Function URL request context.

    Lambda Web Adapter sets x-amzn-request-context from the invocation
    event, overwriting anything a client sent, so http.sourceIp is
    authoritative. Local runs (uvicorn, tests) have no such header and
    fall back to the socket peer address.
    """
    context = request.headers.get("x-amzn-request-context", "")
    if context:
        try:
            return json.loads(context)["http"]["sourceIp"]
        except (ValueError, KeyError, TypeError):
            log.warning("unparseable x-amzn-request-context; failing closed")
            return ""
    return request.client.host if request.client else ""


class IpAllowlistMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)
        allowed = allowed_ips()
        if allowed is not None and source_ip(request) not in allowed:
            return JSONResponse({"error": "forbidden_source"}, status_code=403)
        return await call_next(request)
