"""content_publishing_limit precheck + warning threshold (DESIGN.md 5)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from conftest import load_fixture
from sharedModules import dynamo, registry
from sharedModules.providers import instagram


class _FakeGraph:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.get_count = 0

    def get(self, path: str, params: dict | None = None) -> dict:
        self.get_count += 1
        return self.response

    def post(self, path: str, data: dict | None = None, params: dict | None = None) -> dict:
        raise AssertionError("post should not be called by publishing_limit")

    def delete(self, path: str, params: dict | None = None) -> dict:
        raise AssertionError("delete should not be called by publishing_limit")


def _entry(alias: str) -> dict:
    return {
        "alias": alias,
        "provider": "instagram",
        "publish_enabled": True,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "policy": {},
    }


def _connect(scopes: list[str]) -> None:
    from sharedModules import crypto

    dynamo.put_account(
        "main-ig",
        {
            "provider": "instagram",
            "account_id": "17841000000000001",
            "display_name": "Example (Instagram)",
            "scopes": scopes,
            "page_token_ct": crypto.encrypt("page-token-placeholder"),
            "status": "active",
            "linked_page": "main-page",
        },
    )


@pytest.fixture(autouse=True)
def _reset_limit_cache():
    instagram._limit_cache.clear()
    yield
    instagram._limit_cache.clear()


@pytest.fixture
def ig(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry("main-ig")]))
    registry.all_accounts.cache_clear()
    _connect(
        [
            "pages_show_list",
            "instagram_basic",
            "instagram_content_publish",
        ]
    )
    yield


def test_publishing_limit_reports_remaining(ig):
    fake = _FakeGraph(load_fixture("ig_publishing_limit.json")["ample"])
    provider = instagram.InstagramProvider("main-ig", _client=fake)
    info = provider.publishing_limit()
    assert info["quota_total"] == 50
    assert info["quota_usage"] == 3
    assert info["quota_remaining"] == 47


def test_publishing_limit_cached_for_60s(ig, monkeypatch):
    fake = _FakeGraph(load_fixture("ig_publishing_limit.json")["ample"])
    provider = instagram.InstagramProvider("main-ig", _client=fake)
    # First call hits the API.
    provider.publishing_limit()
    assert fake.get_count == 1
    # Second call inside the TTL serves from cache.
    provider.publishing_limit()
    assert fake.get_count == 1

    # Advance monotonic beyond the TTL and the next call hits again.
    base = time.monotonic
    offset = instagram._LIMIT_CACHE_TTL + 1
    monkeypatch.setattr(instagram.time, "monotonic", lambda: base() + offset)
    provider.publishing_limit()
    assert fake.get_count == 2


def test_validate_draft_warns_at_low_quota(ig):
    fake = _FakeGraph(load_fixture("ig_publishing_limit.json")["low"])
    provider = instagram.InstagramProvider("main-ig", _client=fake)
    draft = SimpleNamespace(
        account="main-ig",
        kind="image",
        text="",
        link="",
        media=[],
        schedule="",
        is_ai_generated=False,
    )
    warnings = provider.validate_draft(draft)
    assert any("quota is low" in w for w in warnings)


def test_validate_draft_quiet_at_ample_quota(ig):
    fake = _FakeGraph(load_fixture("ig_publishing_limit.json")["ample"])
    provider = instagram.InstagramProvider("main-ig", _client=fake)
    draft = SimpleNamespace(
        account="main-ig",
        kind="image",
        text="",
        link="",
        media=[],
        schedule="",
        is_ai_generated=False,
    )
    assert provider.validate_draft(draft) == []
