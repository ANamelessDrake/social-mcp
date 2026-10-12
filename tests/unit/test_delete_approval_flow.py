"""End-to-end delete flow: stage -> approve -> Publisher -> audit (M3)."""

import importlib
import json
import sys
from pathlib import Path

import pytest
from sharedModules import approval, dynamo, registry


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
        self.deletes: list[str] = []

    def delete_post(self, post_id: str) -> None:
        self.deletes.append(post_id)


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


POST = "100000000000001_900000000000099"


def test_delete_happy_path_runs_and_audits_reason(handler):
    pub_app, fake = handler
    dynamo.write_staged_delete(POST, "main-page", "spam escalated", "agent")
    approval.record_delete_approval(POST, "spam escalated", approved_by="op")

    out = pub_app.handler({"kind": "delete", "post_id": POST}, None)
    assert out["ok"] is True
    assert fake.deletes == [POST]
    audit = dynamo.list_audit(account="main-page", limit=10)
    row = next(r for r in audit if r["kind"] == "delete" and r["result"] == "ok")
    assert row.get("reason") == "spam escalated"
    assert row.get("post_id") == POST
    assert dynamo.read_staged_delete(POST) is None
    assert dynamo.read_approval_record(f"delete:{POST}") is None


def test_delete_reason_change_voids_approval(handler):
    pub_app, fake = handler
    dynamo.write_staged_delete(POST, "main-page", "first reason", "agent")
    approval.record_delete_approval(POST, "first reason", approved_by="op")
    # Agent re-stages with a different reason post-approval:
    dynamo.write_staged_delete(POST, "main-page", "DIFFERENT REASON", "agent")

    out = pub_app.handler({"kind": "delete", "post_id": POST}, None)
    assert out["ok"] is False
    assert "changed after approval" in out["error"]
    assert fake.deletes == []
