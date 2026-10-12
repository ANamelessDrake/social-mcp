"""S3 media staging for Instagram publishing (DESIGN.md section 9).

Instagram's container flow fetches media over the public internet, so the
Publisher cannot stream bytes to Meta directly. Instead:

1. `stage_bytes` writes the bytes into the deployment's media bucket under
   `staging/<account>/<ulid>/<name>` with the mime as ContentType and
   returns a `MediaRef(source="s3", key="s3://<bucket>/<key>", ...)`. The
   caller stores that ref on the draft.
2. At publish time the Publisher calls `presign_get` to hand Meta a
   15-minute presigned URL and uses that URL in the media container.
3. On FINISHED the Publisher calls `drain` to delete the staged objects.
   The bucket's 24-hour lifecycle rule backstops a publish that fails
   before `drain` runs.

The 15-minute presign window is deliberately short: Meta's fetch happens in
seconds, so a leaked URL has 50x headroom but no further life.
"""

from __future__ import annotations

import logging
import os
import secrets as pysecrets
from datetime import UTC, datetime
from functools import lru_cache

import boto3

from sharedModules.errors import AccountError
from sharedModules.models import MediaRef

log = logging.getLogger(__name__)

PRESIGN_SECONDS = 900  # 15 minutes


@lru_cache(maxsize=1)
def _s3():
    return boto3.client("s3")


def _bucket() -> str:
    bucket = os.environ.get("MEDIA_BUCKET", "")
    if not bucket:
        raise AccountError(
            "MEDIA_BUCKET env var is unset; the s3Stack must be deployed "
            "before Instagram publishing works"
        )
    return bucket


def _ulid_like() -> str:
    """Lexicographic UTC-ordered id for the key prefix."""
    return f"{int(datetime.now(UTC).timestamp() * 1000):013d}-{pysecrets.token_hex(4)}"


def staging_key(account: str, filename: str | None = None) -> str:
    """Build a key under this account's staging prefix.

    Keeping the key under `staging/<account>/` lets `draft_post` reject
    a media ref that points at another account's staged bytes.
    """
    name = filename or "media"
    name = name.replace("/", "_").replace("\\", "_").strip() or "media"
    return f"staging/{account}/{_ulid_like()}/{name}"


def stage_bytes(data: bytes, mime: str, account: str, name: str | None = None) -> MediaRef:
    """Upload `data` into the media bucket and return a MediaRef.

    `name` is a hint for the stored file name; the key always starts with
    `staging/<account>/<ulid>/` so one account cannot reference another's
    staged media (DESIGN.md section 9).
    """
    if not account:
        raise AccountError("stage_bytes requires an account alias")
    if not mime:
        raise AccountError("stage_bytes requires a mime_type")
    bucket = _bucket()
    key = staging_key(account, name)
    resp = _s3().put_object(Bucket=bucket, Key=key, Body=data, ContentType=mime)
    etag = (resp.get("ETag") or "").strip('"')
    return MediaRef(source="s3", key=f"s3://{bucket}/{key}", mime=mime, etag=etag)


def _parse_ref(ref_or_key: str | MediaRef) -> tuple[str, str]:
    """Return (bucket, key) from either an `s3://bucket/key` URI or MediaRef."""
    key = getattr(ref_or_key, "key", None) or (
        ref_or_key.get("key") if isinstance(ref_or_key, dict) else ref_or_key
    )
    if not isinstance(key, str) or not key.startswith("s3://"):
        raise AccountError(f"not an s3 media ref: {key!r}")
    without_scheme = key[len("s3://"):]
    bucket, _, path = without_scheme.partition("/")
    if not bucket or not path:
        raise AccountError(f"malformed s3 uri: {key!r}")
    return bucket, path


def presign_get(ref_or_key, expires_seconds: int = PRESIGN_SECONDS) -> str:
    """15-minute presigned GET URL for a staged object (DESIGN.md section 9)."""
    bucket, key = _parse_ref(ref_or_key)
    return _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=max(60, min(int(expires_seconds), 3600)),
    )


def delete(ref_or_key) -> None:
    """Delete one staged object. Safe to call on a missing key."""
    try:
        bucket, key = _parse_ref(ref_or_key)
    except AccountError:
        return
    try:
        _s3().delete_object(Bucket=bucket, Key=key)
    except Exception as e:  # noqa: BLE001 - best-effort; lifecycle backstops it
        log.info("mediaStaging.delete best-effort failure for %s: %s", ref_or_key, e)


def drain(media_refs: list) -> None:
    """Delete every s3-sourced ref. Lifecycle rule backstops any failure."""
    for m in media_refs or []:
        source = getattr(m, "source", None) if hasattr(m, "source") else (
            m.get("source") if isinstance(m, dict) else None
        )
        if source == "s3":
            delete(m)
