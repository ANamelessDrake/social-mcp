#!/usr/bin/env python3
"""Operator CLI for a social-mcp deployment.

    # Connect a Page (and its linked IG account) once, up-front
    python scripts/admin.py connect --env dev --alias main-page \\
        [--scopes publish|engagement]

    # Inspect
    python scripts/admin.py list-accounts --env dev
    python scripts/admin.py list-drafts --env dev [--account main-page]
    python scripts/admin.py show --env dev --draft-id <id>
    python scripts/admin.py audit --env dev [--account main-page] [--since 2026-10-10]

    # Approve / reject a draft (manual publish_mode); only path that writes
    # an approval record for `publish_draft`.
    python scripts/admin.py approve --env dev --draft-id <id>
    python scripts/admin.py reject --env dev --kind draft --draft-id <id>

    # M3: per-item approvals for the live-touching verbs. edit, delete,
    # reply approvals carry the operator-approved payload hash; hide is
    # a standing per-account approval that covers queued hide requests
    # and future ones on the same account.
    python scripts/admin.py approve-edit --env dev --post-id <id>
    python scripts/admin.py approve-delete --env dev --post-id <id>
    python scripts/admin.py approve-reply --env dev --comment-id <id>
    python scripts/admin.py approve-hide --env dev --account main-page [--revoke]

    python scripts/admin.py reject --env dev --kind edit --post-id <id>
    python scripts/admin.py reject --env dev --kind delete --post-id <id>
    python scripts/admin.py reject --env dev --kind reply --comment-id <id>
    python scripts/admin.py reject --env dev --kind hide-request \\
        --account main-page --comment-id <id>

    # Global kill switch (DESIGN.md section 7.6); disables publishing on
    # every account in the deployment regardless of per-account publish_mode.
    python scripts/admin.py disable-publishing --env dev
    python scripts/admin.py disable-publishing --env dev --enable

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
import getpass
import hashlib
import hmac
import json
import secrets as pysecrets
import socket
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
# Add sharedModules to sys.path so admin.py reuses the server's hashing
# and canonical JSON implementation. One source of truth for the hash.
sys.path.insert(0, str(ROOT / "infrastructure" / "lambdaFunctions"))

GRAPH_API_VERSION = "v26.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
FB_AUTH_URL = "https://www.facebook.com/{v}/dialog/oauth".format(v=GRAPH_API_VERSION)

# Scopes bundle per capability tier. One Facebook Login consent covers both
# the Pages product and the Instagram Platform product, so the IG scopes
# ride on every tier (M4 reads need `instagram_basic` +
# `instagram_manage_insights`; comments on IG need `instagram_manage_comments`
# which lands on the engagement tier alongside the Page-side equivalent).
PAGE_SCOPES = {
    "readonly": [
        "pages_show_list",
        "pages_read_engagement",
        "pages_read_user_content",
        "read_insights",
        "business_management",
        "public_profile",
        # M4 IG reads (one Facebook consent dialog covers both products):
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
        # M2 additions:
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
        # M3 additions (Pages side) plus the IG-side comment write scope:
        "pages_manage_engagement",
        "instagram_manage_comments",
    ],
    "publish-ig": [
        # M5 Instagram publishing tier. Carries everything `publish` has
        # plus the IG publishing scope; one consent dialog covers Pages
        # and Instagram.
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
        # Everything in `publish-ig` plus the scope IG added for media
        # deletes on 2025-12-03. Narrow tier so operators who do not
        # ever delete IG media never grant it.
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
    scopes = PAGE_SCOPES.get(_SCOPE_CHOICE, PAGE_SCOPES["readonly"])

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


# -------------------- M6 hosted callback (connect --remote) --------------------


def _oauth_callback_url(config: dict, session) -> str:
    """Resolve the OauthCallbackEndpoint stack output.

    Config may override via `oauth_callback_url`; otherwise we read the
    CloudFormation output of the lambda stack. One API call per connect,
    which is fine at CLI speed.
    """
    if config.get("oauth_callback_url"):
        return config["oauth_callback_url"].rstrip("/")
    env_project = f"{config['environment_name']}-{config['project_name']}"
    stack_name = f"{env_project}-lambda"
    cfn = session.client("cloudformation")
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
    except Exception as exc:  # noqa: BLE001
        sys.exit(
            f"could not read stack '{stack_name}' to find the OauthCallbackEndpoint "
            f"output: {exc}. Deploy the lambda stack first, or set oauth_callback_url "
            f"in config/{config['environment_name']}.json."
        )
    for stack in resp.get("Stacks", []):
        for output in stack.get("Outputs", []):
            if output.get("OutputKey") == "OauthCallbackEndpoint":
                return output["OutputValue"].rstrip("/")
    sys.exit(
        "OauthCallbackEndpoint not found in the lambda stack outputs; redeploy to pick it up."
    )


def connect_remote(
    config: dict,
    session,
    alias: str,
    *,
    scopes_tier: str,
    page_id: str,
    ig_alias: str,
) -> None:
    entries = {a["alias"]: a for a in config.get("accounts", [])}
    if alias not in entries:
        sys.exit(
            f"account '{alias}' is not in config/{config['environment_name']}.json; add it first"
        )

    prefix = _secret_prefix(config)
    creds = _meta_app_creds(session, prefix)
    scopes = PAGE_SCOPES.get(scopes_tier, PAGE_SCOPES["readonly"])

    callback = _oauth_callback_url(config, session)
    state = pysecrets.token_urlsafe(16)
    operator = getpass.getuser()

    table = _table(config, session)
    expires_at = int(time.time()) + 900
    table.put_item(
        Item={
            "PK": f"CONNECT_STATE#{state}",
            "SK": "META",
            "status": "pending",
            "alias_prefix": alias,
            "scopes_tier": scopes_tier,
            "operator_identity": operator,
            "page_id": page_id,
            "ig_alias": ig_alias,
            "requested_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "message": "",
            "aliases": [],
            "ttl": expires_at,
        }
    )

    consent_params = {
        "client_id": creds["app_id"],
        "redirect_uri": callback,
        "response_type": "code",
        "scope": ",".join(scopes),
        "state": state,
    }
    consent_url = f"{FB_AUTH_URL}?{urllib.parse.urlencode(consent_params)}"

    print("Open this URL in a browser (any device) to authorize:")
    print(f"  {consent_url}\n")
    print(
        "The hosted callback writes the ACCOUNT item once the browser returns.\n"
        "Make sure the Meta app's Valid OAuth Redirect URIs includes:\n"
        f"  {callback}\n"
    )
    print(f"Polling state '{state}' for up to 10 minutes...")

    deadline = time.time() + 600
    poll_interval = 2
    status = "pending"
    while time.time() < deadline:
        item = table.get_item(Key={"PK": f"CONNECT_STATE#{state}", "SK": "META"}).get("Item")
        if item is None:
            # TTL could clear it mid-flight if the operator is very slow;
            # treat the missing row as a terminal timeout.
            sys.exit("state record expired before completion")
        status = item.get("status", "pending")
        if status == "completed":
            aliases = item.get("aliases", [])
            print(f"connected: {', '.join(aliases) if aliases else '(none)'}")
            if any(a == ig_alias for a in aliases):
                print(
                    f"add '{ig_alias}' to config/{config['environment_name']}.json's "
                    "accounts list before the next deploy"
                )
            return
        if status == "error":
            sys.exit(f"authorization failed: {item.get('message', 'unknown')}")
        time.sleep(poll_interval)

    sys.exit("no completion recorded; the state record has expired")


def list_accounts_cli(config: dict, session) -> None:
    table = _table(config, session)
    for entry in config.get("accounts", []):
        alias = entry["alias"]
        item = table.get_item(Key={"PK": f"ACCOUNT#{alias}", "SK": "META"}).get("Item")
        mode = entry.get("publish_mode", "manual")
        if item is None:
            print(f"{alias} ({entry['provider']}, mode={mode}): not connected")
            continue
        scopes = ", ".join(item.get("scopes", []))
        print(
            f"{alias} ({item['provider']}, mode={mode}): {item.get('status', 'active')} "
            f"name={item.get('display_name', '')} id={item.get('account_id', '')} "
            f"scopes=[{scopes}]"
        )


# -------------------------------- M2 subcommands --------------------------------


def _canonical_hash(draft: dict) -> str:
    """Mirror sharedModules/approval.py's hash_draft for the operator CLI.

    Imported from the server package when available; this fallback keeps
    the CLI runnable without the full lambda bundle on sys.path.
    """
    try:
        from types import SimpleNamespace

        from sharedModules.approval import hash_draft  # type: ignore

        return hash_draft(
            SimpleNamespace(
                account=draft.get("account", ""),
                kind=draft.get("kind", ""),
                text=draft.get("text", "") or "",
                link=draft.get("link", "") or "",
                media=draft.get("media", []) or [],
                schedule=draft.get("schedule", "") or "",
            )
        )
    except Exception:  # noqa: BLE001 - deliberate narrow fallback
        payload = {
            "account": draft.get("account", ""),
            "kind": draft.get("kind", ""),
            "text": draft.get("text", "") or "",
            "link": draft.get("link", "") or "",
            "media": [
                {"etag": m.get("etag", ""), "mime": m.get("mime", "")}
                for m in (draft.get("media") or [])
            ],
            "schedule": draft.get("schedule", "") or "",
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        return hashlib.sha256(blob).hexdigest()


def _actor() -> str:
    return f"{getpass.getuser()}@{socket.gethostname()}"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read_draft(table, draft_id: str) -> dict | None:
    item = table.get_item(Key={"PK": f"DRAFT#{draft_id}", "SK": "META"}).get("Item")
    if item is None:
        return None
    return {k: v for k, v in item.items() if k not in {"PK", "SK", "GSI1PK", "GSI1SK"}}


def _write_draft(table, draft: dict) -> None:
    account = draft.get("account", "unknown")
    created_at = draft.get("created_at") or _now()
    status = draft.get("status", "drafted")
    table.put_item(
        Item={
            "PK": f"DRAFT#{draft['draft_id']}",
            "SK": "META",
            "GSI1PK": f"DRAFTS#{account}",
            "GSI1SK": f"CREATED#{created_at}#{status}",
            **draft,
        }
    )


def _write_approval(table, draft_id: str, content_hash: str, actor: str, scope: str) -> None:
    table.put_item(
        Item={
            "PK": f"APPROVAL#{draft_id}",
            "SK": "META",
            "draft_id": draft_id,
            "content_hash": content_hash,
            "approved_by": actor,
            "approved_at": _now(),
            "scope": scope,
        }
    )


def _delete_approval(table, draft_id: str) -> None:
    table.delete_item(Key={"PK": f"APPROVAL#{draft_id}", "SK": "META"})


def _append_audit(table, row: dict) -> None:
    account = row.get("account", "unknown")
    ts = _now()
    sk = f"TS#{ts}#{pysecrets.token_hex(4)}"
    table.put_item(Item={"PK": f"AUDIT#{account}", "SK": sk, "created_at": ts, **row})


def _mode_for(config: dict, account: str) -> tuple[str, list[str]]:
    for entry in config.get("accounts", []):
        if entry["alias"] == account:
            return entry.get("publish_mode", "manual"), list(entry.get("auto_for_kinds", []))
    return "manual", []


def _show_preview(draft: dict, config: dict) -> None:
    mode, auto_kinds = _mode_for(config, draft.get("account", ""))
    print(f"draft:   {draft.get('draft_id', '')}")
    print(f"account: {draft.get('account', '')}  (publish_mode={mode})")
    print(f"kind:    {draft.get('kind', '')}")
    print(f"status:  {draft.get('status', '')}")
    print(f"created: {draft.get('created_at', '')} by {draft.get('created_by', '')}")
    schedule = draft.get("schedule") or ""
    if schedule:
        print(f"schedule: {schedule}")
    link = draft.get("link") or ""
    if link:
        print(f"link:    {link}")
    media = draft.get("media") or []
    if media:
        print(f"media:   {len(media)} item(s)")
    print("------- text -------")
    print(draft.get("text", ""))
    print("--------------------")
    warnings = draft.get("policy_warnings") or []
    if warnings:
        print("policy warnings:")
        for w in warnings:
            print(f"  - {w}")
    if mode == "auto":
        print("note: this account is in auto mode; approvals are recorded by the server.")
    elif mode == "auto_for":
        print(f"note: this account auto-approves kinds={auto_kinds}; others need manual approval.")


def approve_cli(config: dict, session, draft_id: str) -> None:
    table = _table(config, session)
    draft = _read_draft(table, draft_id)
    if draft is None:
        sys.exit(f"draft '{draft_id}' not found")

    if draft.get("status") in ("published", "discarded"):
        sys.exit(f"draft '{draft_id}' is {draft['status']}; cannot approve")

    mode, auto_kinds = _mode_for(config, draft.get("account", ""))
    kind = draft.get("kind", "")
    if mode == "auto" or (mode == "auto_for" and kind in auto_kinds):
        sys.exit(
            f"account '{draft.get('account', '')}' auto-approves {kind} drafts; "
            "an approval record was already written at draft time. If this draft "
            "shows status='drafted' here, something is wrong - inspect it."
        )

    _show_preview(draft, config)
    current = _canonical_hash(draft)
    print(f"content_hash: {current}")
    choice = input("\nApprove this draft for publishing? [y/N]: ").strip().lower()
    if choice not in {"y", "yes"}:
        print("not approved")
        return

    actor = _actor()
    _write_approval(table, draft_id, current, actor, scope="publish")
    _write_draft(table, {**draft, "status": "approved"})
    _append_audit(
        table,
        {
            "account": draft.get("account", "unknown"),
            "kind": "approve",
            "actor_identity": actor,
            "draft_id": draft_id,
            "result": "ok",
        },
    )
    print(f"approved by {actor}")


def reject_cli(config: dict, session, draft_id: str) -> None:
    table = _table(config, session)
    draft = _read_draft(table, draft_id)
    if draft is None:
        sys.exit(f"draft '{draft_id}' not found")
    if draft.get("status") == "published":
        sys.exit(f"draft '{draft_id}' is already published; cannot reject")
    _show_preview(draft, config)
    choice = input("\nReject (discard) this draft? [y/N]: ").strip().lower()
    if choice not in {"y", "yes"}:
        print("not rejected")
        return
    actor = _actor()
    _delete_approval(table, draft_id)
    _write_draft(table, {**draft, "status": "discarded"})
    _append_audit(
        table,
        {
            "account": draft.get("account", "unknown"),
            "kind": "reject",
            "actor_identity": actor,
            "draft_id": draft_id,
            "result": "ok",
        },
    )
    print(f"rejected by {actor}")


def list_drafts_cli(config: dict, session, account: str = "") -> None:
    table = _table(config, session)
    aliases = [account] if account else [a["alias"] for a in config.get("accounts", [])]
    for alias in aliases:
        resp = table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :pk",
            ExpressionAttributeValues={":pk": f"DRAFTS#{alias}"},
            ScanIndexForward=False,
            Limit=20,
        )
        for item in resp.get("Items", []):
            text = (item.get("text", "") or "")[:60]
            print(
                f"[{alias}] {item.get('draft_id', '')} status={item.get('status', '')} "
                f"kind={item.get('kind', '')} created={item.get('created_at', '')} "
                f"text={text!r}"
            )


def show_cli(config: dict, session, draft_id: str) -> None:
    table = _table(config, session)
    draft = _read_draft(table, draft_id)
    if draft is None:
        sys.exit(f"draft '{draft_id}' not found")
    _show_preview(draft, config)
    approval = table.get_item(Key={"PK": f"APPROVAL#{draft_id}", "SK": "META"}).get("Item")
    if approval is None:
        print("approval: none (pending)")
    else:
        current = _canonical_hash(draft)
        stored = approval.get("content_hash", "")
        state = (
            "approved"
            if hmac.compare_digest(current, stored)
            else "STALE (edited after approval)"
        )
        who = approval.get("approved_by", "")
        when = approval.get("approved_at", "")
        print(f"approval: {state} by {who} at {when}")


def disable_publishing_cli(config: dict, session, enable: bool) -> None:
    table = _table(config, session)
    actor = _actor()
    table.put_item(
        Item={
            "PK": "KILL#",
            "SK": "global",
            "disabled": not enable,
            "set_by": actor,
            "set_at": _now(),
        }
    )
    state = "ENABLED" if enable else "DISABLED"
    print(f"publishing globally {state} (set_by={actor})")


# -------------------------------- M3 subcommands --------------------------------


def _publisher_function_name(config: dict) -> str:
    return f"{config['environment_name']}-{config['project_name']}-publisher"


def _invoke_publisher(config: dict, session, payload: dict) -> dict:
    """Fire the Publisher Lambda from the operator machine. Admin CLI only."""
    client = session.client("lambda")
    name = _publisher_function_name(config)
    resp = client.invoke(
        FunctionName=name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode(),
    )
    body = resp.get("Payload").read().decode() if resp.get("Payload") else "{}"
    try:
        return json.loads(body)
    except ValueError:
        return {"ok": False, "error": "publisher returned invalid JSON"}


def _read_staged_edit(table, post_id: str) -> dict | None:
    item = table.get_item(Key={"PK": f"EDIT#{post_id}", "SK": "META"}).get("Item")
    if item is None:
        return None
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def _read_staged_delete(table, post_id: str) -> dict | None:
    item = table.get_item(Key={"PK": f"DELETE#{post_id}", "SK": "META"}).get("Item")
    if item is None:
        return None
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def _read_staged_reply(table, comment_id: str) -> dict | None:
    item = table.get_item(Key={"PK": f"REPLY#{comment_id}", "SK": "META"}).get("Item")
    if item is None:
        return None
    return {k: v for k, v in item.items() if k not in {"PK", "SK"}}


def _hash_edit(text: str) -> str:
    blob = json.dumps({"text": text or ""}, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()


def _hash_delete(post_id: str, reason: str) -> str:
    payload = {"post_id": post_id or "", "reason": reason or ""}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(blob).hexdigest()


def _hash_reply(text: str) -> str:
    return _hash_edit(text)


def _write_item_approval(
    table, *, key: str, content_hash: str, scope: str, actor: str, payload: dict | None = None
) -> None:
    item = {
        "PK": f"APPROVAL#{key}",
        "SK": "META",
        "approval_key": key,
        "content_hash": content_hash,
        "approved_by": actor,
        "approved_at": _now(),
        "scope": scope,
    }
    if payload:
        item["payload"] = payload
    table.put_item(Item=item)


def approve_edit_cli(config: dict, session, post_id: str) -> None:
    table = _table(config, session)
    staged = _read_staged_edit(table, post_id)
    if staged is None:
        sys.exit(
            f"no staged edit for post '{post_id}'; "
            "the agent must call edit_post first"
        )
    text = staged.get("text", "") or ""
    account = staged.get("account", "")
    print(f"account: {account}")
    print(f"post:    {post_id}")
    print("------- new text -------")
    print(text)
    print("------------------------")
    choice = input("\nApprove this edit? [y/N]: ").strip().lower()
    if choice not in {"y", "yes"}:
        print("not approved")
        return
    actor = _actor()
    content_hash = _hash_edit(text)
    _write_item_approval(
        table,
        key=f"edit:{post_id}",
        content_hash=content_hash,
        scope=f"edit:{post_id}",
        actor=actor,
    )
    result = _invoke_publisher(
        config, session, {"kind": "edit", "post_id": post_id, "actor": actor}
    )
    print(f"publisher: {json.dumps(result)}")


def approve_delete_cli(config: dict, session, post_id: str) -> None:
    table = _table(config, session)
    staged = _read_staged_delete(table, post_id)
    if staged is None:
        sys.exit(
            f"no staged delete for post '{post_id}'; "
            "the agent must call delete_post first"
        )
    account = staged.get("account", "")
    reason = staged.get("reason", "") or ""
    print(f"account: {account}")
    print(f"post:    {post_id}")
    if reason:
        print(f"reason:  {reason}")
    choice = input("\nApprove this delete? [y/N]: ").strip().lower()
    if choice not in {"y", "yes"}:
        print("not approved")
        return
    actor = _actor()
    content_hash = _hash_delete(post_id, reason)
    _write_item_approval(
        table,
        key=f"delete:{post_id}",
        content_hash=content_hash,
        scope=f"delete:{post_id}",
        actor=actor,
        payload={"reason": reason},
    )
    result = _invoke_publisher(
        config, session, {"kind": "delete", "post_id": post_id, "actor": actor}
    )
    print(f"publisher: {json.dumps(result)}")


def approve_reply_cli(config: dict, session, comment_id: str) -> None:
    table = _table(config, session)
    staged = _read_staged_reply(table, comment_id)
    if staged is None:
        sys.exit(
            f"no staged reply for comment '{comment_id}'; "
            "the agent must call reply_to_comment first"
        )
    text = staged.get("text", "") or ""
    account = staged.get("account", "")
    print(f"account:   {account}")
    print(f"replying to comment: {comment_id}")
    print("------- reply text -------")
    print(text)
    print("--------------------------")
    choice = input("\nApprove this reply? [y/N]: ").strip().lower()
    if choice not in {"y", "yes"}:
        print("not approved")
        return
    actor = _actor()
    content_hash = _hash_reply(text)
    _write_item_approval(
        table,
        key=f"reply:{comment_id}",
        content_hash=content_hash,
        scope=f"reply:{comment_id}",
        actor=actor,
    )
    result = _invoke_publisher(
        config, session, {"kind": "reply", "comment_id": comment_id, "actor": actor}
    )
    print(f"publisher: {json.dumps(result)}")


def approve_hide_cli(config: dict, session, account: str, revoke: bool) -> None:
    """Standing per-account hide approval.

    Without --revoke: writes the HIDE#<account> item, then sweeps every
    queued HIDE_REQUEST#<account> item (one Publisher invoke per
    comment, one audit row per hide).

    With --revoke: deletes the standing approval. Queued requests stay
    queued; future hide_comment calls go back to the "needs approval"
    path.
    """
    table = _table(config, session)
    actor = _actor()
    if revoke:
        table.delete_item(Key={"PK": f"HIDE#{account}", "SK": "META"})
        print(f"standing hide approval revoked for '{account}' by {actor}")
        return
    table.put_item(
        Item={
            "PK": f"HIDE#{account}",
            "SK": "META",
            "account": account,
            "approved_by": actor,
            "approved_at": _now(),
        }
    )
    print(f"standing hide approval granted for '{account}' by {actor}")

    # Sweep queued requests
    resp = table.query(
        KeyConditionExpression="PK = :pk",
        ExpressionAttributeValues={":pk": f"HIDE_REQUEST#{account}"},
    )
    pending = resp.get("Items", [])
    if not pending:
        print("no queued hide requests")
        return
    print(f"processing {len(pending)} queued hide request(s)...")
    for req in pending:
        comment_id = req.get("comment_id") or req.get("SK", "")
        result = _invoke_publisher(
            config,
            session,
            {"kind": "hide", "account": account, "comment_id": comment_id, "actor": actor},
        )
        print(f"  {comment_id}: {result.get('ok', False)} {result.get('error', '')}")


def reject_cli_multi(config: dict, session, kind: str, args) -> None:
    """Reject any staged change by kind: draft | edit | delete | reply | hide-request."""
    table = _table(config, session)
    actor = _actor()
    if kind == "draft":
        return reject_cli(config, session, args.draft_id)
    if kind == "edit":
        post_id = args.post_id
        if not post_id:
            sys.exit("--post-id is required for `reject --kind edit`")
        table.delete_item(Key={"PK": f"EDIT#{post_id}", "SK": "META"})
        table.delete_item(Key={"PK": f"APPROVAL#edit:{post_id}", "SK": "META"})
        _append_audit(
            table,
            {
                "account": "unknown",
                "kind": "edit",
                "actor_identity": actor,
                "post_id": post_id,
                "result": "rejected",
            },
        )
        print(f"rejected staged edit for post '{post_id}' by {actor}")
        return
    if kind == "delete":
        post_id = args.post_id
        if not post_id:
            sys.exit("--post-id is required for `reject --kind delete`")
        table.delete_item(Key={"PK": f"DELETE#{post_id}", "SK": "META"})
        table.delete_item(Key={"PK": f"APPROVAL#delete:{post_id}", "SK": "META"})
        _append_audit(
            table,
            {
                "account": "unknown",
                "kind": "delete",
                "actor_identity": actor,
                "post_id": post_id,
                "result": "rejected",
            },
        )
        print(f"rejected staged delete for post '{post_id}' by {actor}")
        return
    if kind == "reply":
        comment_id = args.comment_id
        if not comment_id:
            sys.exit("--comment-id is required for `reject --kind reply`")
        table.delete_item(Key={"PK": f"REPLY#{comment_id}", "SK": "META"})
        table.delete_item(Key={"PK": f"APPROVAL#reply:{comment_id}", "SK": "META"})
        _append_audit(
            table,
            {
                "account": "unknown",
                "kind": "reply",
                "actor_identity": actor,
                "comment_id": comment_id,
                "result": "rejected",
            },
        )
        print(f"rejected staged reply for comment '{comment_id}' by {actor}")
        return
    if kind == "hide-request":
        account = args.account
        comment_id = args.comment_id
        if not (account and comment_id):
            sys.exit(
                "--account and --comment-id are required for `reject --kind hide-request`"
            )
        table.delete_item(Key={"PK": f"HIDE_REQUEST#{account}", "SK": comment_id})
        _append_audit(
            table,
            {
                "account": account,
                "kind": "hide",
                "actor_identity": actor,
                "comment_id": comment_id,
                "result": "rejected",
            },
        )
        print(f"rejected queued hide request for '{account}' / '{comment_id}' by {actor}")
        return
    sys.exit(f"unknown reject kind: {kind}")


def audit_cli(config: dict, session, account: str, since: str, limit: int) -> None:
    table = _table(config, session)
    if account:
        kwargs = {
            "KeyConditionExpression": "PK = :pk",
            "ExpressionAttributeValues": {":pk": f"AUDIT#{account}"},
            "ScanIndexForward": False,
            "Limit": max(1, min(limit, 500)),
        }
        if since:
            kwargs["KeyConditionExpression"] += " AND SK >= :sk"
            kwargs["ExpressionAttributeValues"][":sk"] = f"TS#{since}"
        resp = table.query(**kwargs)
        rows = resp.get("Items", [])
    else:
        resp = table.scan(
            FilterExpression="begins_with(PK, :p)",
            ExpressionAttributeValues={":p": "AUDIT#"},
            Limit=limit * 4,
        )
        rows = sorted(
            resp.get("Items", []), key=lambda r: r.get("created_at", ""), reverse=True
        )[:limit]
    for row in rows:
        print(
            f"{row.get('created_at', '')} [{row.get('account', '')}] "
            f"{row.get('kind', '')} by {row.get('actor_identity', '')} "
            f"draft={row.get('draft_id', '')} result={row.get('result', '')}"
        )


# -------------------- M7 webhook subscription management --------------------
#
# Meta's `/subscribed_apps` edges tell a Page or an IG user to deliver its
# comment events to the app's registered Webhook callback. One call per
# account registers it; one DELETE unsubscribes.


def _subscribe_edge(provider: str, account_id: str, token: str) -> dict:
    import urllib.parse
    import urllib.request

    base = "https://graph.facebook.com/v26.0"
    if provider == "instagram":
        fields = "comments"
        url = f"{base}/{account_id}/subscribed_apps"
    else:
        fields = "feed,comments"
        url = f"{base}/{account_id}/subscribed_apps"
    data = urllib.parse.urlencode(
        {"subscribed_fields": fields, "access_token": token}
    ).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _unsubscribe_edge(account_id: str, token: str) -> dict:
    import urllib.parse
    import urllib.request

    base = "https://graph.facebook.com/v26.0"
    url = (
        f"{base}/{account_id}/subscribed_apps?"
        + urllib.parse.urlencode({"access_token": token})
    )
    req = urllib.request.Request(url, method="DELETE")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _read_edge(account_id: str, token: str) -> dict:
    import urllib.parse
    import urllib.request

    base = "https://graph.facebook.com/v26.0"
    url = (
        f"{base}/{account_id}/subscribed_apps?"
        + urllib.parse.urlencode({"access_token": token})
    )
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _decrypt_page_token(session, token_ct, kms_key_id: str) -> str:
    kms = session.client("kms")
    resp = kms.decrypt(KeyId=kms_key_id, CiphertextBlob=bytes(token_ct))
    return resp["Plaintext"].decode()


def _lookup_account(table, alias: str) -> dict:
    item = table.get_item(Key={"PK": f"ACCOUNT#{alias}", "SK": "META"}).get("Item")
    if item is None:
        sys.exit(f"account '{alias}' not found; run `connect` first")
    return item


def webhook_subscribe_cli(config: dict, session, alias: str) -> None:
    table = _table(config, session)
    account = _lookup_account(table, alias)
    token = _decrypt_page_token(
        session, account["page_token_ct"], _kms_key_id(config, session)
    )
    result = _subscribe_edge(account["provider"], account["account_id"], token)
    print(f"subscribed {alias}: {result}")


def webhook_unsubscribe_cli(config: dict, session, alias: str) -> None:
    table = _table(config, session)
    account = _lookup_account(table, alias)
    token = _decrypt_page_token(
        session, account["page_token_ct"], _kms_key_id(config, session)
    )
    result = _unsubscribe_edge(account["account_id"], token)
    print(f"unsubscribed {alias}: {result}")


def webhook_show_cli(config: dict, session) -> None:
    table = _table(config, session)
    resp = table.scan(
        FilterExpression="begins_with(PK, :p)",
        ExpressionAttributeValues={":p": "ACCOUNT#"},
    )
    kms_key = _kms_key_id(config, session)
    for item in resp.get("Items", []):
        alias = item["PK"].split("#", 1)[1]
        token_ct = item.get("page_token_ct")
        if token_ct is None:
            print(f"{alias}: no token stored")
            continue
        try:
            token = _decrypt_page_token(session, token_ct, kms_key)
            info = _read_edge(item["account_id"], token)
            data = (info or {}).get("data") or []
            subscribed = bool(data)
            fields = (
                ",".join(data[0].get("subscribed_fields", [])) if data else ""
            )
            print(f"{alias}: subscribed={subscribed} fields={fields}")
        except Exception as exc:  # noqa: BLE001
            print(f"{alias}: error reading subscription state: {exc}")


# ----------------------------------- main -----------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in (
        "connect",
        "list-accounts",
        "list-drafts",
        "show",
        "approve",
        "reject",
        "approve-edit",
        "approve-delete",
        "approve-reply",
        "approve-hide",
        "disable-publishing",
        "audit",
        "webhook-subscribe",
        "webhook-unsubscribe",
        "webhook-show",
    ):
        sp = sub.add_parser(name)
        sp.add_argument("--env", required=True)
        sp.add_argument("--profile", default=None)
        if name == "connect":
            sp.add_argument("--alias", required=True)
            sp.add_argument("--scopes", default="readonly", choices=sorted(PAGE_SCOPES))
            sp.add_argument(
                "--remote",
                action="store_true",
                help=(
                    "Use the hosted OAuth callback instead of a loopback "
                    "redirect; works from any machine or phone"
                ),
            )
            sp.add_argument(
                "--page-id",
                default="",
                help=(
                    "Required with --remote when the authorizing user "
                    "administers more than one Page"
                ),
            )
            sp.add_argument(
                "--ig-alias",
                default="",
                help="With --remote, also register the linked IG account under this alias",
            )
        if name in ("approve", "show"):
            sp.add_argument("--draft-id", required=True)
        if name == "reject":
            sp.add_argument(
                "--kind",
                default="draft",
                choices=("draft", "edit", "delete", "reply", "hide-request"),
            )
            sp.add_argument("--draft-id", default="")
            sp.add_argument("--post-id", default="")
            sp.add_argument("--comment-id", default="")
            sp.add_argument("--account", default="")
        if name in ("approve-edit", "approve-delete"):
            sp.add_argument("--post-id", required=True)
        if name == "approve-reply":
            sp.add_argument("--comment-id", required=True)
        if name == "approve-hide":
            sp.add_argument("--account", required=True)
            sp.add_argument("--revoke", action="store_true", help="revoke the standing approval")
        if name == "list-drafts":
            sp.add_argument("--account", default="")
        if name == "disable-publishing":
            sp.add_argument("--enable", action="store_true", help="re-enable publishing")
        if name == "audit":
            sp.add_argument("--account", default="")
            sp.add_argument("--since", default="")
            sp.add_argument("--limit", type=int, default=50)
        if name in ("webhook-subscribe", "webhook-unsubscribe"):
            sp.add_argument("--alias", required=True)

    args = parser.parse_args()
    config = _load_config(args.env)
    session = _session(args.profile, config.get("region", "us-east-1"))

    if args.command == "connect":
        # scopes selection only affects the consent dialog; connect() still
        # pulls from PAGE_SCOPES by name.
        global _SCOPE_CHOICE
        _SCOPE_CHOICE = args.scopes
        if args.remote:
            connect_remote(
                config,
                session,
                args.alias,
                scopes_tier=args.scopes,
                page_id=args.page_id,
                ig_alias=args.ig_alias,
            )
        else:
            connect(config, session, args.alias)
    elif args.command == "list-accounts":
        list_accounts_cli(config, session)
    elif args.command == "list-drafts":
        list_drafts_cli(config, session, args.account)
    elif args.command == "show":
        show_cli(config, session, args.draft_id)
    elif args.command == "approve":
        approve_cli(config, session, args.draft_id)
    elif args.command == "reject":
        reject_cli_multi(config, session, args.kind, args)
    elif args.command == "approve-edit":
        approve_edit_cli(config, session, args.post_id)
    elif args.command == "approve-delete":
        approve_delete_cli(config, session, args.post_id)
    elif args.command == "approve-reply":
        approve_reply_cli(config, session, args.comment_id)
    elif args.command == "approve-hide":
        approve_hide_cli(config, session, args.account, args.revoke)
    elif args.command == "disable-publishing":
        disable_publishing_cli(config, session, args.enable)
    elif args.command == "audit":
        audit_cli(config, session, args.account, args.since, args.limit)
    elif args.command == "webhook-subscribe":
        webhook_subscribe_cli(config, session, args.alias)
    elif args.command == "webhook-unsubscribe":
        webhook_unsubscribe_cli(config, session, args.alias)
    elif args.command == "webhook-show":
        webhook_show_cli(config, session)
    else:  # pragma: no cover - argparse guards above
        sys.exit(f"unknown command: {args.command}")


_SCOPE_CHOICE = "readonly"


if __name__ == "__main__":
    main()
