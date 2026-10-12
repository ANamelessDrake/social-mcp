"""MCP delete_post tool: stage-only flow; auto_publish refused by design (M10)."""

import json

import pytest
from sharedModules import dynamo, registry
from tools import delete_post as delete_tool


def _entry(auto_verbs: list[str] | None = None) -> dict:
    return {
        "alias": "main-page",
        "provider": "facebook",
        "publish_enabled": True,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "auto_verbs": list(auto_verbs or []),
        "policy": {},
    }


class _Prov:
    name = "facebook"
    from sharedModules.providers.base import Capability as _C

    capabilities = frozenset({_C.EDIT, _C.DELETE, _C.COMMENTS})


POST = "100000000000001_900000000000099"


@pytest.fixture(autouse=True)
def _env(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry()]))
    monkeypatch.setenv("PUBLISHER_FUNCTION_ARN", "arn:aws:lambda:us-east-1:0:function:x")
    registry.all_accounts.cache_clear()
    monkeypatch.setattr(delete_tool, "get_provider", lambda _: _Prov())


def test_delete_stages_and_asks_for_approval(monkeypatch):
    out = delete_tool.delete_post(post_id=POST, reason="wrong link", account="main-page")
    assert out["ok"] is True
    assert out["staged_change"] == "delete"
    assert "approve-delete" in out["next"]
    assert dynamo.read_staged_delete(POST) is not None


def test_delete_auto_publish_is_refused_even_with_delete_in_auto_verbs(monkeypatch):
    # delete is hardcoded manual; auto_verbs cannot opt it in. We put the
    # string in anyway to prove the refusal is design-level, not config-level.
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry(auto_verbs=["delete"])]))
    registry.all_accounts.cache_clear()
    out = delete_tool.delete_post(post_id=POST, reason="x", account="main-page", auto_publish=1)
    assert out["ok"] is False
    assert "never auto-published" in out["error"]
    # Nothing staged; nothing approved.
    assert dynamo.read_staged_delete(POST) is None
    assert dynamo.read_approval_record(f"delete:{POST}") is None


def test_delete_auto_publish_refused_without_the_flag(monkeypatch):
    out = delete_tool.delete_post(post_id=POST, account="main-page", auto_publish=1)
    assert out["ok"] is False
    assert "never auto-published" in out["error"]
    assert dynamo.read_staged_delete(POST) is None
