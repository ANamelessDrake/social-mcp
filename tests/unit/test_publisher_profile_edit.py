"""End-to-end profile_edit flow in the Publisher Lambda (M9).

The provider is swapped for a fake so these tests exercise the stage +
approval + policy + Publisher dispatch logic without any Graph calls.
"""

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
        "profile_edits_enabled": True,
        "policy": {},
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry()]))
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    registry.all_accounts.cache_clear()


class _FakeProvider:
    def __init__(self):
        self.updates: list[dict] = []
        self.raise_on_update: Exception | None = None

    def update_profile(self, patch) -> None:
        if self.raise_on_update is not None:
            raise self.raise_on_update
        from dataclasses import asdict

        self.updates.append(asdict(patch))


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


def test_profile_edit_happy_path_runs_and_audits(handler):
    pub_app, fake = handler
    patch = {"about": "updated about", "website": "https://justbardtech.com"}
    dynamo.write_staged_profile_edit("main-page", patch, "agent")
    approval.record_profile_approval("main-page", patch, approved_by="op")

    out = pub_app.handler({"kind": "profile_edit", "account": "main-page", "actor": "ops"}, None)
    assert out["ok"] is True
    assert out["account"] == "main-page"
    assert len(fake.updates) == 1
    assert fake.updates[0]["about"] == "updated about"
    assert fake.updates[0]["website"] == "https://justbardtech.com"
    audit = dynamo.list_audit(account="main-page", limit=10)
    assert any(row["kind"] == "profile_edit" and row["result"] == "ok" for row in audit)
    # Staged + approval cleaned up on success
    assert dynamo.read_staged_profile_edit("main-page") is None
    assert dynamo.read_approval_record("profile:main-page") is None


def test_profile_edit_voids_approval_when_patch_changes(handler):
    pub_app, fake = handler
    first = {"about": "first"}
    dynamo.write_staged_profile_edit("main-page", first, "agent")
    approval.record_profile_approval("main-page", first, approved_by="op")
    # Agent re-stages with a different patch after the operator approved:
    dynamo.write_staged_profile_edit("main-page", {"about": "SECOND"}, "agent")

    out = pub_app.handler({"kind": "profile_edit", "account": "main-page"}, None)
    assert out["ok"] is False
    assert "changed after approval" in out["error"]
    assert fake.updates == []
    audit = dynamo.list_audit(account="main-page", limit=10)
    assert any(
        row["kind"] == "profile_edit" and row["result"] == "error:approval_mismatch"
        for row in audit
    )


def test_profile_edit_without_approval_refuses(handler):
    pub_app, fake = handler
    dynamo.write_staged_profile_edit("main-page", {"about": "x"}, "agent")
    out = pub_app.handler({"kind": "profile_edit", "account": "main-page"}, None)
    assert out["ok"] is False
    assert "no approval record" in out["error"]
    assert fake.updates == []


def test_profile_edit_reruns_policy(handler, monkeypatch):
    pub_app, fake = handler
    # Approve a clean patch, then re-stage one that violates a newly-added
    # policy (simulates a config change between approve and publish).
    patch = {"about": "clean"}
    dynamo.write_staged_profile_edit("main-page", patch, "agent")
    approval.record_profile_approval("main-page", patch, approved_by="op")
    # Change the live config to ban the word "clean"
    entry = _entry()
    entry["policy"] = {"banned_words": ["clean"]}
    monkeypatch.setenv("ACCOUNTS", json.dumps([entry]))
    registry.all_accounts.cache_clear()

    out = pub_app.handler({"kind": "profile_edit", "account": "main-page"}, None)
    assert out["ok"] is False
    assert "policy" in out["error"]
    assert fake.updates == []


def test_profile_edit_missing_account_errors(handler):
    pub_app, _ = handler
    out = pub_app.handler({"kind": "profile_edit"}, None)
    assert out["ok"] is False
    assert "missing account" in out["error"]
