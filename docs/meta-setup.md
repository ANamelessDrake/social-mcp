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

- `--scopes engagement` (M3): the above plus `pages_manage_engagement`.
  Required for `edit_post` / `delete_post` / `reply_to_comment` /
  `hide_comment` to reach Meta. Re-run connect with this flag once per
  Page that needs M3 verbs:

```bash
python scripts/admin.py connect --env dev --alias main-page --scopes engagement
```

Instagram-side scopes ride on every bundle because one Facebook Login
consent covers both the Pages and Instagram Platform products:

- `readonly` and `publish` include `instagram_basic` and
  `instagram_manage_insights` so IG `list_posts` / `get_post` /
  `list_comments` / `get_insights` work on the IG account connected
  through the same Page.
- `engagement` adds `instagram_manage_comments` for IG-side
  `reply_to_comment` / `hide_comment` once those land in M5.

Instagram publishing (`instagram_content_publish`) and media delete
(`instagram_manage_contents`) land with M5; they are not requested at
M4 so the consent dialog stays scoped to what the server can actually
use today.

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

## 7. (M3) Operator loop for edits, deletes, and comment replies

Edits, deletes, and comment replies on already-published posts are not
covered by `publish_mode: auto`: each one needs an explicit operator
approval tied to the exact change. The pattern mirrors the draft loop:
the agent stages the change, the operator reviews and approves, the
Publisher reaches Meta.

```bash
# Agent (any MCP client) stages a change:
#   edit_post(post_id='...', text='revised text')          -> staged
#   delete_post(post_id='...', reason='spam escalated')    -> staged
#   reply_to_comment(comment_id='...', text='thanks for the catch') -> staged

# Operator reviews + approves. Each writes an approval record whose
# hash binds to the staged payload, then invokes the Publisher inline:
python scripts/admin.py approve-edit    --env dev --post-id <id>
python scripts/admin.py approve-delete  --env dev --post-id <id>
python scripts/admin.py approve-reply   --env dev --comment-id <id>

# Reject a staged change (drops the staged item without touching Meta):
python scripts/admin.py reject --env dev --kind edit   --post-id <id>
python scripts/admin.py reject --env dev --kind delete --post-id <id>
python scripts/admin.py reject --env dev --kind reply  --comment-id <id>
```

Hiding comments is reversible from Meta's own UI, so one standing
per-account approval covers every hide:

```bash
# Agent calls hide_comment(comment_id='...'); first time, it is queued.
# Operator grants the standing approval; queued hides are swept
# through the Publisher in one call (one audit row per hide):
python scripts/admin.py approve-hide --env dev --account main-page

# Later, the agent's hide_comment calls proceed immediately. The audit
# log still has one row per comment hidden.

# Revoke the standing approval (future hides queue again, past hides
# stay hidden on Meta):
python scripts/admin.py approve-hide --env dev --account main-page --revoke

# Drop a queued hide request without approving the account:
python scripts/admin.py reject --env dev --kind hide-request \
    --account main-page --comment-id <id>
```

## 8. (M4) Instagram, reached through the linked Page

An Instagram business or creator account is not a separate OAuth grant.
The `connect` flow enumerates `/me/accounts`, detects each Page's
`instagram_business_account`, and (when you say yes) writes a second
ACCOUNT item for the IG account that stores **the same Page token**.
The IG account item carries the IG user id as `account_id` and the Page
alias in `linked_page`; the Page item carries the IG alias in
`linked_ig_account`.

Reads then just work:

```bash
# Call tools against the IG alias (e.g. 'main-page-ig'):
#   list_posts(account='main-page-ig')     -> IG media newest first
#   get_post(post_id=<ig_media_id>, account='main-page-ig')
#   list_comments(post_id=<ig_media_id>, account='main-page-ig')
#   get_insights(post_id=<ig_media_id>, account='main-page-ig')
```

Media types map as follows (DESIGN.md section 6):

| IG `media_type` | Unified `kind` |
|---|---|
| `IMAGE` | `image` |
| `VIDEO` | `video` |
| `CAROUSEL_ALBUM` | `multi_image` |
| `REELS` | `reel` |

Insights are media-type aware (image / carousel share one metric set;
video / reel add `views` and `ig_reels_video_view_total_time`). Meta
deprecated the media-level `impressions`, `plays`, `clips_replays_count`,
and `ig_reels_aggregated_all_plays_count` on 2025-01-21; the provider
never requests those.

If a tool returns "account '...' did not grant 'instagram_manage_insights'
at consent time", re-run connect to pick up the current `readonly` bundle:

```bash
python scripts/admin.py connect --env dev --alias main-page --scopes readonly
```

This overwrites the Page tokens on the Page item (and, through the shared
token, re-enables the linked IG alias).

## 9. (M5) Instagram publishing

M5 lands the container flow, the S3 media staging bucket, and the
publishing-limit precheck. Two scope tiers are relevant:

- `--scopes publish-ig` adds `instagram_content_publish` so an IG alias
  can create and publish media containers.
- `--scopes delete-contents` adds `instagram_manage_contents` on top
  (Meta required it for IG media deletion on 2025-12-03). Only grant
  this tier when the deployment needs to delete IG media; nothing in
  M5 publishes requires it.

Re-run `admin.py connect` once per Page you want to publish from:

```bash
python scripts/admin.py connect --env dev --alias main-page --scopes publish-ig
# or, to also allow IG media deletes:
python scripts/admin.py connect --env dev --alias main-page --scopes delete-contents
```

### Media staging

Instagram's publish flow requires that `image_url` and `video_url` be
publicly reachable; Meta fetches the bytes before `media_publish`. The
flow is:

1. Agent calls `stage_media(data_base64, mime_type)` and gets back an
   `s3://` key.
2. Agent passes that key (comma-separated with others for a carousel)
   to `draft_post(media='s3://...')`.
3. On publish, the Publisher presigns the object for 15 minutes, hands
   the URL to Meta, polls `status_code` until FINISHED, publishes, and
   deletes the staged bytes.
4. If anything fails before step 3's delete, the bucket's 24-hour
   lifecycle rule removes the object.

The staging key always starts with `staging/<account>/`, so a draft
cannot reference another account's staged bytes.

Lambda's synchronous payload is capped at ~6 MB, so `stage_media` is
only for small media (images, short videos). For larger media, host it
at your own https URL and pass the URL directly in `draft_post`.

### Publishing cap

Every IG publish is preceded by a check against the account's live
`content_publishing_limit`. The server treats 50 posts in a rolling
24-hour window as the safe assumption (Meta's docs list both 50 and
100 in different sections) and warns on the draft when the remaining
quota drops below 2. The warning is non-blocking; the publish still
proceeds and Meta's own rate limit is the ultimate backstop.

### Operator loop for IG

```bash
# Agent (any MCP client):
#   stage_media(data_base64=..., mime_type='image/jpeg')  -> s3://... key
#   draft_post(account='main-page-ig', kind='image',
#              text='caption', media='s3://...')          -> draft_id

# Operator reviews + approves the IG draft (manual publish_mode):
python scripts/admin.py approve --env dev --draft-id <id>

# Agent publishes (the Publisher presigns the bucket object, polls the
# container flow, publishes, and drains the staged bytes):
#   publish_draft(draft_id=<id>)  ->  post_id (ig_media_id) + permalink
```

## 10. (M6) Hosted OAuth callback for `connect --remote`

The default `connect` flow uses a loopback redirect on the machine
running the script, which requires both AWS credentials and a browser on
that same machine. M6 adds a hosted callback as a separate Lambda with
its own Function URL so you can run the browser consent from a laptop
with no AWS credentials, from a phone, or from a headless pipeline that
only has the public callback URL.

### Register the callback URL in the Meta app

After deploying the lambda stack, read the `OauthCallbackEndpoint`
output:

```bash
aws cloudformation describe-stacks --stack-name dev-socialmcp-lambda \
  --query 'Stacks[0].Outputs[?OutputKey==`OauthCallbackEndpoint`].OutputValue' \
  --output text
```

In the Meta app's Facebook Login for Business product, under Settings,
add the full URL (including the trailing `/`) to **Valid OAuth Redirect
URIs**. The URL is already https (Function URLs are), which Meta
requires.

### Run `connect --remote`

```bash
python scripts/admin.py connect --env dev --alias main-page --remote \
    [--scopes publish-ig] [--page-id <id>] [--ig-alias main-page-ig]
```

- The CLI writes a `CONNECT_STATE#<state>` DynamoDB row with a 15-minute
  TTL and prints the Facebook authorization URL (with the state baked
  in).
- Open that URL in any browser (same machine, another machine, phone).
  Facebook returns the browser to the hosted callback with
  `?code=&state=`.
- The callback exchanges the code for a long-lived user token,
  enumerates Pages, writes the ACCOUNT item(s), and transitions the
  state record to `completed` with the created aliases.
- The CLI, which has been polling the state row every 2 seconds, sees
  the completion and prints the aliases. If the authorizing user
  administers more than one Page, re-run with `--page-id <id>` naming
  the specific Page.

The state row is single-use: a replayed callback for the same state
returns an error page and does not touch the row. If the browser never
returns, the row expires on its own via DynamoDB TTL after 15 minutes.

### When to use which flow

| Scenario | Flow |
|---|---|
| Developer with AWS creds and a browser locally | `connect` (loopback) |
| Laptop with no AWS creds, authorizing from a phone, headless CI | `connect --remote` |
| Multi-Page authorizing user and hosted flow | `connect --remote --page-id <id>` |

Both flows end in the same place: a KMS-encrypted ACCOUNT item in the
core table with a long-lived user token for refresh and a non-expiring
Page token for day-to-day calls.

