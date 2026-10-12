"""Profile-edit approval hashing + verification (M9, DESIGN.md section 7.7)."""

import json

import pytest
from sharedModules import approval


def _entry() -> dict:
    return {
        "alias": "main-page",
        "provider": "facebook",
        "publish_enabled": False,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "profile_edits_enabled": True,
        "policy": {},
    }


@pytest.fixture(autouse=True)
def _roster(monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry()]))


def test_hash_stable_regardless_of_input_key_order():
    patch_a = {"website": "https://x.example", "about": "hi", "phone": ""}
    patch_b = {"phone": "", "about": "hi", "website": "https://x.example"}
    assert approval.profile_hash("main-page", patch_a) == approval.profile_hash(
        "main-page", patch_b
    )


def test_hash_drops_empty_fields():
    with_empty = {"about": "hi", "website": "", "phone": "   "}
    without = {"about": "hi"}
    # The implementation only keeps truthy values; empty strings and
    # falsy entries do not contribute to the hash. A whitespace string
    # is still truthy (Python-truthy), so it changes the hash; the
    # expected canonicalization keeps whitespace strings. Guard that.
    assert approval.profile_hash("main-page", with_empty) != approval.profile_hash(
        "main-page", without
    )
    assert approval.profile_hash(
        "main-page", {"about": "hi", "website": ""}
    ) == approval.profile_hash("main-page", {"about": "hi"})


def test_hash_sorts_email_list():
    a = {"emails": ["z@example.com", "a@example.com"]}
    b = {"emails": ["a@example.com", "z@example.com"]}
    assert approval.profile_hash("acct", a) == approval.profile_hash("acct", b)


def test_hash_binds_to_account():
    patch = {"about": "shared text"}
    assert approval.profile_hash("acct-1", patch) != approval.profile_hash("acct-2", patch)


def test_record_and_verify_match(aws):
    patch = {"about": "hello", "website": "https://justbardtech.com"}
    rec = approval.record_profile_approval("main-page", patch, approved_by="op")
    assert rec["content_hash"]
    verified = approval.verify_profile_approval("main-page", patch)
    assert verified["approved_by"] == "op"


def test_verify_fails_when_patch_was_edited(aws):
    patch_first = {"about": "first version"}
    approval.record_profile_approval("main-page", patch_first, approved_by="op")
    patch_second = {"about": "SECOND version"}
    with pytest.raises(approval.ApprovalError) as exc:
        approval.verify_profile_approval("main-page", patch_second)
    assert "changed after approval" in str(exc.value)


def test_verify_fails_without_an_approval(aws):
    with pytest.raises(approval.ApprovalError) as exc:
        approval.verify_profile_approval("main-page", {"about": "x"})
    assert "no approval record" in str(exc.value)


def test_hash_matches_hand_rolled_canonical_json():
    """Belt-and-braces: the hash is reproducible from the documented recipe."""
    import hashlib

    patch = {"about": "hi", "emails": ["b@x", "a@x"]}
    payload = {
        "account": "a",
        "patch": {"about": "hi", "emails": ["a@x", "b@x"]},
    }
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    assert approval.profile_hash("a", patch) == expected
