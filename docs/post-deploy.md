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
| `OauthCallbackEndpoint` | Register under the Facebook Login product's Valid OAuth Redirect URIs (for `admin.py connect --remote`) |

Keep these somewhere you can paste from; the webhook and oauth URLs go into
the Meta app console next.

## 2. Create or configure the Meta app

Full runbook in [`meta-setup.md`](meta-setup.md). The short version:

1. developers.facebook.com -> **Create app** -> **Business** type.
2. Add the **Facebook Login for Business** product.
3. Under Facebook Login settings, add to **Valid OAuth Redirect URIs**:
   - `http://127.0.0.1:*` for local `admin.py connect`
   - the `OauthCallbackEndpoint` for `admin.py connect --remote`
4. Add the **Instagram** product (for IG support later).
5. Add the **Webhooks** product (for the real-time comment inbox):
   - Callback URL: the `WebhookIngestEndpoint`
   - Verify Token: the value in `<Env>-SocialMcp-WebhookVerifyToken` (fetch
     it with `aws secretsmanager get-secret-value --secret-id
     <Env>-SocialMcp-WebhookVerifyToken --query SecretString --output text`)
   - Subscribe to the `feed` and `comments` fields on **Page**; `comments`
     on **Instagram**.
6. Keep the app in **Development Mode**. The deployment serves only
   Pages/accounts you administer, so Standard Access is sufficient and no
   App Review is required (DESIGN.md section 14).

## 3. Populate the Meta app secret

The `secretsStack` created an empty shell for your app's id/secret. Fill it in:

```bash
AWS_PROFILE=<profile> aws secretsmanager put-secret-value \
  --region <region> \
  --secret-id <Env>-SocialMcp-MetaApp \
  --secret-string '{"app_id": "<APP_ID>", "app_secret": "<APP_SECRET>"}'
```

The bearer token (`<Env>-SocialMcp-BearerToken`) and the webhook verify
token (`<Env>-SocialMcp-WebhookVerifyToken`) are auto-generated at deploy
time; do not overwrite them unless you are rotating.

## 4. Fetch the bearer token and point a client at the MCP endpoint

```bash
AWS_PROFILE=<profile> aws secretsmanager get-secret-value \
  --region <region> \
  --secret-id <Env>-SocialMcp-BearerToken \
  --query SecretString --output text
```

Drop that into an MCP client config. For Claude Code, create a `.mcp.json`
anywhere (template at [`../clients/examples/claude-code.mcp.json`](../clients/examples/claude-code.mcp.json)):

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
server only accepts requests from IPs resolving to those hostnames. Agents
on your network see the server; agents on anything else get a 401 before
the token check runs.

## 5. Connect your first Page

Two paths:

### Local (default)

A browser opens on the machine running the CLI; the loopback redirect
catches the authorization code.

```bash
cd <repo-root>
. .venv/bin/activate
AWS_PROFILE=<profile> python scripts/admin.py connect \
  --env <env> \
  --alias <short-alias>
```

### Remote (phone or laptop with no AWS credentials)

The CLI writes a short-lived state record to DynamoDB, prints a consent URL
you open on any device, and polls until the hosted callback Lambda
completes the exchange.

```bash
AWS_PROFILE=<profile> python scripts/admin.py connect \
  --env <env> \
  --alias <short-alias> \
  --remote
```

For either path, the CLI:

1. Opens the consent URL (loopback or hosted).
2. Exchanges the short-lived user token for a long-lived one.
3. Lists Pages visible on your account; you pick which Page to register.
4. Detects any linked Instagram business account and offers to register
   it under a second alias.
5. KMS-encrypts the tokens and writes an `ACCOUNT#<alias>` item.

Confirm:

```bash
AWS_PROFILE=<profile> python scripts/admin.py list-accounts --env <env>
```

The output names every registered account, its provider, display name,
granted scopes, publish mode, and token status.

## 6. (Optional) Subscribe webhooks per account

If you want the real-time comment inbox (M7, `wait_for_comments` /
`check_comments`):

```bash
AWS_PROFILE=<profile> python scripts/admin.py webhook-subscribe \
  --env <env> \
  --alias <short-alias>
```

That adds the Page/IG account to your Meta app's `subscribed_apps` edge
for the `feed` and `comments` fields.

Without this, the server still serves pull-based tools (`list_comments`,
`get_post`); you just do not get push notifications when a new comment
lands.

## 7. (Later) Connect a Bluesky account

Bluesky uses an app password, not OAuth. Generate one at
[bsky.app/settings/app-passwords](https://bsky.app/settings/app-passwords)
(it looks like `xxxx-xxxx-xxxx-xxxx`), then:

```bash
AWS_PROFILE=<profile> python scripts/admin.py connect-bluesky \
  --env <env> \
  --alias <short-alias>
```

The CLI prompts for your handle (e.g. `you.bsky.social`) and the app
password; it exchanges those for access + refresh JWTs and writes the
account item like any other provider.

## 8. Flip publishing on (when you are ready)

Right after deploy, publishing is off everywhere:

- `publishing_enabled: false` at the top of `config/<env>.json` is a
  global kill switch.
- Every account defaults to `publish_mode: manual`.

Before an agent can publish, you have to:

1. Change `publishing_enabled: true` in `config/<env>.json` and redeploy.
2. For each account you want writable, set `publish_enabled: true` on
   that account's entry.
3. Decide the trust posture per account:
   - `publish_mode: manual` (default): every publish needs
     `admin.py approve <draft_id>` first. Use on public-facing accounts.
   - `publish_mode: auto`: the server implicitly approves at
     `draft_post` time. Use on dev / sandbox / internal pages where the
     agent is trusted to post without a human in the loop.
   - `publish_mode: auto_for` plus `auto_for_kinds: ["text"]`:
     auto-approve text, hold media posts for manual approval.

Policy (banned characters, banned words, hashtag cap, link-domain
allowlist) runs at draft time **and** at publish time in every mode;
auto mode skips the human review, not the content integrity check.

Edits, deletes, and comment replies always need operator approval,
regardless of mode. See [`content-policy.md`](content-policy.md) for the
per-account policy fields.

## 9. (Optional) Enable profile edits (M9)

Profile management is off per-account by default so a fresh deployment
cannot change a Page's About line, website, or Bluesky bio without an
explicit opt-in and an operator approval on every change.

To turn it on for one account:

1. Edit `config/<env>.json` and add `profile_edits_enabled: true` on
   that account's entry.
2. If this is a Facebook Page, re-consent with the `profile` scope
   tier so the Page token carries `pages_manage_metadata`:
   ```bash
   python scripts/admin.py connect --env <env> --alias <alias> --scopes profile
   ```
   Instagram profile reads need `instagram_basic`, which the Pages
   consent dialog already covers; IG profile edits are not supported
   via the Graph API (the tool refuses with a specific error).
   Bluesky needs no extra consent; the app password already covers
   the profile record.
3. Redeploy so `profile_edits_enabled` lands in the Lambda env.

Operator loop:

- `draft_profile_edit(...)` (MCP tool) stages the patch; policy runs
  on `display_name`, `about`, `description`, and `website`.
- `python scripts/admin.py approve-profile-edit --env <env> --alias <alias>`
  prints the staged patch, asks for confirmation, writes the approval,
  and fires the Publisher.
- Reject with `python scripts/admin.py reject --env <env> --kind profile-edit --account <alias>`;
  the audit row records the rejection.

`publish_mode: auto` does not auto-approve profile edits by default,
even on an auto account. To let one account auto-apply profile edits,
set `publish_mode: auto_for` and add `profile_edit` to
`auto_for_kinds`.

## 10. First smoke test

With one account connected and publishing still off, ask your MCP client
to do something read-only:

- "list accounts I can post to"
- "list my last five posts on the main page"
- "what are the insights on post `<post-id>`"

If those work, you are done with setup. Toggle publishing on when the read
side has been exercised.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| 401 from the MCP endpoint, bearer token looks right | Source IP not in `allowed_client_hostnames`; check DDNS; VPN not connected |
| `admin.py connect` opens the browser, then "state not recognized or expired" on the callback | 15-minute consent window elapsed; re-run the connect command |
| Tool call fails with "scope not granted" naming a Meta permission | Re-run `admin.py connect --alias <alias> --scopes <tier>` to re-consent with the wider scope set |
| Tool call fails with `needs_reconnect` on an account | The stored user token hit a `190` and could not be refreshed; re-run `admin.py connect` for that alias |
| `publish_draft` fails with "publishing is disabled" | Global `publishing_enabled` is false, or the account's `publish_enabled` is false. Check `config/<env>.json`. Not changeable at runtime; it is a redeploy |
| Comment webhook ingest returning 401 to Meta | Verify token drift between the Meta app's Webhook product config and the `<Env>-SocialMcp-WebhookVerifyToken` secret. Re-copy the secret into the Meta app |

For deeper issues, read the Publisher Lambda's CloudWatch log group
(`/aws/lambda/<env>-socialmcp-publisher`); that is where every publish
attempt records its approval re-check, policy re-run, and provider call.
