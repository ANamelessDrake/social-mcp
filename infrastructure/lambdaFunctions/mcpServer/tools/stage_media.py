"""Upload media bytes to the deployment's staging bucket (DESIGN.md section 9).

Instagram's publish flow fetches `image_url` and `video_url` over the public
internet; the server stages those bytes in S3 and hands Meta a short-lived
presigned URL at publish time. `stage_media` is the tool-side entry: an
agent that holds bytes locally (an attachment from a conversation, say)
calls `stage_media` and gets back the `s3://` reference that `draft_post`
accepts in its `media` parameter.

Facebook publish paths also accept the staged refs, so the same tool call
works for both providers; only the Facebook path still accepts a raw
https URL unchanged (DESIGN.md section 5 Facebook).

Payload ceiling: Lambda's synchronous invocation caps requests at 6 MB,
which is roughly 4.5 MB of raw bytes after base64 overhead. Large videos
and reels still need a direct https source (hosted by the operator)
until a resumable upload path lands.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

from sharedModules import registry
from sharedModules.errors import AccountError
from sharedModules.providers import mediaStaging


def stage_media(
    data_base64: str,
    mime_type: str,
    account: str = "",
    filename: str = "",
) -> dict:
    """Stage media bytes for a later `draft_post`.

    Parameters are flat strings. Pass the file as base64; the server
    decodes once, writes once, and returns the `s3://` key you then
    pass to `draft_post` in the `media` list.

    - `data_base64` (required): base64-encoded bytes of the file.
    - `mime_type` (required): the file's content type, like
      "image/jpeg", "image/png", "video/mp4".
    - `account`: alias from `list_accounts`. Omit when only one is
      configured; the staged key is namespaced by alias so each
      account's media is isolated from the others.
    - `filename`: a hint used in the stored key. Does not affect
      upload semantics; a sensible value helps when inspecting the
      bucket.

    Returns `{ok, s3_key, mime, etag, expires_at}` on success. The
    staged object is deleted automatically after a successful publish;
    the bucket's 24-hour lifecycle rule backstops any publish that
    fails or is abandoned.

    Size note: this tool is capped at the Lambda synchronous payload
    limit (about 6 MB after transport overhead, so roughly 4.5 MB of
    raw bytes). For larger uploads, host the file at an https URL you
    control and pass that URL directly in `draft_post`.
    """
    try:
        alias = registry.resolve_alias(account)
    except AccountError as e:
        return {"ok": False, "error": str(e)}

    if not data_base64:
        return {"ok": False, "error": "data_base64 is required"}
    if not mime_type:
        return {"ok": False, "error": "mime_type is required (e.g. image/jpeg)"}

    try:
        data = base64.b64decode(data_base64, validate=True)
    except (ValueError, Exception) as e:  # noqa: BLE001 - base64 raises various types
        return {"ok": False, "error": f"data_base64 is not valid base64: {e}"}

    try:
        ref = mediaStaging.stage_bytes(data, mime_type, alias, filename or None)
    except AccountError as e:
        return {"ok": False, "error": str(e)}

    expires_at = (datetime.now(UTC) + timedelta(days=1)).isoformat(timespec="seconds")
    return {
        "ok": True,
        "s3_key": ref.key,
        "mime": ref.mime,
        "etag": ref.etag,
        "expires_at": expires_at,
        "bytes": len(data),
    }
