# Meta app setup

One-time runbook for the Meta Business app that drives the deployment. See
[DESIGN.md section 3](../DESIGN.md) for the design rationale and
[section 14](../DESIGN.md) for the App Review posture.

## 1. Create the app

1. Sign in to <https://developers.facebook.com/apps> as the Meta user who
   also administers the Pages and Instagram accounts you want to serve.
2. Create a new app, type **Business**.
3. Add the **Facebook Login for Business** product. Under its Settings:
   - Add redirect URI `http://127.0.0.1:0` (any port; the connect script
     uses an ephemeral port and this URI only has to be registered as a
     valid loopback redirect).
   - Enforce HTTPS: off for the loopback redirect.
4. Add the **Instagram Platform** product. The connect flow enumerates
   linked Instagram business accounts automatically; no further action is
   needed here for M1.
5. Keep the app in **Development Mode**. App Review and Business
   Verification are not required for self-admin single-tenant usage; the
   moment you want to post to a Page or IG account you do not administer,
   both become mandatory (DESIGN.md section 14).

## 2. Make yourself an admin on the Pages

The connect flow enumerates Pages the authorizing user administers (via
`GET /me/accounts`). For each Page the deployment should serve:

- Open <https://business.facebook.com/>, pick the Page.
- Settings > People, add the authorizing user as an **Admin**.
- Settings > Linked Accounts, make sure the Instagram business or creator
  account is linked (`instagram_business_account` is what the connect
  script detects).

## 3. Store the Meta app credentials

After the CDK deploy, the empty `{Env}-SocialMcp-MetaApp` secret exists
in Secrets Manager. Store the app id and secret as JSON:

```bash
aws secretsmanager put-secret-value \
  --secret-id Dev-SocialMcp-MetaApp \
  --secret-string '{"app_id": "1234567890", "app_secret": "..."}'
```

The server reads this secret at connect time only; it never reaches an
MCP client.

## 4. Run the connect flow

```bash
python scripts/admin.py connect --env dev --alias main-page
```

The script opens your browser for Facebook consent, exchanges the
authorization code for a long-lived user token, lists the Pages you
administer, lets you pick which Page becomes `main-page`, and offers to
also register the Page's linked Instagram account as a separate alias
(e.g. `main-page-ig`). Tokens are KMS-encrypted before they land in
DynamoDB.

Verify with:

```bash
python scripts/admin.py list-accounts --env dev
```

## Scopes

The connect script requests a bundle by `--scopes`:

- `--scopes readonly` (default, M1): `pages_show_list`,
  `pages_read_engagement`, `pages_read_user_content`, `read_insights`,
  `business_management`, `public_profile`.
- `--scopes publish` (M2): the above plus `pages_manage_posts` and
  `publish_video`. Required for `draft_post` / `publish_draft` /
  `schedule_draft` to reach any Meta publish endpoint. Re-run connect
  with this flag to upgrade an existing read-only connection:

```bash
python scripts/admin.py connect --env dev --alias main-page --scopes publish
```

Comment write access (`pages_manage_engagement`,
`instagram_manage_comments`) lands with M3.

## 5. (M2) Enable publishing in config

The server fences publish calls four ways (DESIGN.md section 7.6):

1. Global `publishing_enabled: true` in `config/{env}.json`.
2. Per-account `publish_enabled: true` in each account's config block.
3. The account's `publish_mode` (`manual` by default; `auto` or `auto_for`
   for trusted accounts, DESIGN.md section 7.4).
4. An approval record whose hash matches the current draft, written
   either by `admin.py approve` (manual) or implicitly by the server at
   draft time (auto).

Flip the first two in config and redeploy (CDK bakes them into the
Lambda environment). `publish_mode` is also config, deliberately not a
runtime switch, so a prompt-injected agent cannot widen its own trust.

## 6. (M2) Operator loop for `publish_mode: manual`

```bash
# Agent drafts (any MCP client):
#   draft_post(kind='text', text='...')  ->  draft_id

# Operator reviews:
python scripts/admin.py show --env dev --draft-id <id>

# Operator approves (writes an approval record whose hash binds to the exact text + media):
python scripts/admin.py approve --env dev --draft-id <id>

# Agent publishes:
#   publish_draft(draft_id=<id>)  ->  post_id + permalink

# At any point the operator can kill switch globally:
python scripts/admin.py disable-publishing --env dev
# ...and later re-enable:
python scripts/admin.py disable-publishing --env dev --enable
```
