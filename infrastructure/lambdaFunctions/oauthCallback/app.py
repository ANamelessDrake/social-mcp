"""Hosted OAuth callback for `admin.py connect --remote`.

Deployment landed from a laptop with no AWS credentials, or consenting
from a phone, needs a redirect URI that is reachable from the browser
but still owned by this deployment. This Lambda is that redirect URI:
Facebook sends the authorization code here, the Lambda exchanges it for
a long-lived user token, enumerates Pages, writes the ACCOUNT items, and
updates the matching CONNECT_STATE record that the admin CLI is polling.

A CONNECT_STATE record is written by the admin CLI before the browser
opens; its 15-minute TTL is the window for the operator to complete the
dialog. Each state is single-use: the first callback marks the record
`status=completed` or `status=error`, and any subsequent callback for
the same state returns a short error page without touching the record.
"""

import json
import logging
import os
import time
import urllib.parse
import urllib.request

from sharedModules import crypto, dynamo
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse
from starlette.routing import Route

log = logging.getLogger()
log.setLevel(logging.INFO)

GRAPH_API_VERSION = "v26.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Scope bundles mirror scripts/admin.py; one Facebook Login consent dialog
# covers Pages and Instagram, so the IG scopes ride on every tier.
PAGE_SCOPES = {
    "readonly": [
        "pages_show_list",
        "pages_read_engagement",
        "pages_read_user_content",
        "read_insights",
        "business_management",
        "public_profile",
        "instagram_basic",
        "instagram_manage_insights",
    ],
    "publish": [
        "pages_show_list",
        "pages_read_engagement",
        "pages_read_user_content",
        "read_insights",
        "business_management",
        "public_profile",
        "instagram_basic",
        "instagram_manage_insights",
        "pages_manage_posts",
        "publish_video",
    ],
    "engagement": [
        "pages_show_list",
        "pages_read_engagement",
        "pages_read_user_content",
        "read_insights",
        "business_management",
        "public_profile",
        "instagram_basic",
        "instagram_manage_insights",
        "pages_manage_posts",
        "publish_video",
        "pages_manage_engagement",
        "instagram_manage_comments",
    ],
    "publish-ig": [
        "pages_show_list",
        "pages_read_engagement",
        "pages_read_user_content",
        "read_insights",
        "business_management",
        "public_profile",
        "instagram_basic",
        "instagram_manage_insights",
        "pages_manage_posts",
        "publish_video",
        "instagram_content_publish",
    ],
    "delete-contents": [
        "pages_show_list",
        "pages_read_engagement",
        "pages_read_user_content",
        "read_insights",
        "business_management",
        "public_profile",
        "instagram_basic",
        "instagram_manage_insights",
        "pages_manage_posts",
        "publish_video",
        "pages_manage_engagement",
        "instagram_manage_comments",
        "instagram_content_publish",
        "instagram_manage_contents",
    ],
}


def _success_page(aliases: list[str]) -> str:
    body = "<li>" + "</li><li>".join(aliases) + "</li>" if aliases else "<li>(none)</li>"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>social-mcp: connected</title></head><body>"
        "<h1>Authorization complete</h1>"
        "<p>You can close this tab. The admin CLI is picking up the result.</p>"
        f"<p>Accounts registered:</p><ul>{body}</ul>"
        "</body></html>"
    )


def _error_page(message: str) -> str:
    # The operator sees this; the admin CLI sees the same message via the
    # state record. Keep it short and free of anything token-shaped.
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>social-mcp: authorization failed</title></head><body>"
        "<h1>Authorization failed</h1>"
        f"<p>{message}</p>"
        "<p>Check your admin CLI window; it will time out shortly if "
        "nothing further happens.</p>"
        "</body></html>"
    )


def _meta_app_creds() -> dict:
    import boto3

    secret_id = os.environ["META_APP_SECRET"]
    body = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)[
        "SecretString"
    ]
    data = json.loads(body)
    if not data.get("app_id") or not data.get("app_secret"):
        raise RuntimeError("Meta app secret missing app_id or app_secret")
    return data


def _graph_get(path: str, params: dict) -> dict:
    url = f"{GRAPH_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode())
    if "error" in payload:
        raise RuntimeError(f"Graph error on {path}: {payload['error'].get('message', 'unknown')}")
    return payload


def _exchange_code(creds: dict, code: str, redirect_uri: str) -> str:
    data = _graph_get(
        "/oauth/access_token",
        {
            "client_id": creds["app_id"],
            "client_secret": creds["app_secret"],
            "redirect_uri": redirect_uri,
            "code": code,
        },
    )
    return data["access_token"]


def _long_lived(creds: dict, short_token: str) -> str:
    data = _graph_get(
        "/oauth/access_token",
        {
            "grant_type": "fb_exchange_token",
            "client_id": creds["app_id"],
            "client_secret": creds["app_secret"],
            "fb_exchange_token": short_token,
        },
    )
    return data["access_token"]


def _enumerate_pages(user_token: str) -> list[dict]:
    data = _graph_get(
        "/me/accounts",
        {
            "fields": "id,name,access_token,instagram_business_account",
            "access_token": user_token,
        },
    )
    return data.get("data", [])


def _own_url(request: Request) -> str:
    """Reconstruct the Function URL this Lambda is reachable at.

    Starlette's request.url gives the full URL including path and query;
    we need the path portion without the query so the redirect URI we
    give Meta matches the one Meta is calling us on (otherwise
    `redirect_uri` on the code exchange rejects).
    """
    url = request.url
    return f"{url.scheme}://{url.netloc}{url.path}"


def _resolve_scopes(tier: str) -> list[str]:
    return PAGE_SCOPES.get(tier, PAGE_SCOPES["readonly"])


def _register_page(
    *,
    alias: str,
    page: dict,
    user_token: str,
    scopes: list[str],
    ig_alias: str = "",
) -> list[str]:
    """Write the ACCOUNT item for a Page and (optionally) its linked IG account.

    Returns the list of aliases actually written so the state record can
    report them back to the operator.
    """
    page_token_ct = crypto.encrypt(page["access_token"])
    user_token_ct = crypto.encrypt(user_token)
    linked_ig = ""

    aliases: list[str] = []
    ig = page.get("instagram_business_account", {}).get("id", "")
    if ig and ig_alias:
        linked_ig = ig_alias

    dynamo.put_account(
        alias,
        {
            "provider": "facebook",
            "account_id": page["id"],
            "display_name": page["name"],
            "scopes": scopes,
            "page_token_ct": page_token_ct,
            "user_token_ct": user_token_ct,
            "status": "active",
            "linked_ig_account": linked_ig,
        },
    )
    aliases.append(alias)

    if ig and ig_alias:
        dynamo.put_account(
            ig_alias,
            {
                "provider": "instagram",
                "account_id": ig,
                "display_name": f"{page['name']} (Instagram)",
                "scopes": scopes,
                "page_token_ct": page_token_ct,
                "status": "active",
                "linked_page": alias,
            },
        )
        aliases.append(ig_alias)

    return aliases


def _complete(state: str, aliases: list[str]) -> None:
    dynamo.update_connect_state(
        state,
        status="completed",
        message="",
        aliases=aliases,
    )


def _fail(state: str, message: str) -> None:
    dynamo.update_connect_state(state, status="error", message=message)


async def callback(request: Request):
    params = dict(request.query_params)
    state = params.get("state", "")
    code = params.get("code", "")

    if not state:
        return HTMLResponse(_error_page("missing state parameter"), status_code=400)

    record = dynamo.read_connect_state(state)
    if record is None:
        # TTL expired, replay, or never existed; look the same to the browser.
        return HTMLResponse(_error_page("state not recognized or expired"), status_code=404)

    status = record.get("status", "pending")
    if status != "pending":
        # Single-use: a completed or errored state cannot be reused. Do not
        # mutate the record further.
        return HTMLResponse(
            _error_page("this authorization link has already been used"),
            status_code=409,
        )

    expires_at = int(record.get("ttl", 0))
    if expires_at and expires_at < int(time.time()):
        _fail(state, "state record expired before the browser returned")
        return HTMLResponse(_error_page("authorization window expired"), status_code=410)

    error = params.get("error") or params.get("error_description")
    if error:
        # Meta told the browser the user denied or something went wrong.
        _fail(state, f"meta reported: {error}")
        return HTMLResponse(_error_page(error), status_code=400)

    if not code:
        _fail(state, "no code and no error returned from meta")
        return HTMLResponse(_error_page("no code returned"), status_code=400)

    tier = record.get("scopes_tier", "readonly")
    alias_prefix = record.get("alias_prefix", "")
    requested_page_id = record.get("page_id", "")
    ig_alias = record.get("ig_alias", "")

    try:
        creds = _meta_app_creds()
        redirect_uri = _own_url(request)
        short = _exchange_code(creds, code, redirect_uri)
        user_token = _long_lived(creds, short)
        pages = _enumerate_pages(user_token)
    except Exception as exc:  # noqa: BLE001
        _fail(state, f"token exchange or enumeration failed: {type(exc).__name__}")
        log.exception("oauth callback: token exchange failed")
        return HTMLResponse(_error_page("could not complete the token exchange"), status_code=502)

    if not pages:
        _fail(state, "authorizing user administers no Pages")
        return HTMLResponse(
            _error_page("no Pages visible on the authorizing user"), status_code=400
        )

    # The admin CLI either named a specific page_id or asked us to pick the
    # single page the user administers. If a specific id was requested,
    # match it; otherwise require exactly one, since the hosted flow has no
    # chance to prompt the operator.
    if requested_page_id:
        matched = [p for p in pages if p["id"] == requested_page_id]
        if not matched:
            _fail(state, "requested page_id not among the authorized Pages")
            return HTMLResponse(
                _error_page("requested Page is not among the authorized Pages"),
                status_code=400,
            )
        chosen = matched[0]
    elif len(pages) == 1:
        chosen = pages[0]
    else:
        names = ", ".join(f"{p['name']} [{p['id']}]" for p in pages)
        _fail(state, f"ambiguous: {names}")
        return HTMLResponse(
            _error_page(
                "the authorizing user administers more than one Page; "
                "re-run admin.py connect --remote --page-id &lt;id&gt; naming "
                "the Page you want to register"
            ),
            status_code=400,
        )

    try:
        aliases = _register_page(
            alias=alias_prefix,
            page=chosen,
            user_token=user_token,
            scopes=_resolve_scopes(tier),
            ig_alias=ig_alias,
        )
    except Exception as exc:  # noqa: BLE001
        _fail(state, f"account registration failed: {type(exc).__name__}")
        log.exception("oauth callback: register_page failed")
        return HTMLResponse(_error_page("could not write the account record"), status_code=500)

    _complete(state, aliases)
    return HTMLResponse(_success_page(aliases))


async def health(_request):
    return PlainTextResponse("ok")


app = Starlette(
    routes=[
        Route("/", callback, methods=["GET"]),
        Route("/callback", callback, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
    ]
)
