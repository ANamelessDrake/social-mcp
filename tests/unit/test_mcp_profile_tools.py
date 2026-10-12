"""MCP profile tools: get_profile, draft_profile_edit, publish_profile_edit (M9)."""

import json
from dataclasses import asdict
from unittest import mock

import pytest
from sharedModules import approval, dynamo, registry
from sharedModules.models import ProfileInfo
from tools import draft_profile_edit as draft_tool
from tools import get_profile as get_tool
from tools import publish_profile_edit as publish_tool


def _entry(profile_edits_enabled: bool = True, mode: str = "manual") -> dict:
    return {
        "alias": "main-page",
        "provider": "facebook",
        "publish_enabled": True,
        "publish_mode": mode,
        "auto_for_kinds": [],
        "profile_edits_enabled": profile_edits_enabled,
        "policy": {},
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry()]))
    monkeypatch.setenv("PUBLISHER_FUNCTION_ARN", "arn:aws:lambda:us-east-1:0:function:x")
    registry.all_accounts.cache_clear()


# ------------------- get_profile -------------------


def test_get_profile_returns_serialized_profile(monkeypatch):
    info = ProfileInfo(
        account="main-page",
        handle="example",
        display_name="Example Page",
        about="about text",
        description="longer bio",
        website="https://justbardtech.com",
    )

    class _Prov:
        name = "facebook"
        from sharedModules.providers.base import Capability as _C

        capabilities = frozenset({_C.PROFILE_READ, _C.PROFILE_EDIT})

        def get_profile(self):
            return info

    monkeypatch.setattr(get_tool, "get_provider", lambda _: _Prov())
    out = get_tool.get_profile(account="main-page")
    assert out["ok"] is True
    assert out["profile"] == asdict(info)


def test_get_profile_refuses_when_capability_missing(monkeypatch):
    class _Prov:
        name = "stub"
        from sharedModules.providers.base import Capability as _C

        capabilities = frozenset({_C.TEXT})

    monkeypatch.setattr(get_tool, "get_provider", lambda _: _Prov())
    out = get_tool.get_profile(account="main-page")
    assert out["ok"] is False
    assert "profile metadata" in out["error"]


# ------------------- draft_profile_edit -------------------


def test_draft_profile_edit_stages_only_nonempty_fields(monkeypatch):
    class _Prov:
        name = "facebook"
        from sharedModules.providers.base import Capability as _C

        capabilities = frozenset({_C.PROFILE_READ, _C.PROFILE_EDIT})

    monkeypatch.setattr(draft_tool, "get_provider", lambda _: _Prov())
    out = draft_tool.draft_profile_edit(
        account="main-page",
        about="hello",
        website="https://justbardtech.com",
    )
    assert out["ok"] is True
    assert out["account"] == "main-page"
    assert out["patch"] == {
        "about": "hello",
        "website": "https://justbardtech.com",
    }
    assert out["approval"] == "pending"
    assert out["content_hash"] == approval.profile_hash("main-page", out["patch"])
    staged = dynamo.read_staged_profile_edit("main-page")
    assert staged is not None
    assert staged["patch"]["about"] == "hello"


def test_draft_profile_edit_refuses_empty_patch(monkeypatch):
    class _Prov:
        name = "facebook"
        from sharedModules.providers.base import Capability as _C

        capabilities = frozenset({_C.PROFILE_READ, _C.PROFILE_EDIT})

    monkeypatch.setattr(draft_tool, "get_provider", lambda _: _Prov())
    out = draft_tool.draft_profile_edit(account="main-page")
    assert out["ok"] is False
    assert "at least one of" in out["error"]
    assert dynamo.read_staged_profile_edit("main-page") is None


def test_draft_profile_edit_refuses_when_feature_flag_off(monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry(profile_edits_enabled=False)]))
    registry.all_accounts.cache_clear()

    class _Prov:
        name = "facebook"
        from sharedModules.providers.base import Capability as _C

        capabilities = frozenset({_C.PROFILE_READ, _C.PROFILE_EDIT})

    monkeypatch.setattr(draft_tool, "get_provider", lambda _: _Prov())
    out = draft_tool.draft_profile_edit(account="main-page", about="hello")
    assert out["ok"] is False
    assert "profile_edits_enabled: false" in out["error"]


def test_draft_profile_edit_refuses_when_capability_missing(monkeypatch):
    class _Prov:
        name = "instagram"
        from sharedModules.providers.base import Capability as _C

        capabilities = frozenset({_C.PROFILE_READ})  # no PROFILE_EDIT

    monkeypatch.setattr(draft_tool, "get_provider", lambda _: _Prov())
    out = draft_tool.draft_profile_edit(account="main-page", about="hello")
    assert out["ok"] is False
    assert "does not support profile edits" in out["error"]


def test_draft_profile_edit_parses_comma_separated_emails(monkeypatch):
    class _Prov:
        name = "facebook"
        from sharedModules.providers.base import Capability as _C

        capabilities = frozenset({_C.PROFILE_READ, _C.PROFILE_EDIT})

    monkeypatch.setattr(draft_tool, "get_provider", lambda _: _Prov())
    out = draft_tool.draft_profile_edit(account="main-page", emails="a@x.com, b@x.com")
    assert out["ok"] is True
    assert out["patch"]["emails"] == ["a@x.com", "b@x.com"]


def test_draft_profile_edit_auto_records_approval_in_auto_mode(monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry(mode="auto")]))
    registry.all_accounts.cache_clear()

    class _Prov:
        name = "facebook"
        from sharedModules.providers.base import Capability as _C

        capabilities = frozenset({_C.PROFILE_READ, _C.PROFILE_EDIT})

    monkeypatch.setattr(draft_tool, "get_provider", lambda _: _Prov())
    out = draft_tool.draft_profile_edit(account="main-page", about="hi")
    assert out["approval"] == "auto"
    assert dynamo.read_approval_record("profile:main-page") is not None


# ------------------- publish_profile_edit -------------------


def test_publish_profile_edit_sync_invokes_publisher(monkeypatch):
    patch = {"about": "hello"}
    dynamo.write_staged_profile_edit("main-page", patch, "agent")
    approval.record_profile_approval("main-page", patch, approved_by="op")

    seen: dict = {}

    class _FakeLambda:
        def invoke(self, FunctionName=None, InvocationType=None, Payload=None):  # noqa: N803
            seen["event"] = json.loads(Payload.decode())
            seen["name"] = FunctionName
            body = json.dumps({"ok": True, "account": "main-page"}).encode()
            return {"Payload": mock.MagicMock(read=lambda: body)}

    from tools import _common

    monkeypatch.setattr(_common, "_lambda_client", lambda: _FakeLambda())
    out = publish_tool.publish_profile_edit(account="main-page")
    assert out["ok"] is True
    assert seen["event"]["kind"] == "profile_edit"
    assert seen["event"]["account"] == "main-page"


def test_publish_profile_edit_errors_when_nothing_staged(monkeypatch):
    out = publish_tool.publish_profile_edit(account="main-page")
    assert out["ok"] is False
    assert "no staged change" in out["error"]


def test_publish_profile_edit_errors_when_approval_missing(monkeypatch):
    dynamo.write_staged_profile_edit("main-page", {"about": "x"}, "agent")
    out = publish_tool.publish_profile_edit(account="main-page")
    assert out["ok"] is False
    assert "no approval record" in out["error"]
