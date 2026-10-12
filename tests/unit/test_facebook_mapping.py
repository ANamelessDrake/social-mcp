"""Facebook provider mapping tests, against recorded Graph fixtures.

The provider's HTTP client is swapped for a fake that returns fixture JSON
by (method, path, params), so these tests exercise the real mapping code
without any network.
"""

import json

import pytest
from conftest import load_fixture
from sharedModules import dynamo, registry
from sharedModules.providers import facebook
from sharedModules.providers.metaClient import GraphClient


class _FakeGraph:
    """Pretends to be GraphClient; returns fixture JSON for recorded paths."""

    def __init__(self, mapping: dict[tuple[str, str], dict]) -> None:
        self._map = mapping
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        self.calls.append(("GET", path, dict(params or {})))
        # Match on prefix so /{id}/posts and /{id}/insights can share a provider instance
        for (method, suffix), payload in self._map.items():
            if method == "GET" and path.endswith(suffix):
                return payload
        raise AssertionError(f"unexpected GET {path} params={params}")


def _entry(alias: str) -> dict:
    return {
        "alias": alias,
        "provider": "facebook",
        "publish_enabled": False,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "policy": {},
    }


@pytest.fixture
def facebook_provider(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry("main-page")]))
    registry.all_accounts.cache_clear()
    # Encrypt a bogus token so the provider has something to decrypt
    from sharedModules import crypto

    token_ct = crypto.encrypt("page-token-placeholder")
    dynamo.put_account(
        "main-page",
        {
            "provider": "facebook",
            "account_id": "100000000000001",
            "display_name": "Example Page",
            "scopes": [
                "pages_show_list",
                "pages_read_engagement",
                "pages_read_user_content",
                "read_insights",
                "business_management",
                "public_profile",
            ],
            "page_token_ct": token_ct,
            "status": "active",
        },
    )

    fake = _FakeGraph(
        {
            ("GET", "/100000000000001/posts"): load_fixture("page_feed.json"),
            ("GET", "/100000000000001_900000000000001"): load_fixture("single_post.json"),
            (
                "GET",
                "/100000000000001_900000000000001/comments",
            ): load_fixture("post_comments.json"),
            (
                "GET",
                "/100000000000001_900000000000001/insights",
            ): load_fixture("post_insights.json"),
        }
    )
    provider = facebook.FacebookProvider("main-page", _client=fake)
    return provider, fake


def test_list_posts_maps_shape_and_kinds(facebook_provider):
    provider, _ = facebook_provider
    posts = provider.list_posts(since_days=0, limit=10)
    assert [p.post_id for p in posts] == [
        "100000000000001_900000000000001",
        "100000000000001_900000000000002",
        "100000000000001_900000000000003",
    ]
    kinds = [p.kind for p in posts]
    assert "image" in kinds
    assert "link" in kinds
    assert "text" in kinds
    assert posts[0].media_count == 1


def test_list_posts_passes_since_only_when_requested(facebook_provider):
    provider, fake = facebook_provider
    provider.list_posts(since_days=7, limit=5)
    _, _, params = fake.calls[-1]
    assert params["since"] == "-7 days"
    assert params["limit"] == 5

    fake.calls.clear()
    provider.list_posts(since_days=0, limit=5)
    _, _, params = fake.calls[-1]
    assert "since" not in params


def test_get_post_includes_full_text_media_and_insights(facebook_provider):
    provider, _ = facebook_provider
    post = provider.get_post("100000000000001_900000000000001")
    assert post.full_text.startswith("Shipping the new deploy pipeline")
    assert len(post.media) == 2  # subattachments expand
    # Unified insights roll up the four impression metrics and the reactions
    assert post.insights["impressions"] == 1240 + 0 + 310 + 930
    assert post.insights["reactions"] == 42 + 7 + 1 + 0 + 0 + 0


def test_list_comments_assigns_thread_ids(facebook_provider):
    provider, _ = facebook_provider
    comments = provider.list_comments("100000000000001_900000000000001", limit=25)
    by_id = {c.comment_id: c for c in comments}
    # Top-level comment: thread_id = own id
    assert by_id["900000000000001_1"].thread_id == "900000000000001_1"
    # Reply: thread_id points at the parent
    assert by_id["900000000000001_1_1"].thread_id == "900000000000001_1"


def test_get_insights_records_source_metrics(facebook_provider):
    provider, _ = facebook_provider
    ins = provider.get_insights("100000000000001_900000000000001")
    assert ins.source_metrics["post_impressions_organic"] == 1240
    assert "post_impressions_unique" not in ins.source_metrics  # we never ask for it


def test_graph_client_version_is_pinned():
    from sharedModules.providers.metaClient import GRAPH_API_VERSION

    assert GRAPH_API_VERSION == "v26.0"


def test_graph_client_can_be_constructed_with_a_token_fn():
    # Sanity check that the public constructor contract still works
    client = GraphClient(access_token_fn=lambda: "t")
    client.close()
