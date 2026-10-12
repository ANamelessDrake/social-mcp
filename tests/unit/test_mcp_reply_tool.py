"""MCP reply_to_comment tool: default stage flow + M10 auto_verbs opt-in."""

import json
from unittest import mock

import pytest
from sharedModules import approval, dynamo, registry
from tools import reply_to_comment as reply_tool


def _entry(auto_verbs: list[str] | None = None, policy: dict | None = None) -> dict:
    return {
        "alias": "main-page",
        "provider": "facebook",
        "publish_enabled": True,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "auto_verbs": list(auto_verbs or []),
        "policy": policy or {},
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
    monkeypatch.setattr(reply_tool, "get_provider", lambda _: _Prov())


# -------- default (manual) flow --------


def test_reply_stages_and_asks_for_approval(monkeypatch):
    out = reply_tool.reply_to_comment(comment_id=COMMENT, text="hello", account="main-page")
    assert out["ok"] is True
    assert out["staged_change"] == "reply"
    assert "approve-reply" in out["next"]
    assert dynamo.read_staged_reply(COMMENT) is not None
    assert dynamo.read_approval_record(f"reply:{COMMENT}") is None


def test_reply_requires_comment_id(monkeypatch):
    out = reply_tool.reply_to_comment(comment_id="", text="x", account="main-page")
    assert out["ok"] is False
    assert "comment_id" in out["error"]


def test_reply_requires_text(monkeypatch):
    out = reply_tool.reply_to_comment(comment_id=COMMENT, text="  ", account="main-page")
    assert out["ok"] is False
    assert "text is required" in out["error"]


def test_reply_blocked_by_policy_before_stage(monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry(policy={"banned_words": ["banned"]})]))
    registry.all_accounts.cache_clear()
    out = reply_tool.reply_to_comment(
        comment_id=COMMENT, text="this is banned", account="main-page"
    )
    assert out["ok"] is False
    assert "policy" in out["error"]
    assert dynamo.read_staged_reply(COMMENT) is None


# -------- M10 auto_verbs path --------


def test_reply_auto_publish_refuses_without_opt_in(monkeypatch):
    out = reply_tool.reply_to_comment(
        comment_id=COMMENT, text="x", account="main-page", auto_publish=1
    )
    assert out["ok"] is False
    assert "auto_verbs" in out["error"]
    assert dynamo.read_staged_reply(COMMENT) is None
    assert dynamo.read_approval_record(f"reply:{COMMENT}") is None


def test_reply_auto_publish_fires_publisher_when_opted_in(monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry(auto_verbs=["reply"])]))
    registry.all_accounts.cache_clear()

    seen: dict = {}

    class _FakeLambda:
        def invoke(self, FunctionName=None, InvocationType=None, Payload=None):  # noqa: N803
            seen["event"] = json.loads(Payload.decode())
            body = json.dumps({"ok": True, "comment_id": COMMENT, "reply_id": "y"}).encode()
            return {"Payload": mock.MagicMock(read=lambda: body)}

    from tools import _common

    monkeypatch.setattr(_common, "_lambda_client", lambda: _FakeLambda())

    out = reply_tool.reply_to_comment(
        comment_id=COMMENT, text="hello", account="main-page", auto_publish=1
    )
    assert out["ok"] is True
    assert seen["event"]["kind"] == "reply"
    assert seen["event"]["comment_id"] == COMMENT

    rec = dynamo.read_approval_record(f"reply:{COMMENT}")
    assert rec is not None
    assert rec["content_hash"] == approval.hash_reply("hello")
    assert dynamo.read_staged_reply(COMMENT) is not None


def test_reply_auto_publish_policy_fails_before_fire(monkeypatch):
    monkeypatch.setenv(
        "ACCOUNTS",
        json.dumps([_entry(auto_verbs=["reply"], policy={"banned_words": ["banned"]})]),
    )
    registry.all_accounts.cache_clear()
    out = reply_tool.reply_to_comment(
        comment_id=COMMENT, text="banned content", account="main-page", auto_publish=1
    )
    assert out["ok"] is False
    assert "policy" in out["error"]
    assert dynamo.read_staged_reply(COMMENT) is None
