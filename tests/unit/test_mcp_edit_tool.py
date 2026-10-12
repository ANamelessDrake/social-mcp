"""MCP edit_post tool: default stage flow + M10 auto_verbs opt-in."""

import json
from unittest import mock

import pytest
from sharedModules import approval, dynamo, registry
from tools import edit_post as edit_tool


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
    monkeypatch.setattr(edit_tool, "get_provider", lambda _: _Prov())


# -------- default (manual) flow --------


def test_edit_post_stages_and_asks_for_approval(monkeypatch):
    out = edit_tool.edit_post(post_id=POST, text="new text", account="main-page")
    assert out["ok"] is True
    assert out["staged_change"] == "edit"
    assert "approve-edit" in out["next"]
    staged = dynamo.read_staged_edit(POST)
    assert staged is not None
    assert staged["text"] == "new text"
    assert dynamo.read_approval_record(f"edit:{POST}") is None


def test_edit_post_requires_post_id(monkeypatch):
    out = edit_tool.edit_post(post_id="", text="x", account="main-page")
    assert out["ok"] is False
    assert "post_id" in out["error"]


def test_edit_post_requires_text(monkeypatch):
    out = edit_tool.edit_post(post_id=POST, text="   ", account="main-page")
    assert out["ok"] is False
    assert "text is required" in out["error"]


def test_edit_post_refuses_when_capability_missing(monkeypatch):
    class _Thin:
        name = "stub"
        from sharedModules.providers.base import Capability as _C

        capabilities = frozenset({_C.TEXT})

    monkeypatch.setattr(edit_tool, "get_provider", lambda _: _Thin())
    out = edit_tool.edit_post(post_id=POST, text="x", account="main-page")
    assert out["ok"] is False
    assert "does not support edits" in out["error"]


# -------- M10 auto_verbs path --------


def test_edit_auto_publish_refuses_without_opt_in(monkeypatch):
    out = edit_tool.edit_post(post_id=POST, text="x", account="main-page", auto_publish=1)
    assert out["ok"] is False
    assert "auto_verbs" in out["error"]
    # Must not have staged or approved anything.
    assert dynamo.read_staged_edit(POST) is None
    assert dynamo.read_approval_record(f"edit:{POST}") is None


def test_edit_auto_publish_fires_publisher_when_opted_in(monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry(auto_verbs=["edit"])]))
    registry.all_accounts.cache_clear()

    seen: dict = {}

    class _FakeLambda:
        def invoke(self, FunctionName=None, InvocationType=None, Payload=None):  # noqa: N803
            seen["event"] = json.loads(Payload.decode())
            seen["name"] = FunctionName
            body = json.dumps({"ok": True, "post_id": POST}).encode()
            return {"Payload": mock.MagicMock(read=lambda: body)}

    from tools import _common

    monkeypatch.setattr(_common, "_lambda_client", lambda: _FakeLambda())

    out = edit_tool.edit_post(post_id=POST, text="hello", account="main-page", auto_publish=1)
    assert out["ok"] is True
    assert out["post_id"] == POST
    assert seen["event"]["kind"] == "edit"
    assert seen["event"]["post_id"] == POST

    # Approval record must exist and match the current text.
    rec = dynamo.read_approval_record(f"edit:{POST}")
    assert rec is not None
    assert rec["content_hash"] == approval.hash_edit("hello")
    assert dynamo.read_staged_edit(POST) is not None


def test_edit_auto_publish_zero_is_identical_to_default(monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry(auto_verbs=["edit"])]))
    registry.all_accounts.cache_clear()
    out = edit_tool.edit_post(post_id=POST, text="new text", account="main-page", auto_publish=0)
    assert out["ok"] is True
    assert out["staged_change"] == "edit"
    assert "approve-edit" in out["next"]
    assert dynamo.read_approval_record(f"edit:{POST}") is None
