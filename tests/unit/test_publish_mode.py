"""Per-account publish_mode (DESIGN.md section 7.4)."""

import json

import pytest
from sharedModules import approval, dynamo, registry


def _entry(mode: str, auto_for_kinds=None, policy=None) -> dict:
    return {
        "alias": "main-page",
        "provider": "facebook",
        "publish_enabled": True,
        "publish_mode": mode,
        "auto_for_kinds": auto_for_kinds or [],
        "policy": policy or {},
    }


@pytest.fixture
def set_mode(monkeypatch, aws):
    def _set(mode: str, **kw):
        monkeypatch.setenv("ACCOUNTS", json.dumps([_entry(mode, **kw)]))
        registry.all_accounts.cache_clear()

    return _set


def _draft_post(**kwargs) -> dict:
    from tools.draft_post import draft_post

    return draft_post(**kwargs)


def test_manual_mode_leaves_approval_pending(set_mode):
    set_mode("manual")
    r = _draft_post(kind="text", text="hi")
    assert r["ok"]
    assert r["approval"] == "pending"
    assert dynamo.read_approval(r["draft_id"]) is None


def test_auto_mode_records_implicit_approval(set_mode):
    set_mode("auto")
    r = _draft_post(kind="text", text="hi")
    assert r["ok"]
    assert r["approval"] == "auto"
    rec = dynamo.read_approval(r["draft_id"])
    assert rec is not None
    assert rec["scope"] == "publish"
    # The implicit approval hash matches the current draft (verify would pass).
    approval.verify_approval(r["draft_id"], scope="publish")


def test_auto_for_matches_kind(set_mode):
    set_mode("auto_for", auto_for_kinds=["text"])
    r = _draft_post(kind="text", text="hi")
    assert r["approval"] == "auto"


def test_auto_for_skips_other_kinds(set_mode):
    set_mode("auto_for", auto_for_kinds=["text"])
    r = _draft_post(kind="image", text="x", media="https://cdn.example.com/a.jpg")
    assert r["approval"] == "pending"


def test_auto_publish_rejected_on_manual(set_mode):
    set_mode("manual")
    r = _draft_post(kind="text", text="hi", auto_publish=1)
    assert not r["ok"]
    assert "publish_mode" in r["error"]


def test_auto_mode_approval_is_voided_by_update(set_mode):
    """Core promise: even in auto mode, an edit after approval voids it."""
    set_mode("auto")
    created = _draft_post(kind="text", text="first")
    assert created["approval"] == "auto"

    from tools.update_draft import update_draft

    update_draft(draft_id=created["draft_id"], text="second")
    # Approval was deleted; auto mode does NOT re-record on update.
    assert dynamo.read_approval(created["draft_id"]) is None


def test_policy_still_runs_in_auto_mode(set_mode):
    set_mode("auto", policy={"banned_characters": ["\u2014"]})
    r = _draft_post(kind="text", text="hello \u2014 world")
    assert not r["ok"]
    assert "banned_characters" in r["error"]
