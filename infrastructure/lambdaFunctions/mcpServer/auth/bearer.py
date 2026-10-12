"""Deployment-local bearer token auth (DESIGN.md section 4.1).

Single-tenant by design: one static token in Secrets Manager, all-or-nothing
access. For local development and tests, DEV_BEARER_TOKEN short-circuits
Secrets Manager.
"""

import hmac
import os
from functools import lru_cache

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

PUBLIC_PATHS = {"/health"}


@lru_cache(maxsize=1)
def expected_token() -> str:
    direct = os.environ.get("DEV_BEARER_TOKEN")
    if direct:
        return direct

    import boto3

    secret = boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["BEARER_TOKEN_SECRET"]
    )
    return secret["SecretString"]


class BearerAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in PUBLIC_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(token, expected_token()):
            return JSONResponse(
                {"error": "invalid_token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)
