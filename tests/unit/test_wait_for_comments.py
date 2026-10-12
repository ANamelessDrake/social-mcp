"""wait_for_comments: subscribe-then-drain race, event unblock, timeout."""

import json

import boto3
import pytest
from sharedModules import registry, scope


@pytest.fixture
def two_accounts(monkeypatch):
    monkeypatch.setenv(
        "ACCOUNTS",
        json.dumps(
            [
                {
                    "alias": "main-page",
                    "provider": "facebook",
                    "publish_enabled": False,
                    "publish_mode": "manual",
                    "auto_for_kinds": [],
                    "policy": {},
                },
                {
                    "alias": "news-ig",
                    "provider": "instagram",
                    "publish_enabled": False,
                    "publish_mode": "manual",
                    "auto_for_kinds": [],
                    "policy": {},
                },
            ]
        ),
    )
    registry.all_accounts.cache_clear()
    yield
    registry.all_accounts.cache_clear()


def _write_event(table, alias: str, created_time: str, comment_id: str, post_id: str = "P"):
    table.put_item(
        Item={
            "PK": f"COMMENT_EVENT#{alias}",
            "SK": f"TS#{created_time}#{comment_id}",
            "account": alias,
            "provider": "facebook",
            "post_id": post_id,
            "comment_id": comment_id,
            "thread_id": comment_id,
            "created_time": created_time,
            "author": "A",
            "text": f"c{comment_id}",
            "is_hidden": False,
            "body_fetched": True,
            "ingested_at": created_time,
        }
    )


class _FakeSub:
    """Standin for EventSubscription.

    `drain_backlog_only`: raise if next_event is ever called (confirms we
    returned via the backlog drain).
    `events`: queue to return from next_event.
    """

    def __init__(self, drain_backlog_only=False, events=None):
        self.drain_backlog_only = drain_backlog_only
        self.events = list(events or [])
        self.connected = False
        self.closed = False

    def connect(self):
        self.connected = True

    def next_event(self, deadline):
        if self.drain_backlog_only:
            raise AssertionError("backlog should have satisfied the wait")
        return self.events.pop(0) if self.events else None

    def close(self):
        self.closed = True


def test_backlog_in_store_returns_immediately(aws, monkeypatch):
    table = boto3.resource("dynamodb").Table("test-socialmcp-core")
    _write_event(table, "main-page", "2026-10-01T00:00:00", "1")

    fake = _FakeSub(drain_backlog_only=True)
    monkeypatch.setattr(
        "tools.wait_for_comments.EventSubscription", lambda channel: fake
    )

    from tools.wait_for_comments import wait_for_comments

    out = wait_for_comments(timeout_seconds=5, account="main-page")
    assert out["ok"] is True
    assert [c["comment_id"] for c in out["comments"]] == ["1"]
    assert fake.closed is True


def test_event_from_stream_returns(aws, monkeypatch):
    event = {
        "account": "main-page",
        "comment_id": "9",
        "post_id": "P",
        "created_time": "2026-10-01T00:05:00",
        "text": "fresh",
    }
    fake = _FakeSub(events=[event])
    monkeypatch.setattr(
        "tools.wait_for_comments.EventSubscription", lambda channel: fake
    )

    from tools.wait_for_comments import wait_for_comments

    out = wait_for_comments(timeout_seconds=5, account="main-page")
    assert out["comments"][0]["comment_id"] == "9"


def test_timeout_returns_empty(aws, monkeypatch):
    fake = _FakeSub(events=[])
    monkeypatch.setattr(
        "tools.wait_for_comments.EventSubscription", lambda channel: fake
    )

    from tools.wait_for_comments import wait_for_comments

    out = wait_for_comments(timeout_seconds=5, account="main-page")
    assert out["timed_out"] is True
    assert out["comments"] == []


def test_wildcard_subscribes_without_account(aws, monkeypatch):
    captured = {}

    def factory(channel):
        captured["channel"] = channel
        return _FakeSub(events=[])

    monkeypatch.setattr("tools.wait_for_comments.EventSubscription", factory)
    from tools.wait_for_comments import wait_for_comments

    wait_for_comments(timeout_seconds=5)
    assert captured["channel"] == "social-mcp/comments/*"


def test_wildcard_filter_rejects_mismatched_event(aws, monkeypatch, two_accounts):
    on_wrong = {
        "account": "news-ig",
        "comment_id": "7",
        "post_id": "P",
        "created_time": "t",
    }
    on_right = {
        "account": "main-page",
        "comment_id": "8",
        "post_id": "P",
        "created_time": "u",
    }
    fake = _FakeSub(events=[on_wrong, on_right])
    monkeypatch.setattr(
        "tools.wait_for_comments.EventSubscription", lambda channel: fake
    )

    from tools.wait_for_comments import wait_for_comments

    out = wait_for_comments(timeout_seconds=5, account="main-page")
    assert out["comments"][0]["comment_id"] == "8"


def test_pinned_scope_narrows_the_subscribed_channel(aws, monkeypatch, two_accounts):
    """A connection pinned to one account must subscribe to that account's
    wildcard channel, not the top-level wildcard, so the server never
    delivers events the caller is not pinned to see."""
    captured = {}

    def factory(channel):
        captured["channel"] = channel
        return _FakeSub(events=[])

    monkeypatch.setattr("tools.wait_for_comments.EventSubscription", factory)
    scope.set_scope(("main-page",))

    from tools.wait_for_comments import wait_for_comments

    wait_for_comments(timeout_seconds=5)
    assert captured["channel"] == "social-mcp/comments/main-page/*"


def test_pinned_scope_filters_wildcard_events(aws, monkeypatch, two_accounts):
    """When the subscription channel is a wildcard (unpinned, or pinned to
    multiple accounts), events for accounts outside the pin must not reach
    the caller even if AppSync delivers them."""
    out_of_scope = {
        "account": "news-ig",
        "comment_id": "99",
        "post_id": "P",
        "created_time": "t",
    }
    fake = _FakeSub(events=[out_of_scope])
    monkeypatch.setattr(
        "tools.wait_for_comments.EventSubscription", lambda channel: fake
    )
    scope.set_scope(("main-page",))

    from tools.wait_for_comments import wait_for_comments

    # Even if the server delivered an out-of-scope event, the tool drops
    # it; the only outcome visible to the caller is the timeout.
    out = wait_for_comments(timeout_seconds=5)
    assert out["timed_out"] is True
    assert out["comments"] == []


def test_pinned_scope_rejects_named_out_of_scope_account(aws, two_accounts):
    scope.set_scope(("main-page",))
    from tools.wait_for_comments import wait_for_comments

    out = wait_for_comments(timeout_seconds=5, account="news-ig")
    assert out["ok"] is False
    assert "news-ig" in out["error"]
