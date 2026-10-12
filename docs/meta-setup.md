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

## Scopes requested in M1

M1 is read-only, so the connect script requests only the read half of the
Pages scopes:

- `pages_show_list`
- `pages_read_engagement`
- `pages_read_user_content`
- `read_insights`
- `business_management`
- `public_profile`

Publishing (`pages_manage_posts`, `publish_video`) is added in M2, and
comment write access (`pages_manage_engagement`,
`instagram_manage_comments`) in M3; re-run connect to upgrade scopes.
