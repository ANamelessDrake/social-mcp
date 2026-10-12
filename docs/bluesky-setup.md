# Bluesky setup

Bluesky joins social-mcp as a third provider alongside Facebook Pages and
Instagram. The connect flow is different from Meta's: Bluesky does not
have an OAuth consent dialog; instead the account owner mints an **app
password** and the server trades that for a session (access + refresh JWT)
once.

## 1. Create a Bluesky account (or use one you own)

A Bluesky account can be hosted on the main `bsky.social` PDS or on a
self-hosted PDS. If you self-host, set `BLUESKY_HOST` on the MCP server
and Publisher Lambdas to point at your PDS; the default is
`https://bsky.social`.

## 2. Mint an app password

Open https://bsky.app/settings/app-passwords in the browser of the account
you want this deployment to post as, click **Add App Password**, give it a
name like `social-mcp`, and copy the password. App passwords grant
everything the account can do except mint more app passwords.

## 3. Add the account to your config

In `config/{env}.json`, add an `accounts` entry:

```json
{
  "alias": "main-bsky",
  "provider": "bluesky",
  "publish_enabled": false,
  "publish_mode": "manual",
  "auto_for_kinds": [],
  "policy": { "banned_characters": ["\u2014"], "banned_words": [], "max_hashtags": -1, "allowed_link_domains": [], "voice": "brand" }
}
```

Then redeploy so the server loads the new alias in its roster.

## 4. Run connect-bluesky

```
python scripts/admin.py connect-bluesky --env dev --alias main-bsky
```

You will be prompted for your handle (e.g. `wilma.bsky.social`) and the
app password you minted. The server trades those for a session via
`com.atproto.server.createSession`, KMS-encrypts both JWTs, and writes
the `ACCOUNT#main-bsky` item.

The app password never touches disk on the server side; only the issued
`accessJwt` and `refreshJwt` are stored (both encrypted).

## 5. Verify

```
python scripts/admin.py list-accounts --env dev
```

should now show the Bluesky account with `status=active`.

## Capability notes

- **Supported**: text, link (via facets), image, multi-image (up to 4),
  schedule (server-side), delete, comments (reply thread), insights
  (likes/replies/reposts/quotes).
- **Not supported**: video (new API, deferred), reel (not a Bluesky
  concept), edit (post records are immutable; delete and repost).
- **Hide comment**: there is no server-callable comment-hide on Bluesky
  (muting is a per-viewer client-side action), so the tool refuses with
  a specific error. Comments can still be read with `list_comments` and
  the `reply_to_comment` surface works via the thread's reply refs.
- **Media staging**: Bluesky's `uploadBlob` uploads bytes directly, so
  the provider fetches each media URL (presigned by the Publisher for
  `s3://` refs; passed through for `https://`) and streams the response
  into `uploadBlob`. The staging bucket's 24h lifecycle is the backstop
  on failure, same as Instagram.

## Token refresh

Bluesky access JWTs are short-lived; the client refreshes them on an
`ExpiredToken` response using the stored refresh JWT (same one-shot
pattern Meta's client uses on error code 190). On a refresh failure the
account is marked `needs_reconnect` and the next tool call surfaces the
`connect-bluesky` command.
