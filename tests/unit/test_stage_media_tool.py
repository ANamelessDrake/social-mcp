"""stage_media tool + the cross-account S3 key check in draft_post."""

from __future__ import annotations

import base64
import json

import boto3
import pytest
from sharedModules import dynamo, registry
from sharedModules.providers import mediaStaging


@pytest.fixture(autouse=True)
def _reset_s3_cache():
    mediaStaging._s3.cache_clear()
    yield
    mediaStaging._s3.cache_clear()


@pytest.fixture
def bucket(monkeypatch, aws):
    name = "example-socialmcp-media"
    boto3.client("s3").create_bucket(Bucket=name)
    monkeypatch.setenv("MEDIA_BUCKET", name)
    return name


def _ig_entry(alias: str) -> dict:
    return {
        "alias": alias,
        "provider": "instagram",
        "publish_enabled": True,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "policy": {},
    }


def _connect_ig(alias: str) -> None:
    from sharedModules import crypto

    dynamo.put_account(
        alias,
        {
            "provider": "instagram",
            "account_id": f"17841000000000{len(alias):03d}",
            "display_name": alias,
            "scopes": ["instagram_basic", "instagram_content_publish"],
            "page_token_ct": crypto.encrypt("page-token-placeholder"),
            "status": "active",
            "linked_page": "ignored",
        },
    )


def test_stage_media_returns_s3_key_under_account_prefix(bucket, monkeypatch):
    monkeypatch.setenv(
        "ACCOUNTS", json.dumps([_ig_entry("main-ig"), _ig_entry("other-ig")])
    )
    registry.all_accounts.cache_clear()
    from mcpServer.tools.stage_media import stage_media

    data = base64.b64encode(b"\xff\xd8\xffpng-ish-bytes").decode()
    result = stage_media(data_base64=data, mime_type="image/jpeg", account="main-ig")
    assert result["ok"] is True
    assert result["s3_key"].startswith("s3://example-socialmcp-media/staging/main-ig/")
    assert result["mime"] == "image/jpeg"
    assert result["bytes"] > 0


def test_stage_media_rejects_bad_base64(bucket, monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_ig_entry("main-ig")]))
    registry.all_accounts.cache_clear()
    from mcpServer.tools.stage_media import stage_media

    result = stage_media(data_base64="not-base64!!!", mime_type="image/jpeg", account="main-ig")
    assert result["ok"] is False
    assert "base64" in result["error"]


def test_stage_media_requires_mime(bucket, monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_ig_entry("main-ig")]))
    registry.all_accounts.cache_clear()
    from mcpServer.tools.stage_media import stage_media

    data = base64.b64encode(b"\xff\xd8\xffmore-bytes").decode()
    result = stage_media(data_base64=data, mime_type="", account="main-ig")
    assert result["ok"] is False
    assert "mime" in result["error"].lower()


def test_draft_post_accepts_matching_s3_key(bucket, monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_ig_entry("main-ig")]))
    monkeypatch.setenv("PUBLISHING_ENABLED", "false")
    registry.all_accounts.cache_clear()
    _connect_ig("main-ig")

    from mcpServer.tools.draft_post import draft_post
    from mcpServer.tools.stage_media import stage_media

    data = base64.b64encode(b"\xff\xd8\xffbytes").decode()
    staged = stage_media(data_base64=data, mime_type="image/jpeg", account="main-ig")
    result = draft_post(
        kind="image",
        account="main-ig",
        text="caption",
        media=staged["s3_key"],
    )
    assert result["ok"] is True
    assert result["draft_id"]


def test_draft_post_rejects_cross_account_s3_key(bucket, monkeypatch):
    monkeypatch.setenv(
        "ACCOUNTS", json.dumps([_ig_entry("main-ig"), _ig_entry("other-ig")])
    )
    monkeypatch.setenv("PUBLISHING_ENABLED", "false")
    registry.all_accounts.cache_clear()
    _connect_ig("main-ig")
    _connect_ig("other-ig")

    from mcpServer.tools.draft_post import draft_post
    from mcpServer.tools.stage_media import stage_media

    data = base64.b64encode(b"\xff\xd8\xffbytes").decode()
    staged = stage_media(data_base64=data, mime_type="image/jpeg", account="other-ig")
    # Try to use `other-ig`'s staged key on `main-ig`
    result = draft_post(
        kind="image",
        account="main-ig",
        text="caption",
        media=staged["s3_key"],
    )
    assert result["ok"] is False
    assert "staging prefix" in result["error"]
