"""Instagram provider mapping tests, against recorded Graph fixtures.

The provider's HTTP client is swapped for a fake that returns fixture JSON
by (method, path), so these tests exercise the real mapping code without
any network. Mirrors `test_facebook_mapping.py`.
"""

import json

import pytest
from conftest import load_fixture
from sharedModules import dynamo, registry
from sharedModules.errors import AccountError
from sharedModules.providers import instagram
from sharedModules.providers.instagram import BANNED_METRICS


class _FakeGraph:
    """Pretends to be GraphClient; returns fixture JSON for recorded paths."""

    def __init__(self, mapping: dict[tuple[str, str], dict]) -> None:
        self._map = mapping
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        self.calls.append(("GET", path, dict(params or {})))
        # Match on exact-suffix so /{id} and /{id}/comments stay distinct.
        for (method, suffix), payload in self._map.items():
            if method == "GET" and path.endswith(suffix):
                return payload
        raise AssertionError(f"unexpected GET {path} params={params}")


def _entry(alias: str) -> dict:
    return {
        "alias": alias,
        "provider": "instagram",
        "publish_enabled": False,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "policy": {},
    }


def _connect(scopes: list[str], aws) -> None:
    """Write a connected IG ACCOUNT item with the given scopes."""
    from sharedModules import crypto

    token_ct = crypto.encrypt("page-token-placeholder")
    dynamo.put_account(
        "main-ig",
        {
            "provider": "instagram",
            "account_id": "17841000000000001",
            "display_name": "Example (Instagram)",
            "scopes": scopes,
            "page_token_ct": token_ct,
            "status": "active",
            "linked_page": "main-page",
        },
    )


@pytest.fixture
def ig_provider(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry("main-ig")]))
    registry.all_accounts.cache_clear()
    _connect(
        [
            "pages_show_list",
            "pages_read_engagement",
            "instagram_basic",
            "instagram_manage_insights",
        ],
        aws,
    )

    fake = _FakeGraph(
        {
            ("GET", "/17841000000000001/media"): load_fixture("ig_media_list.json"),
            ("GET", "/17895000000000001"): load_fixture("ig_media_single.json"),
            ("GET", "/17895000000000003"): load_fixture("ig_media_single_reel.json"),
            ("GET", "/17895000000000002"): load_fixture("ig_media_carousel.json"),
            ("GET", "/17895000000000001/comments"): load_fixture("ig_comments.json"),
            ("GET", "/17895000000000001/insights"): load_fixture("ig_insights_image.json"),
            # Carousel insights use the IMAGE-tier metric set, so the image
            # fixture is a valid shape for a carousel insights response too.
            ("GET", "/17895000000000002/insights"): load_fixture("ig_insights_image.json"),
            ("GET", "/17895000000000003/insights"): load_fixture("ig_insights_reel.json"),
        }
    )
    provider = instagram.InstagramProvider("main-ig", _client=fake)
    return provider, fake


def test_list_posts_maps_media_type_to_unified_kind(ig_provider):
    provider, _ = ig_provider
    posts = provider.list_posts(since_days=0, limit=10)
    by_id = {p.post_id: p for p in posts}
    assert by_id["17895000000000001"].kind == "image"
    assert by_id["17895000000000002"].kind == "multi_image"
    assert by_id["17895000000000003"].kind == "reel"
    assert by_id["17895000000000004"].kind == "video"
    # Carousel's media_count comes from the children edge
    assert by_id["17895000000000002"].media_count == 3
    # Single image / reel has one media
    assert by_id["17895000000000001"].media_count == 1


def test_list_posts_passes_since_only_when_requested(ig_provider):
    provider, fake = ig_provider
    provider.list_posts(since_days=14, limit=5)
    _, _, params = fake.calls[-1]
    assert params["since"] == "-14 days"
    assert params["limit"] == 5
    fake.calls.clear()
    provider.list_posts(since_days=0, limit=5)
    _, _, params = fake.calls[-1]
    assert "since" not in params


def test_get_post_image_includes_full_caption_and_engagement(ig_provider):
    provider, _ = ig_provider
    post = provider.get_post("17895000000000001")
    assert post.full_text.startswith("Launch day!")
    # Direct counters land in engagement
    assert post.engagement["likes"] == 128
    assert post.engagement["comments"] == 12
    # Insights unification picks up the IMAGE-tier metrics
    assert post.insights["reach"] == 4820
    assert post.insights["reactions"] == 128  # likes map to reactions
    assert post.insights["engagements"] == 173  # total_interactions
    # Media-level impressions are deprecated for IG; the unified field stays 0
    assert post.insights["impressions"] == 0
    # Images do not populate views
    assert post.insights["views"] == 0


def test_get_post_carousel_expands_children_media(ig_provider):
    provider, _ = ig_provider
    post = provider.get_post("17895000000000002")
    assert len(post.media) == 3
    assert post.media[2]["type"] == "video"
    assert post.media[0]["url"] == "https://example.com/ig/bts_1.jpg"


def test_list_comments_flattens_nested_replies_with_thread_ids(ig_provider):
    provider, _ = ig_provider
    comments = provider.list_comments("17895000000000001", limit=25)
    by_id = {c.comment_id: c for c in comments}
    # Top-level: thread_id = own id
    assert by_id["17900000000000001"].thread_id == "17900000000000001"
    assert by_id["17900000000000002"].thread_id == "17900000000000002"
    # Replies: thread_id points at the top-level parent, not the reply chain
    assert by_id["17900000000000011"].thread_id == "17900000000000001"
    assert by_id["17900000000000012"].thread_id == "17900000000000001"
    # Author uses `username` not `from.name`
    assert by_id["17900000000000001"].author == "example_user_a"


def test_insights_reel_uses_video_metric_set(ig_provider):
    provider, fake = ig_provider
    ins = provider.get_insights("17895000000000003", _media_type_hint="REELS")
    assert ins.reach == 15200
    assert ins.views == 22800
    assert ins.reactions == 540  # likes
    assert ins.engagements == 751  # total_interactions
    # And the Graph request did include the reels-specific metric
    _, _, params = fake.calls[-1]
    assert "ig_reels_video_view_total_time" in params["metric"]


def test_insights_requests_never_contain_deprecated_metrics(ig_provider):
    provider, fake = ig_provider
    provider.get_insights("17895000000000001", _media_type_hint="IMAGE")
    provider.get_insights("17895000000000003", _media_type_hint="REELS")
    for _, _, params in fake.calls:
        metric_arg = params.get("metric", "")
        for banned in BANNED_METRICS:
            assert banned not in metric_arg.split(","), (
                f"deprecated metric {banned!r} must not be requested"
            )


def test_insights_without_scope_fails_with_a_specific_message(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry("main-ig")]))
    registry.all_accounts.cache_clear()
    # Connected but without instagram_manage_insights
    _connect(["pages_show_list", "pages_read_engagement", "instagram_basic"], aws)
    provider = instagram.InstagramProvider("main-ig", _client=_FakeGraph({}))
    with pytest.raises(AccountError) as exc:
        provider.get_insights("17895000000000001", _media_type_hint="IMAGE")
    msg = str(exc.value)
    assert "instagram_manage_insights" in msg
    assert "admin.py connect" in msg


def test_get_insights_without_hint_fetches_media_type_first(ig_provider):
    provider, fake = ig_provider
    ins = provider.get_insights("17895000000000001")
    # First GET is the media_type lookup, second is the insights call
    paths = [c[1] for c in fake.calls]
    assert paths[-2].endswith("/17895000000000001")
    assert paths[-1].endswith("/17895000000000001/insights")
    assert ins.reach == 4820
