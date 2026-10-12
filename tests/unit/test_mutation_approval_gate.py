"""Mutation check: the approval gate is load-bearing (kickoff requirement).

The kickoff's test asks a direct question: if the gate is removed, do the
tests that depend on it fail? If yes, the gate is load-bearing. If no, the
gate is decorative.

We demonstrate load-bearingness by taking the one test that proves the
gate's job (the Publisher refusing to publish an edit-after-approval
draft) and running it with the gate stubbed out. The Publisher should
then publish the stale content, confirming the real gate is what was
stopping it.

A test that asserted "we removed the gate and the system behaves
identically" would prove the gate was never doing its job.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

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


class _FakeProvider:
    def __init__(self) -> None:
        self.calls: list = []

    def publish(self, draft):
        self.calls.append(draft)
        return PublishResult(
            account="main-page",
            post_id="100_999",
            permalink="https://www.facebook.com/100_999",
            kind="text",
        )


@pytest.fixture
def _publisher():
    root = Path(__file__).resolve().parents[2]
    pub_dir = root / "infrastructure" / "lambdaFunctions" / "publisher"
    if str(pub_dir) not in sys.path:
        sys.path.insert(0, str(pub_dir))
    import app as pub_app  # type: ignore

    importlib.reload(pub_app)
    return pub_app


def test_edit_after_approval_IS_stopped_by_the_gate(_publisher, monkeypatch, aws):
    """Baseline: with the real gate, the Publisher refuses the stale draft."""
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry()]))
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    registry.all_accounts.cache_clear()

    fake = _FakeProvider()
    monkeypatch.setattr(_publisher, "get_provider", lambda _: fake, raising=False)

    dynamo.write_draft(
        {
            "draft_id": "mt1",
            "account": "main-page",
            "kind": "text",
            "text": "approved text",
            "link": "",
            "media": [],
            "schedule": "",
            "status": "approved",
            "created_by": "test",
        }
    )
    approval.record_approval(draft_id="mt1", scope="publish", approved_by="op")
    dynamo.update_draft_fields("mt1", {"text": "SNEAKY edit after approval"})

    out = _publisher.handler({"draft_id": "mt1"}, None)
    assert out["ok"] is False, "the real approval gate must refuse this"
    assert fake.calls == [], "Meta must never be called when the gate refuses"


def test_removing_the_gate_lets_the_stale_draft_through(_publisher, monkeypatch, aws):
    """Mutation: patch verify_approval to a no-op and confirm the stale draft publishes.

    If this test passed with the gate intact, the gate was never doing its job.
    """
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry()]))
    monkeypatch.setenv("PUBLISHING_ENABLED", "true")
    registry.all_accounts.cache_clear()

    fake = _FakeProvider()
    monkeypatch.setattr(_publisher, "get_provider", lambda _: fake, raising=False)

    # Mutation: the gate is now a no-op. The approval module the Publisher
    # imported is still `approval_mod`; patch its verify_approval.
    monkeypatch.setattr(
        _publisher.approval_mod,
        "verify_approval",
        lambda draft_id, scope: {"ok": True},
        raising=False,
    )

    dynamo.write_draft(
        {
            "draft_id": "mt2",
            "account": "main-page",
            "kind": "text",
            "text": "approved text",
            "link": "",
            "media": [],
            "schedule": "",
            "status": "approved",
            "created_by": "test",
        }
    )
    approval.record_approval(draft_id="mt2", scope="publish", approved_by="op")
    dynamo.update_draft_fields("mt2", {"text": "SNEAKY edit after approval"})

    out = _publisher.handler({"draft_id": "mt2"}, None)
    # With the gate removed, the Publisher now reaches Meta - which is
    # the exact failure mode the gate exists to prevent. Finding this
    # case here is the whole point: the gate is load-bearing.
    assert out["ok"] is True
    assert fake.calls, "with the gate removed, Meta was called with the stale draft"
    assert fake.calls[0].text == "SNEAKY edit after approval"
