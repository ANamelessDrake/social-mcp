"""Bluesky provider read-op mapping tests, against recorded XRPC fixtures.

The provider's HTTP client is swapped for a fake that returns fixture JSON
by (method, nsid, params), so these tests exercise the real mapping code
without any network.
"""

import json

import pytest
from conftest import load_bluesky_fixture
from sharedModules import dynamo, registry
from sharedModules.providers import bluesky


class _FakeClient:
    """Pretends to be BlueskyClient; returns fixture JSON for recorded nsids."""

    def __init__(self, mapping: dict[tuple[str, str], dict]) -> None:
        self._map = mapping
        self.calls: list[tuple[str, str, dict]] = []
        self.blob_calls: list[tuple[int, str]] = []

    def get(self, nsid: str, params: dict | None = None) -> dict:
        self.calls.append(("GET", nsid, dict(params or {})))
        for (method, suffix), payload in self._map.items():
            if method == "GET" and nsid == suffix:
                return payload
        raise AssertionError(f"unexpected GET {nsid} params={params}")

    def post(self, nsid: str, body: dict | None = None) -> dict:
        self.calls.append(("POST", nsid, dict(body or {})))
        for (method, suffix), payload in self._map.items():
            if method == "POST" and nsid == suffix:
                return payload
        raise AssertionError(f"unexpected POST {nsid} body={body}")

    def upload_blob(self, data: bytes, mime: str) -> dict:
        self.blob_calls.append((len(data), mime))
        for (method, suffix), payload in self._map.items():
            if method == "UPLOAD" and suffix == "blob":
                return payload
        raise AssertionError("unexpected upload_blob call")


def _entry(alias: str) -> dict:
    return {
        "alias": alias,
        "provider": "bluesky",
        "publish_enabled": False,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "policy": {},
    }


@pytest.fixture
def bsky_provider(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry("main-bsky")]))
    registry.all_accounts.cache_clear()
    from sharedModules import crypto

    access_ct = crypto.encrypt("access-jwt-placeholder")
    refresh_ct = crypto.encrypt("refresh-jwt-placeholder")
    dynamo.put_account(
        "main-bsky",
        {
            "provider": "bluesky",
            "account_id": "did:plc:examplehandle1234567890",
            "display_name": "wilma.bsky.social",
            "scopes": ["bluesky"],
            "page_token_ct": access_ct,
            "user_token_ct": refresh_ct,
            "status": "active",
        },
    )
    fake = _FakeClient(
        {
            ("GET", "app.bsky.feed.getAuthorFeed"): load_bluesky_fixture("author_feed.json"),
            ("GET", "app.bsky.feed.getPostThread"): load_bluesky_fixture("post_thread.json"),
        }
    )
    provider = bluesky.BlueskyProvider("main-bsky", _client=fake)
    return provider, fake


def test_list_posts_maps_shape_and_kinds(bsky_provider):
    provider, _ = bsky_provider
    posts = provider.list_posts(since_days=0, limit=10)
    assert len(posts) == 3
    kinds = [p.kind for p in posts]
    assert "text" in kinds
    assert "image" in kinds
    assert "multi_image" in kinds
    assert posts[0].post_id.startswith("at://did:plc:")
    # Permalink is the unified bsky.app URL built from handle + rkey
    assert posts[0].permalink == (
        "https://bsky.app/profile/wilma.bsky.social/post/3la000text1"
    )


def test_list_posts_passes_actor_and_limit(bsky_provider):
    provider, fake = bsky_provider
    provider.list_posts(since_days=0, limit=5)
    _, _, params = fake.calls[-1]
    assert params["actor"] == "did:plc:examplehandle1234567890"
    assert params["limit"] == 5


def test_list_posts_filters_by_since_days(bsky_provider, monkeypatch):
    # since_days > 0 drops any post whose createdAt is before cutoff; the
    # fixture has posts dated 2026-10-10, 10-09, 10-08. A cutoff of
    # 2099-01-01 drops them all.
    from datetime import UTC, datetime

    from sharedModules.providers import bluesky as bsky_mod

    cutoff = (
        datetime(2099, 1, 1, tzinfo=UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
    monkeypatch.setattr(bsky_mod, "_cutoff_iso", lambda _d: cutoff)
    provider, _ = bsky_provider
    posts = provider.list_posts(since_days=1, limit=10)
    assert posts == []


def test_get_post_includes_engagement_and_insights(bsky_provider):
    provider, _ = bsky_provider
    post = provider.get_post(
        "at://did:plc:examplehandle1234567890/app.bsky.feed.post/3la000text1"
    )
    assert post.engagement == {"likes": 7, "replies": 2, "reposts": 1, "quotes": 0}
    # engagements = likes + replies + reposts + quotes
    assert post.insights["engagements"] == 10
    assert post.insights["reactions"] == 7
    assert post.insights["comments"] == 2
    assert post.insights["shares"] == 1
    assert post.insights["impressions"] == 0  # unavailable via public XRPC
    assert post.insights["source_metrics"]["likeCount"] == 7


def test_list_comments_flattens_nested_replies(bsky_provider):
    provider, _ = bsky_provider
    comments = provider.list_comments(
        "at://did:plc:examplehandle1234567890/app.bsky.feed.post/3la000text1",
        limit=50,
    )
    # Two replies total: one direct reply, one reply-to-reply
    assert len(comments) == 2
    by_id = {c.comment_id: c for c in comments}
    # Top-level reply: thread_id points at the parent post_id
    top = by_id[
        "at://did:plc:examplereplier0000000001/app.bsky.feed.post/3la000reply1"
    ]
    assert top.thread_id == (
        "at://did:plc:examplehandle1234567890/app.bsky.feed.post/3la000text1"
    )
    # Nested reply: thread_id points at its immediate parent (the top-level reply)
    nested = by_id[
        "at://did:plc:examplehandle1234567890/app.bsky.feed.post/3la000reply2"
    ]
    assert nested.thread_id == top.comment_id


def test_fetch_comment_resolves_parent_uri(bsky_provider):
    provider, _ = bsky_provider
    data = provider.fetch_comment(
        "at://did:plc:examplehandle1234567890/app.bsky.feed.post/3la000text1"
    )
    assert data["comment_id"].startswith("at://did:plc:")
    assert data["author"] == "wilma.bsky.social"
    assert "deploy pipeline" in data["text"]
