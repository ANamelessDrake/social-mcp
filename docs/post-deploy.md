# Post-deploy runbook

Finish bringing up a fresh `social-mcp` deployment. You should already have:

- A successful `./deployCDK.sh <env> --auto-approve` with all seven stacks
  reporting `CREATE_COMPLETE` or `UPDATE_COMPLETE`.
- An AWS profile that can read Secrets Manager and DynamoDB in the
  deployment account.
- Python 3.12 and this repo's `.venv` ready for `scripts/admin.py` calls.

Everything below is a one-time setup. Day-to-day operation lives in the
admin CLI (`admin.py approve`, `audit`, `disable-publishing`) and the MCP
tools themselves.

This document reflects the Meta app console UX as of 2026-10. Meta's
flow has changed meaningfully twice in the last 24 months; if the
screens you see do not match the sections below, trust the field names
in your Business Login Configuration over the labels here.

## 1. Collect the three Function URLs

```bash
AWS_PROFILE=<profile> aws cloudformation describe-stacks \
  --region <region> \
  --stack-name <env>-socialmcp-lambda \
  --query 'Stacks[0].Outputs' \
  --output table
```

Three outputs matter:

| Output | What uses it |
|---|---|
| `McpEndpoint` | MCP clients (Claude Code, Claude Desktop, WILMA) connect here with the bearer token |
| `WebhookIngestEndpoint` | Register as the Callback URL on your Meta app's Webhook product |
| `OauthCallbackEndpoint` | Register under Facebook Login for Business, and add its hostname to App Domains (both are required, see sections 7 and 8) |

Keep these somewhere you can paste from; the webhook and oauth URLs go into
the Meta app console next.

## 2. Create the Meta app (use-case picker)

The current console does not ask you to pick an app type and add
products. It asks you to pick use cases, and derives the right product
set.

1. developers.facebook.com -> **Create app**.
2. **App details** step: pick a name (e.g. `<project>-social-mcp-prod`).
3. **Use cases** step: tick exactly two.
   - **Manage everything on your Page**
   - **Manage messaging & content on Instagram**

   Do **not** also tick "Authenticate and request data from users with
   Facebook Login". Selecting either business use case greys that one
   out, because the business use cases bundle the login variant our
   OAuth flow needs. Picking it on top causes `admin.py connect` to
   land on the wrong auth tier.
4. **Business** step and **Requirements** step: default through.

The app lands in Development Mode, which is where it stays for
single-tenant use. The authorizing admin (you) is the only user who
needs to grant permissions, so no App Review is required.

## 3. Customize the Manage Pages use case

Open the use case from the left sidebar, then click **Customize** ->
**Permissions and features**.

Enable these six permissions (click Action on each; Standard Access is
enough for Pages the authorizing admin administers):

| Permission | What breaks without it |
|---|---|
| `pages_manage_posts` | `publish_draft`, `edit_post`, `delete_post` |
| `pages_read_engagement` | `list_posts`, `get_post`, Page metadata reads, `get_profile` |
| `pages_read_user_content` | `list_comments` (reading comments users left on your Pages) |
| `pages_manage_engagement` | `reply_to_comment`, `hide_comment` |
| `pages_manage_metadata` | Webhooks subscriptions, `publish_profile_edit` (M9) |
| `read_insights` | `get_insights` for Pages |

`pages_show_list`, `business_management`, and `public_profile` show
"Ready for testing" automatically; they came with the use case, no
action needed. Everything else on the page (`email`, `Live Video API`,
`Page Mentions`, the `facebook_branded_content_*` family) is unused by
social-mcp; skip.

## 4. Customize the Instagram use case

Open the Instagram use case from the sidebar and click **Customize**.
The **critical first step** is picking the right variant.

The default landing page is **"Instagram API with Instagram Login"**.
Its welcome blurb lists `instagram_business_basic`,
`instagram_business_manage_comments`, and
`instagram_business_manage_messages` as required. This variant cannot
do content publish and cannot do insights. It is not what social-mcp
uses.

Click the **"switch to the API setup with Facebook login"** link in
that welcome blurb. The required-permissions block changes to list
`instagram_basic`, `instagram_content_publish`,
`pages_read_engagement`, `business_management`, `pages_show_list`.
That is the Facebook-login variant and the one our provider targets.

Now click **Permissions and features** and enable these three (the
other two Instagram scopes are auto-granted on this variant):

| Permission | What breaks without it |
|---|---|
| `instagram_manage_comments` | `list_comments`, `reply_to_comment`, `hide_comment` on IG |
| `instagram_manage_insights` | `get_insights` on IG posts |
| `instagram_manage_contents` | `delete_post` on IG (Meta split this off from publish in late 2025) |

Explicitly **skip** the entire `instagram_business_*` family. Those
scopes grant access through a direct IG Business access token; our
provider calls IG through the connected Facebook Page token on the
legacy `instagram_*` scopes. Enabling the business-tier scopes does
not help and clutters the consent dialog.

Also skip: `instagram_manage_messages` (DMs are not supported here),
`instagram_manage_engagement` (likes), `instagram_manage_upcoming_events`,
`instagram_shopping_tag_products`, the `instagram_branded_content_*`
family, the `instagram_creator_marketplace_*` family, and
`Instagram Public Content Access` (hashtag search).

## 5. Populate the Meta app secret

The `secretsStack` created an empty shell for your app's id/secret.
Grab the App ID and App Secret from the Meta app's **Settings ->
Basic** page, then:

```bash
AWS_PROFILE=<profile> aws secretsmanager put-secret-value \
  --region <region> \
  --secret-id <Env>-SocialMcp-MetaApp \
  --secret-string '{"app_id": "<APP_ID>", "app_secret": "<APP_SECRET>"}'
```

The bearer token (`<Env>-SocialMcp-BearerToken`) and the webhook verify
token (`<Env>-SocialMcp-WebhookVerifyToken`) are auto-generated at
deploy time; do not overwrite them unless you are rotating.

## 6. Fetch the bearer token and point a client at the MCP endpoint

```bash
AWS_PROFILE=<profile> aws secretsmanager get-secret-value \
  --region <region> \
  --secret-id <Env>-SocialMcp-BearerToken \
  --query SecretString --output text
```

Drop that into an MCP client config. For Claude Code, create a
`.mcp.json` anywhere (template at
[`../clients/examples/claude-code.mcp.json`](../clients/examples/claude-code.mcp.json)):

```json
{
  "mcpServers": {
    "social": {
      "type": "http",
      "url": "<McpEndpoint>",
      "headers": { "Authorization": "Bearer <bearer-token>" }
    }
  }
}
```

If `allowed_client_hostnames` is set in your `config/<env>.json`, the
server only accepts requests from source IPs resolving to those
hostnames. Agents on your network see the server; agents on anything
else get a 401 before the token check runs.

## 7. Register OAuth redirect URIs

**Products -> Facebook Login for Business -> Settings.**

Register only one Valid OAuth Redirect URI:

```
<OauthCallbackEndpoint>
```

Do **not** also register `http://127.0.0.1/`. Meta Business apps with
Enforce HTTPS on reject loopback; the "loopback exception" that
existed on the older consumer Facebook Login flow is unreliable here
and Strict Mode makes the port-flexibility rule fragile. The remote
connect flow (see section 12) replaces loopback entirely.

Toggles:

- Client OAuth login: **Yes**
- Web OAuth login: **Yes**
- Enforce HTTPS: **Yes**
- Use Strict Mode for redirect URIs: **Yes**
- Force Web OAuth reauthentication: **No** (demands a password every connect)
- Embedded browser OAuth login: **No**
- Login from devices: **No** (TV flow)
- Login with the JavaScript SDK: **No**

Leave blank:

- Allowed Domains for the JavaScript SDK (not used)
- Deauthorize callback URL (only needed for App Review / production use with non-tester users)
- Data Deletion Request URL (same)

## 8. Add the OAuth callback hostname to App Domains

**Settings -> Basic.** Find the **App Domains** field (above the
privacy policy URL field) and add just the hostname of the
`OauthCallbackEndpoint`:

```
<oauth-callback-hostname>
```

Where `<oauth-callback-hostname>` is the full `OauthCallbackEndpoint`
minus the `https://` and the trailing slash
(for example `abc123xyz.lambda-url.<region>.on.aws`).

App Domains is a **separate allowlist** from Valid OAuth Redirect
URIs; both must list the callback hostname. If this field is empty,
Meta's consent dialog returns a `The domain of this URL isn't included
in the app's domains` error even though the redirect URI itself is
registered correctly.

Save.

## 9. Create a Business Login Configuration

**Products -> Facebook Login for Business -> Configurations ->
Create configuration.**

The Business Login Configuration is where permission grants actually
happen on Business apps. The classic `scope=` OAuth URL silently
clamps to a read-only baseline on Business apps; writes only land
when the consent URL carries a `config_id=` naming a configuration
that lists them.

Walk the wizard:

- **Name:** anything descriptive (e.g. `<env>-social-mcp`)
- **Login variant:** Business Login (or whichever option the current
  console labels for the OAuth code flow)
- **Access token:** **User access token**. Not System-user; our
  token exchange code targets the user-token exchange path and
  derives the Page token through `/me/accounts`. System-user would
  require rewriting `metaClient.py`.
- **Assets:** pre-select the Pages that will be registered under
  this deployment. The Page selection in the connect flow still runs
  against this list.
- **Permissions:** tick every permission the use cases enabled in
  sections 3 and 4. Everything, not just the ones with "manage" in
  the name. Nothing is granted at consent time unless it is listed
  here on Business apps.

Save, then **copy the Configuration ID**.

## 10. Save the Configuration ID into config

Add this to `config/<env>.json` at the top level (next to
`publishing_enabled`, `allowed_client_hostnames`, etc.):

```json
"meta_business_login_config_id": "<CONFIG_ID>"
```

The admin CLI auto-detects this field and switches the consent URL to
`config_id=<CONFIG_ID>` instead of `scope=<comma-list>`. Without it,
Business apps silently grant reads only, no matter what you request.

If `meta_business_login_config_id` is absent, the CLI falls back to
the classic `scope=` URL; dev / example environments on consumer FB
Login still work the old way.

## 11. Configure the Webhooks product

**Products -> Webhooks -> Configure.**

- **Callback URL:** `<WebhookIngestEndpoint>`
- **Verify Token:** fetch with
  ```bash
  AWS_PROFILE=<profile> aws secretsmanager get-secret-value \
    --region <region> \
    --secret-id <Env>-SocialMcp-WebhookVerifyToken \
    --query SecretString --output text
  ```
- Subscribe **Page**: tick `feed` and `comments`.
- Subscribe **Instagram**: tick `comments`.

## 12. Pre-add aliases to config before connecting

The `admin.py connect` CLI refuses to write an ACCOUNT item for an
alias that is not already in `config/<env>.json`, and the Lambda's
`ACCOUNTS` env var is baked in at deploy time. Adding or removing an
alias is a redeploy.

Add one entry per Page you plan to register (and one per linked IG
account you plan to pass with `--ig-alias`):

```json
{
  "alias": "<alias>",
  "provider": "facebook",
  "publish_enabled": false,
  "publish_mode": "manual",
  "auto_for_kinds": [],
  "auto_verbs": [],
  "profile_edits_enabled": false,
  "policy": {
    "banned_characters": ["\u2014"],
    "banned_words": [],
    "required_signoff": "",
    "max_hashtags": -1,
    "allowed_link_domains": [],
    "voice": "brand"
  }
}
```

For an IG alias, use `"provider": "instagram"`. Everything else stays
the same. The dormant IG entry does nothing until `admin.py connect
--ig-alias <alias>` writes a token into it.

Keep every account's `publish_enabled: false` and
`profile_edits_enabled: false` for now; sections 15 and 16 cover
flipping them on.

Redeploy once after editing config so the new aliases land in the
Lambda env:

```bash
./deployCDK.sh <env> --auto-approve
```

## 13. Run the first connect

Use `--remote` as the default on Business apps:

```bash
cd <repo-root>
. .venv/bin/activate
AWS_PROFILE=<profile> python scripts/admin.py connect \
  --env <env> \
  --alias <alias> \
  --scopes profile \
  --remote \
  --page-id <PAGE_ID>
```

Flag notes:

- **`--remote`** writes a short-lived state record to DynamoDB, prints
  a consent URL you open on any device, and polls until the hosted
  callback Lambda completes the exchange. Local loopback (`admin.py
  connect` without `--remote`) is likely to fail on Business apps per
  section 7.
- **`--page-id <id>`** is required whenever the authorizing admin
  administers more than one Page. The CLI refuses to guess; a run
  without it will fail with
  `authorization failed: ambiguous: <Name> [<id>], ...` and you can
  copy the right ID straight out of that error.
- **`--ig-alias <alias>`** registers a linked IG Business account
  under a second alias. Pass it only if the Page actually has a
  linked IG account; omit otherwise.
- **`--scopes profile`** requests the M9 scope tier
  (`pages_manage_metadata`). Even on the Business Login Configuration
  flow where the configuration dictates what Meta grants, the CLI
  carries the tier label through to the state record for audit; use
  `profile` to tag the connect as a profile-enabled grant.

Confirm:

```bash
AWS_PROFILE=<profile> python scripts/admin.py list-accounts --env <env>
```

The output names every registered account, its provider, display
name, granted scopes, publish mode, and token status.

## 14. Scope-grant reporting quirk

`list-accounts` reads the user-level token's `/debug_token` response
to populate the `scopes` list. On Business Login Configuration flows,
the user token often carries only the read scopes while the derived
Page Access Token carries the full capability set from the
configuration. Which means `scopes=[...]` in `list-accounts` can look
narrower than the writes the token can actually perform.

If a publish or edit works despite `list-accounts` showing a
reads-only grant, the token is fine. The reporting is cosmetic.

A future tightening to call `/me/accounts` and record the Page token's
`tasks` array (Meta's actual Page-level capability list:
`CREATE_CONTENT`, `MODERATE`, etc.) is noted but not shipped.

## 15. Flip publishing on

Right after section 13, publishing is off everywhere:

- `publishing_enabled: false` at the top of `config/<env>.json` is a
  global kill switch.
- Every account carries `publish_enabled: false` and
  `publish_mode: manual`.

To enable publishing on one account:

1. Set `publishing_enabled: true` at the top of `config/<env>.json`.
2. On that account's entry, set `publish_enabled: true`.
3. Choose a `publish_mode`:
   - `manual` (default): every publish needs
     `admin.py approve <draft_id>` first. Use on public-facing
     accounts.
   - `auto`: the server implicitly approves at `draft_post` time if
     the tool is called with `auto_publish=1`. Use on dev / sandbox
     / internal pages where the agent is trusted to post without a
     human in the loop.
   - `auto_for` plus `auto_for_kinds: ["text"]`: auto-approve text
     posts, hold media posts for manual approval.
4. Redeploy so the new flags land in the Lambda env.

Policy (banned characters, banned words, hashtag cap, link-domain
allowlist) runs at draft time **and** at publish time in every mode;
auto mode skips the human review, not the content integrity check.

### Per-verb auto-approval (M10)

`publish_mode` governs the publish verb only. Edits, replies, and
hides against already-live content default to operator approval and
can be opted into one-shot auto-approve via the per-account
`auto_verbs` list.

The eligible values are `edit`, `reply`, and `hide`. The MCP tool for
each accepts a matching `auto_publish=1` parameter which records the
implicit approval and fires the Publisher in one call:

```json
{
  "alias": "<alias>",
  "publish_mode": "auto",
  "auto_verbs": ["edit", "reply", "hide"]
}
```

`publish_mode` and `auto_verbs` are **independent axes**. You can run
publish on manual while opting edit into auto, or vice versa.

**Deletes and profile edits are hardcoded manual** and cannot be
listed in `auto_verbs`. `delete_post` with `auto_publish=1` returns a
design-level refusal regardless of config; `publish_profile_edit`
always requires `admin.py approve-profile-edit`.

See DESIGN.md section 7.8 for the full semantics.

## 16. Enable profile edits (optional, M9)

Profile management is off per-account by default so a fresh
deployment cannot change a Page's About line, website, or Bluesky bio
without an explicit opt-in and an operator approval on every change.

To turn it on for one account:

1. Add `"profile_edits_enabled": true` on that account's entry in
   `config/<env>.json`.
2. If this is a Facebook Page and you did not already use
   `--scopes profile` in section 13, re-consent now so the token
   carries `pages_manage_metadata`:
   ```bash
   python scripts/admin.py connect --env <env> --alias <alias> --scopes profile --remote --page-id <PAGE_ID>
   ```
   Instagram profile edits are not supported via the Graph API; the
   tool refuses with a specific error. Bluesky needs no extra
   consent; the app password already covers the profile record.
3. Redeploy so `profile_edits_enabled` lands in the Lambda env.

Operator loop:

- `draft_profile_edit(...)` (MCP tool) stages the patch. Policy runs
  on `display_name`, `about`, `description`, and `website`.
- `python scripts/admin.py approve-profile-edit --env <env> --alias <alias>`
  prints the staged patch, asks for confirmation, writes the approval,
  and fires the Publisher.
- Reject with
  `python scripts/admin.py reject --env <env> --kind profile-edit --account <alias>`;
  the audit row records the rejection.

Profile edits are **always manual** regardless of `publish_mode` or
`auto_verbs`.

## 17. Connect a Bluesky account (optional)

Bluesky uses an app password, not OAuth. Generate one at
[bsky.app/settings/app-passwords](https://bsky.app/settings/app-passwords)
(it looks like `xxxx-xxxx-xxxx-xxxx`), then:

```bash
AWS_PROFILE=<profile> python scripts/admin.py connect-bluesky \
  --env <env> \
  --alias <alias>
```

The CLI prompts for your handle (e.g. `you.bsky.social`) and the app
password; it exchanges those for access + refresh JWTs and writes the
ACCOUNT item like any other provider. The alias must already exist in
`config/<env>.json` with `"provider": "bluesky"`.

## 18. Subscribe webhooks per account (optional)

If you want the real-time comment inbox (`wait_for_comments` /
`check_comments`):

```bash
AWS_PROFILE=<profile> python scripts/admin.py webhook-subscribe \
  --env <env> \
  --alias <alias>
```

That adds the Page / IG account to your Meta app's `subscribed_apps`
edge for the `feed` and `comments` fields.

Without this the server still serves pull-based tools
(`list_comments`, `get_post`); you just do not get push notifications
when a new comment lands.

## 19. First smoke test

Three levels, each isolates a different link in the chain.

### Level 1: curl the Lambda directly

Bypasses Claude Code; confirms the server is alive, the IP gate lets
you through, and the bearer token works.

```bash
TOKEN=$(AWS_PROFILE=<profile> aws secretsmanager get-secret-value \
  --region <region> \
  --secret-id <Env>-SocialMcp-BearerToken \
  --query SecretString --output text)

curl -sS -X POST \
  <McpEndpoint> \
  -H "Authorization: Bearer $TOKEN" \
  -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | head -c 2000
```

Expected: a JSON blob (or SSE event) listing every tool. A 401
means the IP gate is rejecting you (not on an allowed hostname) or
the bearer token is wrong; swap `-sS` for `-sSi` to see which gate
answered.

### Level 2: Claude Code sees the server

```bash
claude mcp list
```

Confirm `social` is listed. If not, re-add with
`claude mcp add --transport http -s user social <McpEndpoint> -H "Authorization: Bearer <token>"`
and restart Claude Code.

### Level 3: tool calls through the agent

With one account connected and publishing still off, ask your MCP
client to do something read-only:

- "list the social accounts I can post to"
- "get the current profile for `<alias>` on social"
- "list the last five posts on `<alias>` on social"
- "insights for one of those posts on social"

If those return clean data from Meta, the stack is wired end to end.
Enable publishing per sections 15 and 16 when the read side has been
exercised.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| 401 from the MCP endpoint, bearer token looks right | Source IP not resolving under `allowed_client_hostnames`; check DDNS; VPN not connected |
| `Invalid Scopes: publish_video` on consent | You are on a pre-9e05a91 client; `publish_video` was retired Meta-side. Pull latest |
| `The domain of this URL isn't included in the app's domains` on consent | Section 8 App Domains field is empty or missing the OauthCallbackEndpoint hostname |
| `authorization failed: missing state parameter` on the callback | You reopened a stale callback URL (or refreshed the "Authorization failed" tab from a previous attempt). Re-run connect for a fresh `state` |
| `authorization failed: ambiguous: <Name> [<id>], ...` | Authorizing user administers more than one Page; copy the right ID from the error and pass `--page-id` |
| Consent completed successfully but writes still refused | The Business Login Configuration was not created, or does not include the four write permissions. Open the configuration (section 9), tick the writes, save, re-run connect |
| `list-accounts` shows fewer scopes than requested but writes work | Reporting quirk (section 14); the token is fine, the display is cosmetic |
| "This content isn't available right now" with no permission picker on consent | Meta is remembering prior consent OR the Business Login Configuration is empty. The CLI already includes `auth_type=rerequest`; the fix is confirming the configuration carries the permissions |
| Tool call fails with "scope not granted" naming a Meta permission | Re-run `admin.py connect --alias <alias> --scopes <tier>` to re-consent with the wider scope set |
| Tool call fails with `needs_reconnect` on an account | The stored user token hit a `190` and could not be refreshed; re-run `admin.py connect` for that alias |
| `publish_draft` fails with "publishing is disabled" | Global `publishing_enabled` is false, or the account's `publish_enabled` is false. Check `config/<env>.json`. Not changeable at runtime; it is a redeploy |
| Comment webhook ingest returning 401 to Meta | Verify token drift between the Meta app's Webhook product config and the `<Env>-SocialMcp-WebhookVerifyToken` secret. Re-copy the secret into the Meta app |
| `delete_post` with `auto_publish=1` refused even though delete is in `auto_verbs` | Working as designed: `delete` and `profile_edit` are hardcoded manual (section 15). The `auto_verbs` list only accepts `edit`, `reply`, `hide` |

For deeper issues, read the Publisher Lambda's CloudWatch log group
(`/aws/lambda/<env>-socialmcp-publisher`); that is where every publish
attempt records its approval re-check, policy re-run, and provider
call.
