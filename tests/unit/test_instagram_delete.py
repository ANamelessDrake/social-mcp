"""Instagram delete needs instagram_manage_contents (added 2025-12-03)."""

from __future__ import annotations

import json

import pytest
from sharedModules import dynamo, registry
from sharedModules.errors import AccountError
from sharedModules.providers import instagram


class _FakeDelete:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []

    def delete(self, path: str, params: dict | None = None) -> dict:
        self.calls.append(("DELETE", path, dict(params or {})))
        return {"success": True}


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


def test_delete_calls_the_graph_endpoint(aws, monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry("main-ig")]))
    registry.all_accounts.cache_clear()
    _connect(["instagram_basic", "instagram_manage_contents"])
    fake = _FakeDelete()
    provider = instagram.InstagramProvider("main-ig", _client=fake)
    provider.delete_post("17895000000000003")
    assert fake.calls == [("DELETE", "/17895000000000003", {})]


def test_delete_without_scope_errors_specifically(aws, monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry("main-ig")]))
    registry.all_accounts.cache_clear()
    _connect(["instagram_basic"])
    provider = instagram.InstagramProvider("main-ig", _client=_FakeDelete())
    with pytest.raises(AccountError, match="instagram_manage_contents"):
        provider.delete_post("17895000000000003")
