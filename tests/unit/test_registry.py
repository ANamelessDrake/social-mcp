import json

import pytest
from sharedModules import registry
from sharedModules.errors import AccountError


def _set_accounts(monkeypatch, entries):
    monkeypatch.setenv("ACCOUNTS", json.dumps(entries))
    registry.all_accounts.cache_clear()


def _entry(alias: str, provider: str = "facebook") -> dict:
    return {
        "alias": alias,
        "provider": provider,
        "publish_enabled": False,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "policy": {},
    }


def test_single_account_is_the_default(monkeypatch):
    _set_accounts(monkeypatch, [_entry("only")])
    assert registry.resolve_alias("") == "only"


def test_multiple_accounts_require_the_parameter(monkeypatch):
    _set_accounts(monkeypatch, [_entry("a"), _entry("b", "instagram")])
    with pytest.raises(AccountError) as exc:
        registry.resolve_alias("")
    assert "a" in str(exc.value) and "b" in str(exc.value)
    assert registry.resolve_alias("b") == "b"


def test_unknown_alias_names_the_known_ones(monkeypatch):
    _set_accounts(monkeypatch, [_entry("only")])
    with pytest.raises(AccountError) as exc:
        registry.resolve_alias("typo")
    assert "only" in str(exc.value)


def test_no_accounts_configured(monkeypatch):
    _set_accounts(monkeypatch, [])
    with pytest.raises(AccountError):
        registry.resolve_alias("")


def test_sweep_resolution_fans_out_when_empty(monkeypatch):
    _set_accounts(monkeypatch, [_entry("b"), _entry("a", "instagram")])
    assert registry.resolve_sweep_aliases("") == ["a", "b"]
    assert registry.resolve_sweep_aliases("a") == ["a"]


def test_sweep_resolution_rejects_unknown_alias(monkeypatch):
    _set_accounts(monkeypatch, [_entry("only")])
    with pytest.raises(AccountError):
        registry.resolve_sweep_aliases("typo")


def test_record_is_not_connected_without_a_grant(monkeypatch, aws):
    _set_accounts(monkeypatch, [_entry("only")])
    record = registry.account_record("only")
    assert record["status"] == "not_connected"
    assert record["provider"] == "facebook"


def test_record_merges_stored_grant(monkeypatch, aws):
    from sharedModules import dynamo

    _set_accounts(monkeypatch, [_entry("only")])
    dynamo.put_account(
        "only",
        {
            "provider": "facebook",
            "account_id": "100000000000001",
            "display_name": "Example Page",
            "scopes": ["pages_show_list", "read_insights"],
            "status": "active",
        },
    )
    record = registry.account_record("only")
    assert record["status"] == "active"
    assert record["account_id"] == "100000000000001"
    assert "read_insights" in record["scopes"]


def test_require_scope_blocks_missing_scope(monkeypatch, aws):
    from sharedModules import dynamo

    _set_accounts(monkeypatch, [_entry("only")])
    dynamo.put_account(
        "only",
        {
            "provider": "facebook",
            "account_id": "x",
            "display_name": "p",
            "scopes": ["pages_show_list"],
            "status": "active",
        },
    )
    registry.require_scope("only", "pages_show_list")  # ok
    with pytest.raises(AccountError) as exc:
        registry.require_scope("only", "pages_manage_posts")
    assert "pages_manage_posts" in str(exc.value)


def test_require_scope_blocks_unconnected_account(monkeypatch, aws):
    _set_accounts(monkeypatch, [_entry("only")])
    with pytest.raises(AccountError) as exc:
        registry.require_scope("only", "read_insights")
    assert "not connected" in str(exc.value)
