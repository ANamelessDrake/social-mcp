#!/usr/bin/env python3
"""Operator CLI for a social-mcp deployment.

    python scripts/admin.py connect --env dev --alias main-page [--profile NAME]
    python scripts/admin.py list-accounts --env dev [--profile NAME]

    # M2+ (not yet implemented in M1)
    python scripts/admin.py approve --env dev --draft-id <id>
    python scripts/admin.py reject --env dev --draft-id <id>
    python scripts/admin.py disable-publishing --env dev
    python scripts/admin.py audit --env dev --account main-page

connect runs the Facebook Login browser consent against a loopback redirect
on this machine, exchanges the short-lived token for a long-lived one,
enumerates /me/accounts, lets you pick which Pages to register, detects any
linked Instagram business account on each Page, KMS-encrypts the tokens,
and writes the ACCOUNT items.

The connect flow is the only path that handles an authorization code.
Requires AWS credentials for the deployment account and a browser; the
code never leaves this machine.
"""

import argparse
import json
import secrets as pysecrets
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]

GRAPH_API_VERSION = "v26.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
FB_AUTH_URL = "https://www.facebook.com/{v}/dialog/oauth".format(v=GRAPH_API_VERSION)

# M1 scopes: read-only Pages. publish_video, pages_manage_posts and the
# engagement scopes land with M2 (`--scopes publish`) and M3.
PAGE_SCOPES = {
    "readonly": [
        "pages_show_list",
        "pages_read_engagement",
        "pages_read_user_content",
        "read_insights",
        "business_management",
        "public_profile",
    ],
}


def _load_config(env: str) -> dict:
    path = ROOT / "config" / f"{env}.json"
    if not path.is_file():
        sys.exit(f"config/{env}.json not found")
    return json.loads(path.read_text())


def _session(profile: str | None, region: str):
    """Region comes from the env config, so an unset profile region never breaks."""
    return boto3.Session(profile_name=profile, region_name=region)


def _secret_prefix(config: dict) -> str:
    return f"{config['environment_name_upper']}-{config['project_name_upper']}"


def _table(config: dict, session):
    name = f"{config['environment_name']}-{config['project_name']}-core"
    return session.resource("dynamodb").Table(name)


def _meta_app_creds(session, prefix: str) -> dict:
    secret = session.client("secretsmanager").get_secret_value(
        SecretId=f"{prefix}-MetaApp"
    )["SecretString"]
    try:
        payload = json.loads(secret)
    except ValueError:
        sys.exit(
            "Meta app secret is not JSON; store it as "
            '\'{"app_id": "...", "app_secret": "..."}\''
        )
    for field in ("app_id", "app_secret"):
        if not payload.get(field):
            sys.exit(f"Meta app secret is missing `{field}`; see docs/meta-setup.md")
    return payload


# -------------------- Facebook Login (loopback redirect) --------------------


class _CallbackHandler(BaseHTTPRequestHandler):
    """Captures ?code=&state= from Meta's loopback redirect."""

    result: dict = {}

    def do_GET(self):  # noqa: N802 (http.server API)
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        body = (
            "Authorization received. You can close this tab."
            if "code" in _CallbackHandler.result
            else f"Authorization failed: {_CallbackHandler.result.get('error', 'unknown')}"
        )
        self.wfile.write(body.encode())

    def log_message(self, *_args):
        pass


def _consent_code(app_id: str, scopes: list[str]) -> tuple[str, str]:
    """Run the browser consent; return (code, redirect_uri)."""
    state = pysecrets.token_urlsafe(16)
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}"

    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(scopes),
        "state": state,
    }
    url = f"{FB_AUTH_URL}?{urllib.parse.urlencode(params)}"

    print("Opening browser for Facebook consent (or open this URL yourself):")
    print(f"  {url}\n")
    webbrowser.open(url)

    server.timeout = 300
    server.handle_request()
    result = _CallbackHandler.result
    if "code" not in result:
        sys.exit(f"consent failed: {result.get('error', 'no response within 5 minutes')}")
    if result.get("state") != state:
        sys.exit("consent failed: state mismatch (possible interception); try again")
    return result["code"], redirect_uri


def _graph_get(path: str, params: dict) -> dict:
    url = f"{GRAPH_BASE}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode()
    data = json.loads(body)
    if "error" in data:
        sys.exit(f"Graph error on {path}: {data['error']}")
    return data


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


def _long_lived_user_token(creds: dict, short_token: str) -> str:
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
    """Return [{id, name, access_token, instagram_business_account}]."""
    data = _graph_get(
        "/me/accounts",
        {"fields": "id,name,access_token,instagram_business_account", "access_token": user_token},
    )
    return data.get("data", [])


# -------------------------------- subcommands --------------------------------


def connect(config: dict, session, alias: str) -> None:
    entries = {a["alias"]: a for a in config.get("accounts", [])}
    if alias not in entries:
        sys.exit(
            f"account '{alias}' is not in config/{config['environment_name']}.json; add it first"
        )
    if entries[alias]["provider"] not in ("facebook", "instagram"):
        sys.exit(
            f"account '{alias}' has provider '{entries[alias]['provider']}' which "
            "connect does not yet support"
        )

    prefix = _secret_prefix(config)
    creds = _meta_app_creds(session, prefix)
    scopes = PAGE_SCOPES["readonly"]

    code, redirect_uri = _consent_code(creds["app_id"], scopes)
    short = _exchange_code(creds, code, redirect_uri)
    user_token = _long_lived_user_token(creds, short)

    pages = _enumerate_pages(user_token)
    if not pages:
        sys.exit(
            "no Pages visible on the authorizing user; make sure the user is an "
            "admin on the Page the deployment should serve (docs/meta-setup.md)"
        )

    print("Pages the authorizing user administers:")
    for idx, page in enumerate(pages, start=1):
        ig = page.get("instagram_business_account", {}).get("id", "")
        ig_hint = f" (linked IG: {ig})" if ig else ""
        print(f"  {idx}. {page['name']} [{page['id']}]{ig_hint}")

    which = input(
        f"\nWhich Page should become alias '{alias}'? (number, or 'q' to quit): "
    ).strip()
    if which.lower() in {"q", "quit", "exit", ""}:
        sys.exit("aborted")
    try:
        page = pages[int(which) - 1]
    except (ValueError, IndexError):
        sys.exit("invalid choice")

    # Persist the Page account item
    table = _table(config, session)
    key_id = _kms_key_id(config, session)
    kms = session.client("kms")

    page_token_ct = kms.encrypt(KeyId=key_id, Plaintext=page["access_token"].encode())[
        "CiphertextBlob"
    ]
    user_token_ct = kms.encrypt(KeyId=key_id, Plaintext=user_token.encode())["CiphertextBlob"]

    table.put_item(
        Item={
            "PK": f"ACCOUNT#{alias}",
            "SK": "META",
            "provider": "facebook",
            "account_id": page["id"],
            "display_name": page["name"],
            "scopes": scopes,
            "page_token_ct": page_token_ct,
            "user_token_ct": user_token_ct,
            "status": "active",
            "linked_ig_account": "",
        }
    )
    print(f"account '{alias}' connected: {page['name']} [{page['id']}]")

    # Offer to connect any linked IG business account too
    ig = page.get("instagram_business_account", {}).get("id", "")
    if ig:
        answer = (
            input(
                f"Page has a linked Instagram account ({ig}). Register it as a "
                "separate alias? [y/N]: "
            )
            .strip()
            .lower()
        )
        if answer in {"y", "yes"}:
            ig_alias = input("Alias for the IG account (e.g. 'main-page-ig'): ").strip()
            if not ig_alias:
                print("skipping Instagram")
            else:
                table.put_item(
                    Item={
                        "PK": f"ACCOUNT#{ig_alias}",
                        "SK": "META",
                        "provider": "instagram",
                        "account_id": ig,
                        "display_name": f"{page['name']} (Instagram)",
                        # The Page token also drives IG publishing endpoints
                        "scopes": scopes,
                        "page_token_ct": page_token_ct,
                        "status": "active",
                        "linked_page": alias,
                    }
                )
                # Back-reference on the Page item
                table.update_item(
                    Key={"PK": f"ACCOUNT#{alias}", "SK": "META"},
                    UpdateExpression="SET linked_ig_account = :a",
                    ExpressionAttributeValues={":a": ig_alias},
                )
                print(
                    f"account '{ig_alias}' connected (Instagram); add it to "
                    f"config/{config['environment_name']}.json's accounts list "
                    "before the next deploy so the server loads it in the roster"
                )


def _kms_key_id(config: dict, session) -> str:
    """Resolve the token-key alias to a key id."""
    env_project = f"{config['environment_name']}-{config['project_name']}"
    alias = f"alias/{env_project}-tokens"
    return session.client("kms").describe_key(KeyId=alias)["KeyMetadata"]["KeyId"]


def list_accounts_cli(config: dict, session) -> None:
    table = _table(config, session)
    for entry in config.get("accounts", []):
        alias = entry["alias"]
        item = table.get_item(Key={"PK": f"ACCOUNT#{alias}", "SK": "META"}).get("Item")
        if item is None:
            print(f"{alias} ({entry['provider']}): not connected")
            continue
        scopes = ", ".join(item.get("scopes", []))
        print(
            f"{alias} ({item['provider']}): {item.get('status', 'active')} "
            f"name={item.get('display_name', '')} id={item.get('account_id', '')} "
            f"scopes=[{scopes}]"
        )


def _not_yet_implemented(name: str) -> None:
    print(f"'{name}' is not yet implemented in M1; lands with M2 (publishing).")


# ----------------------------------- main -----------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("connect", "list-accounts", "approve", "reject", "disable-publishing", "audit"):
        sp = sub.add_parser(name)
        sp.add_argument("--env", required=True)
        sp.add_argument("--profile", default=None)
        if name == "connect":
            sp.add_argument("--alias", required=True)
        if name in ("approve", "reject"):
            sp.add_argument("--draft-id", required=True)
        if name == "audit":
            sp.add_argument("--account", required=False, default="")

    args = parser.parse_args()
    config = _load_config(args.env)
    session = _session(args.profile, config.get("region", "us-east-1"))

    if args.command == "connect":
        connect(config, session, args.alias)
    elif args.command == "list-accounts":
        list_accounts_cli(config, session)
    else:
        _not_yet_implemented(args.command)


if __name__ == "__main__":
    main()
