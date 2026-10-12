"""Per-connection account scoping via a request header.

    X-Social-Accounts: main-page              one account
    X-Social-Accounts: main-page,news-ig      several
    (absent)                                   every account

Any MCP client that can send the bearer header can send this one, so
pinning an agent to an account is client configuration, not a code change
or a new credential. See sharedModules/scope.py for what this does and
does not guarantee (DESIGN.md section 4.2).
"""

from sharedModules.registry import all_accounts
from sharedModules.scope import set_scope
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .bearer import PUBLIC_PATHS

SCOPE_HEADER = "x-social-accounts"


class AccountScopeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        raw = request.headers.get(SCOPE_HEADER, "")
        aliases = tuple(a.strip() for a in raw.split(",") if a.strip())
        if not aliases:
            set_scope(None)
            return await call_next(request)

        # A typo here would otherwise present as "no accounts are configured"
        # on every tool call, so fail loudly at the door instead.
        known = all_accounts()
        unknown = [a for a in aliases if a not in known]
        if unknown:
            return JSONResponse(
                {
                    "error": "unknown_account_scope",
                    "detail": (
                        f"{SCOPE_HEADER} names unknown accounts: {', '.join(unknown)}; "
                        f"this deployment serves: {', '.join(sorted(known))}"
                    ),
                },
                status_code=400,
            )

        set_scope(aliases)
        return await call_next(request)
