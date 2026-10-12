"""S3 media staging: upload, presign, drain, key namespacing (DESIGN.md 9)."""

from __future__ import annotations

import boto3
import pytest
from sharedModules.errors import AccountError
from sharedModules.providers import mediaStaging


@pytest.fixture(autouse=True)
def _reset_s3_cache():
    """Each test gets a fresh moto S3 client; drop the lru_cache."""
    mediaStaging._s3.cache_clear()
    yield
    mediaStaging._s3.cache_clear()


@pytest.fixture
def bucket(monkeypatch, aws):
    """Fresh bucket matching the deployed MEDIA_BUCKET env var."""
    name = "example-socialmcp-media"
    boto3.client("s3").create_bucket(Bucket=name)
    monkeypatch.setenv("MEDIA_BUCKET", name)
    return name


def test_stage_bytes_writes_to_account_prefixed_key(bucket):
    ref = mediaStaging.stage_bytes(b"abc123", "image/jpeg", "main-ig", "photo.jpg")
    assert ref.source == "s3"
    assert ref.key.startswith(f"s3://{bucket}/staging/main-ig/")
    assert ref.key.endswith("photo.jpg")
    assert ref.mime == "image/jpeg"
    assert ref.etag


def test_stage_bytes_requires_account(bucket):
    with pytest.raises(AccountError, match="account"):
        mediaStaging.stage_bytes(b"abc", "image/jpeg", "")


def test_stage_bytes_requires_mime(bucket):
    with pytest.raises(AccountError, match="mime"):
        mediaStaging.stage_bytes(b"abc", "", "main-ig")


def test_stage_bytes_errors_without_bucket_env(monkeypatch, aws):
    monkeypatch.delenv("MEDIA_BUCKET", raising=False)
    with pytest.raises(AccountError, match="MEDIA_BUCKET"):
        mediaStaging.stage_bytes(b"abc", "image/jpeg", "main-ig")


def test_presign_get_returns_url_for_the_key(bucket):
    ref = mediaStaging.stage_bytes(b"abc", "image/jpeg", "main-ig")
    url = mediaStaging.presign_get(ref)
    # moto's presigner returns an https URL carrying the bucket and key
    assert url.startswith("https://")
    assert bucket in url
    # the staging prefix is preserved so Meta's fetch finds the object
    assert "/staging/main-ig/" in url


def test_presign_rejects_non_s3_refs(bucket):
    with pytest.raises(AccountError, match="s3 media ref"):
        mediaStaging.presign_get("https://cdn.example.com/x.jpg")


def test_drain_deletes_s3_refs_and_ignores_https(bucket):
    s3 = boto3.client("s3")
    ref_a = mediaStaging.stage_bytes(b"a", "image/jpeg", "main-ig")
    ref_b = mediaStaging.stage_bytes(b"b", "image/jpeg", "main-ig")
    url_ref = {"source": "url", "key": "https://cdn.example.com/c.jpg"}

    before = s3.list_objects_v2(Bucket=bucket).get("KeyCount", 0)
    assert before == 2

    mediaStaging.drain([ref_a, ref_b, url_ref])

    after = s3.list_objects_v2(Bucket=bucket).get("KeyCount", 0)
    assert after == 0


def test_drain_tolerates_missing_object(bucket):
    """A publish-time failure deletes nothing; drain on a missing key is no-op."""
    ref = mediaStaging.stage_bytes(b"a", "image/jpeg", "main-ig")
    mediaStaging.delete(ref)
    # second drain must not raise
    mediaStaging.drain([ref])


def test_delete_tolerates_malformed_ref(bucket):
    mediaStaging.delete("not-an-s3-uri")
    mediaStaging.delete({"source": "url", "key": "https://x"})
