"""MCP hide_comment tool: default stage-standing flow + M10 auto_verbs opt-in."""

import json
from unittest import mock

import pytest
from sharedModules import dynamo, registry
from tools import hide_comment as hide_tool


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

    capabilities = frozenset({_C.COMMENTS, _C.EDIT, _C.DELETE})


COMMENT = "100000000000001_900000000000099_abc"


@pytest.fixture(autouse=True)
def _env(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry()]))
    monkeypatch.setenv("PUBLISHER_FUNCTION_ARN", "arn:aws:lambda:us-east-1:0:function:x")
    registry.all_accounts.cache_clear()
    monkeypatch.setattr(hide_tool, "get_provider", lambda _: _Prov())


# -------- default (no standing approval) flow --------


def test_hide_without_standing_queues_the_request(monkeypatch):
    out = hide_tool.hide_comment(comment_id=COMMENT, account="main-page")
    assert out["ok"] is True
    assert out["staged_change"] == "hide"
    assert "approve-hide" in out["next"]
    assert dynamo.read_hide_standing("main-page") is None


def test_hide_with_standing_calls_publisher(monkeypatch):
    dynamo.write_hide_standing("main-page", approved_by="op")

    seen: dict = {}

    class _FakeLambda:
        def invoke(self, FunctionName=None, InvocationType=None, Payload=None):  # noqa: N803
            seen["event"] = json.loads(Payload.decode())
            body = json.dumps({"ok": True, "comment_id": COMMENT, "account": "main-page"}).encode()
            return {"Payload": mock.MagicMock(read=lambda: body)}

    from tools import _common

    monkeypatch.setattr(_common, "_lambda_client", lambda: _FakeLambda())

    out = hide_tool.hide_comment(comment_id=COMMENT, account="main-page")
    assert out["ok"] is True
    assert seen["event"]["kind"] == "hide"
    assert seen["event"]["comment_id"] == COMMENT


def test_hide_requires_comment_id(monkeypatch):
    out = hide_tool.hide_comment(comment_id="", account="main-page")
    assert out["ok"] is False
    assert "comment_id" in out["error"]


# -------- M10 auto_verbs path --------


def test_hide_auto_publish_refuses_without_opt_in(monkeypatch):
    out = hide_tool.hide_comment(comment_id=COMMENT, account="main-page", auto_publish=1)
    assert out["ok"] is False
    assert "auto_verbs" in out["error"]
    assert dynamo.read_hide_standing("main-page") is None


def test_hide_auto_publish_grants_standing_and_fires(monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry(auto_verbs=["hide"])]))
    registry.all_accounts.cache_clear()

    seen: dict = {}

    class _FakeLambda:
        def invoke(self, FunctionName=None, InvocationType=None, Payload=None):  # noqa: N803
            seen["event"] = json.loads(Payload.decode())
            body = json.dumps({"ok": True, "comment_id": COMMENT, "account": "main-page"}).encode()
            return {"Payload": mock.MagicMock(read=lambda: body)}

    from tools import _common

    monkeypatch.setattr(_common, "_lambda_client", lambda: _FakeLambda())

    out = hide_tool.hide_comment(comment_id=COMMENT, account="main-page", auto_publish=1)
    assert out["ok"] is True
    assert dynamo.read_hide_standing("main-page") is not None
    assert seen["event"]["kind"] == "hide"


def test_hide_auto_publish_reuses_existing_standing(monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry(auto_verbs=["hide"])]))
    registry.all_accounts.cache_clear()
    dynamo.write_hide_standing("main-page", approved_by="op")

    class _FakeLambda:
        def invoke(self, FunctionName=None, InvocationType=None, Payload=None):  # noqa: N803
            body = json.dumps({"ok": True, "comment_id": COMMENT, "account": "main-page"}).encode()
            return {"Payload": mock.MagicMock(read=lambda: body)}

    from tools import _common

    monkeypatch.setattr(_common, "_lambda_client", lambda: _FakeLambda())

    out = hide_tool.hide_comment(comment_id=COMMENT, account="main-page", auto_publish=1)
    assert out["ok"] is True
    standing = dynamo.read_hide_standing("main-page")
    assert standing is not None
    # The pre-existing standing is NOT overwritten with "auto_verbs" marker.
    assert standing["approved_by"] == "op"
