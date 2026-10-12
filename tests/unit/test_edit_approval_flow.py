"""End-to-end edit flow: stage -> approve -> Publisher -> audit (M3).

The provider is swapped for a fake so these tests exercise the stage +
approval + Publisher dispatch logic without the Graph wire format
(that is `test_facebook_edit_delete.py`).
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
        "policy": {},
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry()]))
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    registry.all_accounts.cache_clear()


class _FakeProvider:
    def __init__(self):
        self.edits: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        self.replies: list[tuple[str, str]] = []
        self.hides: list[str] = []

    def edit_post(self, post_id: str, text: str) -> None:
        self.edits.append((post_id, text))

    def delete_post(self, post_id: str) -> None:
        self.deletes.append(post_id)

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


POST = "100000000000001_900000000000099"


def test_edit_happy_path_runs_and_audits(handler):
    pub_app, fake = handler
    dynamo.write_staged_edit(POST, "main-page", "revised", "agent")
    approval.record_edit_approval(POST, "revised", approved_by="op")

    out = pub_app.handler({"kind": "edit", "post_id": POST}, None)
    assert out["ok"] is True
    assert fake.edits == [(POST, "revised")]
    audit = dynamo.list_audit(account="main-page", limit=10)
    assert any(row["kind"] == "edit" and row["result"] == "ok" for row in audit)
    # staged + approval both cleaned up
    assert dynamo.read_staged_edit(POST) is None
    assert dynamo.read_approval_record(f"edit:{POST}") is None


def test_edit_voids_approval_when_text_changes(handler):
    """DESIGN.md 7.3: edit after approve must not slip through."""
    pub_app, fake = handler
    dynamo.write_staged_edit(POST, "main-page", "first", "agent")
    approval.record_edit_approval(POST, "first", approved_by="op")
    # Agent re-stages the edit with new text after the operator approved:
    dynamo.write_staged_edit(POST, "main-page", "SECOND, different", "agent")

    out = pub_app.handler({"kind": "edit", "post_id": POST}, None)
    assert out["ok"] is False
    assert "changed after approval" in out["error"]
    assert fake.edits == []
    # The approval is intentionally left to let the operator re-approve,
    # but the staged change is preserved so the operator can see what
    # changed; the audit row records the mismatch.
    audit = dynamo.list_audit(account="main-page", limit=10)
    assert any(
        row["kind"] == "edit" and row["result"] == "error:approval_mismatch" for row in audit
    )


def test_edit_without_approval_refuses(handler):
    pub_app, fake = handler
    dynamo.write_staged_edit(POST, "main-page", "x", "agent")
    out = pub_app.handler({"kind": "edit", "post_id": POST}, None)
    assert out["ok"] is False
    assert "has no approval record" in out["error"]
    assert fake.edits == []
