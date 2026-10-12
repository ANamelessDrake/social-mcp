"""End-to-end reply flow: stage -> policy warnings -> approve -> publish (M3)."""

import importlib
import json
import sys
from pathlib import Path

import pytest
from sharedModules import approval, dynamo, registry


def _entry(policy: dict | None = None) -> dict:
    return {
        "alias": "main-page",
        "provider": "facebook",
        "publish_enabled": True,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "policy": policy or {},
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry()]))
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    registry.all_accounts.cache_clear()


class _FakeProvider:
    def __init__(self):
        self.replies: list[tuple[str, str]] = []

    def reply_to_comment(self, comment_id: str, text: str):
        self.replies.append((comment_id, text))
        from sharedModules.models import Comment

        return Comment(
            account="main-page",
            comment_id=f"{comment_id}_reply",
            post_id="",
            thread_id=comment_id,
            created_time="",
            author="",
            text=text,
            is_hidden=False,
            reply_count=0,
        )


@pytest.fixture
def handler(monkeypatch):
    root = Path(__file__).resolve().parents[2]
    pub_dir = root / "infrastructure" / "lambdaFunctions" / "publisher"
    if str(pub_dir) not in sys.path:
        sys.path.insert(0, str(pub_dir))
    pub_app = importlib.import_module("app")
    importlib.reload(pub_app)
    fake = _FakeProvider()
    monkeypatch.setattr(pub_app, "get_provider", lambda _: fake, raising=False)
    return pub_app, fake


COMMENT = "100000000000001_900000000000400"


def test_reply_happy_path_runs_and_cleans_up(handler):
    pub_app, fake = handler
    dynamo.write_staged_reply(COMMENT, "main-page", "thanks for the catch", "agent")
    approval.record_reply_approval(COMMENT, "thanks for the catch", approved_by="op")

    out = pub_app.handler({"kind": "reply", "comment_id": COMMENT}, None)
    assert out["ok"] is True
    assert fake.replies == [(COMMENT, "thanks for the catch")]
    assert dynamo.read_staged_reply(COMMENT) is None
    assert dynamo.read_approval_record(f"reply:{COMMENT}") is None
    audit = dynamo.list_audit(account="main-page", limit=10)
    assert any(r["kind"] == "reply" and r["result"] == "ok" for r in audit)


def test_reply_voids_approval_on_text_change(handler):
    pub_app, fake = handler
    dynamo.write_staged_reply(COMMENT, "main-page", "polite reply", "agent")
    approval.record_reply_approval(COMMENT, "polite reply", approved_by="op")
    dynamo.write_staged_reply(COMMENT, "main-page", "RUDE REPLY", "agent")

    out = pub_app.handler({"kind": "reply", "comment_id": COMMENT}, None)
    assert out["ok"] is False
    assert "changed after approval" in out["error"]
    assert fake.replies == []


def test_reply_policy_runs_at_publish(handler, monkeypatch):
    """A config change between approval and publish must be honored."""
    pub_app, fake = handler
    dynamo.write_staged_reply(COMMENT, "main-page", "hello \u2014 world", "agent")
    approval.record_reply_approval(COMMENT, "hello \u2014 world", approved_by="op")

    # Flip the policy to ban that char after approval:
    monkeypatch.setenv(
        "ACCOUNTS",
        json.dumps([_entry({"banned_characters": ["\u2014"]})]),
    )
    registry.all_accounts.cache_clear()

    out = pub_app.handler({"kind": "reply", "comment_id": COMMENT}, None)
    assert out["ok"] is False
    assert "banned_characters" in out["error"]
    assert fake.replies == []
