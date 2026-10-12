"""Publisher Lambda handler (DESIGN.md section 2 + 7.3).

The Publisher is the only path that reaches Meta. These tests swap the
provider for a fake so the handler's approval re-check, policy re-run,
kill-switch handling, and audit path are the thing under test, not the
Graph API wire format (that is `test_facebook_mapping.py`).
"""

import json

import pytest
from sharedModules import approval, dynamo, registry
from sharedModules.models import PublishResult


def _entry() -> dict:
    return {
        "alias": "main-page",
        "provider": "facebook",
        "publish_enabled": True,
        "publish_mode": "auto",
        "auto_for_kinds": [],
        "policy": {},
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry()]))
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    registry.all_accounts.cache_clear()


def _draft(draft_id: str = "d1", **overrides) -> dict:
    base = {
        "draft_id": draft_id,
        "account": "main-page",
        "kind": "text",
        "text": "hello",
        "link": "",
        "media": [],
        "schedule": "",
        "status": "approved",
        "created_by": "test",
    }
    base.update(overrides)
    return base


class _FakeProvider:
    def __init__(self, result: PublishResult | None = None, raises: Exception | None = None):
        self._result = result
        self._raises = raises
        self.calls: list = []

    def publish(self, draft):
        self.calls.append(draft)
        if self._raises is not None:
            raise self._raises
        return self._result or PublishResult(
            account="main-page",
            post_id="100_900",
            permalink="https://www.facebook.com/100_900",
            kind="text",
        )


@pytest.fixture
def handler(monkeypatch):
    """Load the Publisher handler and swap get_provider at that scope."""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    pub_dir = root / "infrastructure" / "lambdaFunctions" / "publisher"
    sys.path.insert(0, str(pub_dir))
    import importlib

    pub_app = importlib.import_module("app")
    # Freshen in case a previous test loaded it:
    importlib.reload(pub_app)
    return pub_app


def test_publisher_happy_path_posts_and_audits(handler, monkeypatch):
    fake = _FakeProvider()
    monkeypatch.setattr(handler, "get_provider", lambda _: fake, raising=False)

    dynamo.write_draft(_draft())
    approval.record_approval(draft_id="d1", scope="publish", approved_by="op")

    out = handler.handler({"draft_id": "d1"}, None)
    assert out["ok"] is True
    assert out["post_id"] == "100_900"
    assert dynamo.read_draft("d1")["status"] == "published"
    audit = dynamo.list_audit(account="main-page", limit=10)
    assert any(row.get("kind") == "publish" and row.get("result") == "ok" for row in audit)


def test_publisher_refuses_an_edit_after_approval(handler, monkeypatch):
    """The gate: edit between approve and publish voids the record."""
    fake = _FakeProvider()
    monkeypatch.setattr(handler, "get_provider", lambda _: fake, raising=False)

    dynamo.write_draft(_draft(text="first"))
    approval.record_approval(draft_id="d1", scope="publish", approved_by="op")
    # Someone edited the draft after approval:
    dynamo.update_draft_fields("d1", {"text": "SECOND, scary"})

    out = handler.handler({"draft_id": "d1"}, None)
    assert out["ok"] is False
    assert "edited after approval" in out["error"]
    assert dynamo.read_draft("d1")["status"] == "failed"
    assert fake.calls == []  # Meta was never called


def test_publisher_blocks_on_policy_drift(handler, monkeypatch):
    """Policy change between approval and publish must not slip past."""
    fake = _FakeProvider()
    monkeypatch.setattr(handler, "get_provider", lambda _: fake, raising=False)

    dynamo.write_draft(_draft(text="hello \u2014 world"))
    approval.record_approval(draft_id="d1", scope="publish", approved_by="op")

    # Flip the roster so the policy now bans the character the draft contains:
    entry = _entry()
    entry["policy"] = {"banned_characters": ["\u2014"]}
    monkeypatch.setenv("ACCOUNTS", json.dumps([entry]))
    registry.all_accounts.cache_clear()

    out = handler.handler({"draft_id": "d1"}, None)
    assert out["ok"] is False
    assert "banned_characters" in out["error"]
    assert fake.calls == []


def test_publisher_bubbles_provider_errors(handler, monkeypatch):
    from sharedModules.errors import AccountError

    fake = _FakeProvider(raises=AccountError("Meta token expired"))
    monkeypatch.setattr(handler, "get_provider", lambda _: fake, raising=False)

    dynamo.write_draft(_draft())
    approval.record_approval(draft_id="d1", scope="publish", approved_by="op")

    out = handler.handler({"draft_id": "d1"}, None)
    assert out["ok"] is False
    assert "Meta token expired" in out["error"]
    assert dynamo.read_draft("d1")["status"] == "failed"


def test_global_kill_switch_blocks_publish(handler, monkeypatch):
    fake = _FakeProvider()
    monkeypatch.setattr(handler, "get_provider", lambda _: fake, raising=False)

    dynamo.write_draft(_draft())
    approval.record_approval(draft_id="d1", scope="publish", approved_by="op")
    dynamo.set_publishing_disabled(True, actor="operator")

    out = handler.handler({"draft_id": "d1"}, None)
    assert out["ok"] is False
    assert "globally disabled" in out["error"]
    assert fake.calls == []
