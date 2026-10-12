"""Hide flow: queued-first-time, standing-after, revocation, sweep (M3)."""

import importlib
import json
import sys
from pathlib import Path

import pytest
from sharedModules import dynamo, registry


def _entry() -> dict:
    return {
        "alias": "main-page",
        "provider": "facebook",
        "publish_enabled": True,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "policy": {},
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry()]))
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    registry.all_accounts.cache_clear()


class _FakeProvider:
    def __init__(self):
        self.hides: list[str] = []

    def hide_comment(self, comment_id: str) -> None:
        self.hides.append(comment_id)


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


C1 = "cmt_1"
C2 = "cmt_2"


def test_hide_without_standing_approval_refuses(handler):
    pub_app, fake = handler
    out = pub_app.handler(
        {"kind": "hide", "account": "main-page", "comment_id": C1}, None
    )
    assert out["ok"] is False
    assert "standing hide approval" in out["error"]
    assert fake.hides == []


def test_hide_with_standing_approval_runs_and_audits_per_comment(handler):
    pub_app, fake = handler
    dynamo.write_hide_standing("main-page", approved_by="op")
    # Simulate two tool-staged hide requests:
    dynamo.write_hide_request("main-page", C1, "agent")
    dynamo.write_hide_request("main-page", C2, "agent")

    out1 = pub_app.handler(
        {"kind": "hide", "account": "main-page", "comment_id": C1}, None
    )
    out2 = pub_app.handler(
        {"kind": "hide", "account": "main-page", "comment_id": C2}, None
    )
    assert out1["ok"] is True
    assert out2["ok"] is True
    assert fake.hides == [C1, C2]

    audit = dynamo.list_audit(account="main-page", limit=20)
    rows = [r for r in audit if r["kind"] == "hide" and r["result"] == "ok"]
    assert {r["comment_id"] for r in rows} == {C1, C2}

    # Requests cleaned up as they fire
    remaining = dynamo.list_hide_requests("main-page")
    assert remaining == []


def test_hide_standing_revocation_blocks_next_hide(handler):
    pub_app, fake = handler
    dynamo.write_hide_standing("main-page", approved_by="op")
    pub_app.handler({"kind": "hide", "account": "main-page", "comment_id": C1}, None)
    assert fake.hides == [C1]

    dynamo.delete_hide_standing("main-page")
    out = pub_app.handler(
        {"kind": "hide", "account": "main-page", "comment_id": C2}, None
    )
    assert out["ok"] is False
    assert fake.hides == [C1]  # unchanged
