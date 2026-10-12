"""Approval hashing + verification (DESIGN.md section 7.2)."""

import json

import pytest
from sharedModules import approval, dynamo


def _entry(alias: str = "main-page") -> dict:
    return {
        "alias": alias,
        "provider": "facebook",
        "publish_enabled": False,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "policy": {},
    }


def _draft(draft_id: str = "d1", **overrides) -> dict:
    base = {
        "draft_id": draft_id,
        "account": "main-page",
        "kind": "text",
        "text": "hello",
        "link": "",
        "media": [],
        "schedule": "",
        "status": "drafted",
        "created_by": "test",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _roster(monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry()]))


def test_hash_is_deterministic_regardless_of_field_order():
    from types import SimpleNamespace

    a = SimpleNamespace(
        account="x", kind="text", text="abc", link="", media=[], schedule=""
    )
    b = SimpleNamespace(
        schedule="", media=[], link="", text="abc", kind="text", account="x"
    )
    assert approval.hash_draft(a) == approval.hash_draft(b)


def test_media_etag_change_voids_the_hash():
    from types import SimpleNamespace

    before = SimpleNamespace(
        account="x",
        kind="image",
        text="",
        link="",
        media=[{"etag": "aaa", "mime": "image/jpeg"}],
        schedule="",
    )
    after = SimpleNamespace(
        account="x",
        kind="image",
        text="",
        link="",
        media=[{"etag": "bbb", "mime": "image/jpeg"}],
        schedule="",
    )
    assert approval.hash_draft(before) != approval.hash_draft(after)


def test_media_order_affects_hash():
    """Carousel order is semantically load-bearing; reorder voids approval."""
    from types import SimpleNamespace

    a = SimpleNamespace(
        account="x",
        kind="multi_image",
        text="",
        link="",
        media=[{"etag": "1", "mime": "image/jpeg"}, {"etag": "2", "mime": "image/jpeg"}],
        schedule="",
    )
    b = SimpleNamespace(
        account="x",
        kind="multi_image",
        text="",
        link="",
        media=[{"etag": "2", "mime": "image/jpeg"}, {"etag": "1", "mime": "image/jpeg"}],
        schedule="",
    )
    assert approval.hash_draft(a) != approval.hash_draft(b)


def test_record_and_verify_match(aws):
    dynamo.write_draft(_draft())
    rec = approval.record_approval(draft_id="d1", scope="publish", approved_by="op")
    assert rec["content_hash"]
    verified = approval.verify_approval("d1", scope="publish")
    assert verified["approved_by"] == "op"


def test_verify_fails_when_draft_was_edited(aws):
    dynamo.write_draft(_draft(text="first version"))
    approval.record_approval(draft_id="d1", scope="publish", approved_by="op")
    # Edit after approval
    dynamo.update_draft_fields("d1", {"text": "second version"})
    with pytest.raises(approval.ApprovalError) as exc:
        approval.verify_approval("d1", scope="publish")
    assert "edited after approval" in str(exc.value)


def test_verify_fails_without_an_approval(aws):
    dynamo.write_draft(_draft())
    with pytest.raises(approval.ApprovalError) as exc:
        approval.verify_approval("d1", scope="publish")
    assert "no approval record" in str(exc.value)


def test_invalidate_approval_drops_the_record(aws):
    dynamo.write_draft(_draft())
    approval.record_approval(draft_id="d1", scope="publish", approved_by="op")
    approval.invalidate_approval("d1")
    assert dynamo.read_approval("d1") is None


def test_verify_rejects_a_mismatched_scope(aws):
    dynamo.write_draft(_draft())
    approval.record_approval(draft_id="d1", scope="publish", approved_by="op")
    with pytest.raises(approval.ApprovalError) as exc:
        approval.verify_approval("d1", scope="schedule")
    assert "scope" in str(exc.value)
