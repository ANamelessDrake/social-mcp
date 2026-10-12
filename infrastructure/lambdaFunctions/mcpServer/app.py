"""social-mcp MCP server.

FastMCP application served over streamable HTTP. On Lambda it runs behind
Lambda Web Adapter (uvicorn started by run.sh); locally:

    DEV_BEARER_TOKEN=local-token python -m uvicorn app:app --port 8000

The server is stateless (no MCP session affinity), which is required on
Lambda where consecutive requests may hit different containers.
"""

import logging

from auth.accountScope import AccountScopeMiddleware
from auth.bearer import BearerAuthMiddleware
from auth.ipgate import IpAllowlistMiddleware
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from tools import register_all

logging.basicConfig(level=logging.INFO)

# DNS rebinding protection guards localhost-bound servers; this one is a
# public endpoint on a Function URL domain (unknown at build time) behind
# bearer auth, so Host validation would only 421 legitimate traffic.
mcp = FastMCP(
    "social-mcp",
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
register_all(mcp)

app = mcp.streamable_http_app()


class NoServerSentStreamMiddleware(BaseHTTPMiddleware):
    """Refuse GET /mcp, which the spec allows and Lambda makes necessary.

    A GET on the MCP endpoint opens an SSE channel for server-initiated
    messages. This server is stateless and never initiates anything, so the
    stream carries nothing, but on Lambda it parks a whole invocation until
    the client disconnects or the function times out: billed time and a
    concurrency slot per connected client. The spec's answer for a server
    that does not offer the stream is 405, which tells clients to stop
    asking.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method == "GET" and request.url.path.rstrip("/") == "/mcp":
            return JSONResponse(
                {"error": "server_sent_streams_unsupported"},
                status_code=405,
                headers={"Allow": "POST"},
            )
        return await call_next(request)


# Added last = runs first, so the order here is inside-out: IP check, token,
# account scoping (only meaningful once authenticated), then the GET refusal.
app.add_middleware(NoServerSentStreamMiddleware)
app.add_middleware(AccountScopeMiddleware)
app.add_middleware(BearerAuthMiddleware)
app.add_middleware(IpAllowlistMiddleware)


async def health(_request):
    return PlainTextResponse("ok")


app.router.routes.append(Route("/health", health, methods=["GET"]))
