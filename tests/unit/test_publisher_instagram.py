"""Publisher dispatch for Instagram publish: S3 drain on success, retain on failure.

The provider is faked out so the test exercises the Publisher's flow,
not the Graph wire format.
"""

from __future__ import annotations

import json

import boto3
import pytest
from sharedModules import approval, dynamo, registry
from sharedModules.errors import AccountError
from sharedModules.models import PublishResult
from sharedModules.providers import mediaStaging


def _entry() -> dict:
    return {
        "alias": "main-ig",
        "provider": "instagram",
        "publish_enabled": True,
        "publish_mode": "auto",
        "auto_for_kinds": [],
        "policy": {},
    }


@pytest.fixture(autouse=True)
def _reset_s3_cache():
    mediaStaging._s3.cache_clear()
    yield
    mediaStaging._s3.cache_clear()


@pytest.fixture
def env(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry()]))
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    monkeypatch.setenv("MEDIA_BUCKET", "example-socialmcp-media")
    boto3.client("s3").create_bucket(Bucket="example-socialmcp-media")
    registry.all_accounts.cache_clear()
    yield


class _FakeProvider:
    def __init__(self, raises: Exception | None = None):
        self._raises = raises
        self.publish_calls = 0

    def publish(self, draft):
        self.publish_calls += 1
        if self._raises:
            raise self._raises
        return PublishResult(
            account="main-ig",
            post_id="17895000000000900",
            permalink="https://www.instagram.com/p/CfakePublishedMedia/",
            kind=draft.kind,
        )


def _staged_media_draft():
    """Create one staged S3 object and build a draft that references it."""
    ref = mediaStaging.stage_bytes(b"image-bytes", "image/jpeg", "main-ig", "p.jpg")
    draft = {
        "draft_id": "ig1",
        "account": "main-ig",
        "kind": "image",
        "text": "caption",
        "link": "",
        "media": [{"source": "s3", "key": ref.key, "mime": ref.mime, "etag": ref.etag}],
        "schedule": "",
        "status": "approved",
        "created_by": "test",
    }
    return draft, ref


@pytest.fixture
def handler(monkeypatch):
    import importlib
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    pub_dir = root / "infrastructure" / "lambdaFunctions" / "publisher"
    sys.path.insert(0, str(pub_dir))
    import app as pub_app

    importlib.reload(pub_app)
    return pub_app


def _list_keys(bucket: str) -> list[str]:
    resp = boto3.client("s3").list_objects_v2(Bucket=bucket)
    return [o["Key"] for o in resp.get("Contents", [])]


def test_publish_success_drains_staged_media(env, handler, monkeypatch):
    fake = _FakeProvider()
    monkeypatch.setattr(handler, "get_provider", lambda _: fake, raising=False)

    draft, ref = _staged_media_draft()
    dynamo.write_draft(draft)
    approval.record_approval(draft_id="ig1", scope="publish", approved_by="op")

    out = handler.handler({"draft_id": "ig1"}, None)
    assert out["ok"] is True
    assert fake.publish_calls == 1
    # The staged bytes are gone on success:
    assert _list_keys("example-socialmcp-media") == []


def test_publish_failure_keeps_staged_media_for_lifecycle(env, handler, monkeypatch):
    fake = _FakeProvider(raises=AccountError("container timed out"))
    monkeypatch.setattr(handler, "get_provider", lambda _: fake, raising=False)

    draft, ref = _staged_media_draft()
    dynamo.write_draft(draft)
    approval.record_approval(draft_id="ig1", scope="publish", approved_by="op")

    out = handler.handler({"draft_id": "ig1"}, None)
    assert out["ok"] is False
    assert "container timed out" in out["error"]
    # The S3 object survived; the 24h bucket lifecycle rule will handle it.
    keys = _list_keys("example-socialmcp-media")
    assert len(keys) == 1
    assert keys[0].startswith("staging/main-ig/")
    assert dynamo.read_draft("ig1")["status"] == "failed"
