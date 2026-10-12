"""draft_post / update_draft / discard_draft / list_drafts (DESIGN.md 7)."""

import json

import pytest
from sharedModules import dynamo, registry


def _manual(alias: str = "main-page") -> dict:
    return {
        "alias": alias,
        "provider": "facebook",
        "publish_enabled": True,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "policy": {},
    }


@pytest.fixture(autouse=True)
def _roster(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_manual()]))
    registry.all_accounts.cache_clear()


def _tool(name: str):
    from tools import discard_draft as dd
    from tools import draft_post as dp
    from tools import get_draft as gd
    from tools import list_drafts as ld
    from tools import update_draft as ud

    return {
        "draft_post": dp.draft_post,
        "update_draft": ud.update_draft,
        "discard_draft": dd.discard_draft,
        "get_draft": gd.get_draft,
        "list_drafts": ld.list_drafts,
    }[name]


def test_draft_post_text_returns_id_and_pending_approval():
    result = _tool("draft_post")(kind="text", text="hello world")
    assert result["ok"]
    assert result["approval"] == "pending"
    assert result["publish_mode"] == "manual"
    assert "draft_id" in result
    stored = dynamo.read_draft(result["draft_id"])
    assert stored is not None
    assert stored["kind"] == "text"


def test_draft_post_link_without_a_link_fails():
    result = _tool("draft_post")(kind="link", text="see this")
    assert not result["ok"]
    assert "link" in result["error"].lower()


def test_draft_post_multi_image_needs_two_urls():
    one = _tool("draft_post")(
        kind="multi_image", text="pic", media="https://cdn.example.com/one.jpg"
    )
    assert not one["ok"]
    two = _tool("draft_post")(
        kind="multi_image",
        text="pics",
        media="https://cdn.example.com/one.jpg,https://cdn.example.com/two.jpg",
    )
    assert two["ok"]


def test_update_draft_invalidates_approval():
    from sharedModules import approval

    created = _tool("draft_post")(kind="text", text="first version")
    draft_id = created["draft_id"]
    approval.record_approval(draft_id=draft_id, scope="publish", approved_by="op")
    assert dynamo.read_approval(draft_id) is not None

    _tool("update_draft")(draft_id=draft_id, text="second version")
    assert dynamo.read_approval(draft_id) is None


def test_discard_draft_sets_status_and_drops_approval():
    from sharedModules import approval

    created = _tool("draft_post")(kind="text", text="to be discarded")
    draft_id = created["draft_id"]
    approval.record_approval(draft_id=draft_id, scope="publish", approved_by="op")

    result = _tool("discard_draft")(draft_id=draft_id)
    assert result["ok"]
    assert dynamo.read_draft(draft_id)["status"] == "discarded"
    assert dynamo.read_approval(draft_id) is None


def test_list_drafts_newest_first():
    import time

    a = _tool("draft_post")(kind="text", text="first")
    time.sleep(0.01)
    b = _tool("draft_post")(kind="text", text="second")
    result = _tool("list_drafts")()
    ids = [r["draft_id"] for r in result["drafts"]]
    assert ids.index(b["draft_id"]) < ids.index(a["draft_id"])


def test_get_draft_reports_approval_state():
    from sharedModules import approval

    created = _tool("draft_post")(kind="text", text="hello")
    draft_id = created["draft_id"]
    pending = _tool("get_draft")(draft_id=draft_id)
    assert pending["approval"]["state"] == "pending"

    approval.record_approval(draft_id=draft_id, scope="publish", approved_by="op")
    now_approved = _tool("get_draft")(draft_id=draft_id)
    assert now_approved["approval"]["state"] == "approved"
