"""Facebook publish mapping (M2) against recorded Graph fixtures."""

import json

import pytest
from conftest import load_fixture
from sharedModules import crypto, dynamo, registry
from sharedModules.errors import CapabilityError
from sharedModules.providers import facebook


class _RecordingGraph:
    """Captures POSTs and returns fixture JSON matched on path suffix."""

    def __init__(self, mapping: dict) -> None:
        self._map = mapping
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, dict]] = []

    def post(self, path: str, data: dict | None = None, params: dict | None = None) -> dict:
        self.posts.append((path, dict(data or {})))
        for suffix, payload in self._map.items():
            if path.endswith(suffix):
                return payload
        raise AssertionError(f"unexpected POST {path}")

    def get(self, path: str, params: dict | None = None) -> dict:
        self.gets.append((path, dict(params or {})))
        return {}


@pytest.fixture
def connected(monkeypatch, aws):
    entry = {
        "alias": "main-page",
        "provider": "facebook",
        "publish_enabled": True,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "policy": {},
    }
    monkeypatch.setenv("ACCOUNTS", json.dumps([entry]))
    registry.all_accounts.cache_clear()
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
                "pages_manage_posts",
                "publish_video",
                "public_profile",
                "business_management",
            ],
            "page_token_ct": token_ct,
            "status": "active",
        },
    )


def _draft(**fields):
    from types import SimpleNamespace

    base = {
        "account": "main-page",
        "kind": "text",
        "text": "hello world",
        "link": "",
        "media": [],
        "schedule": "",
    }
    base.update(fields)
    return SimpleNamespace(**base)


def test_text_post_hits_feed_endpoint(connected):
    graph = _RecordingGraph({"/100000000000001/feed": load_fixture("post_create.json")})
    provider = facebook.FacebookProvider("main-page", _client=graph)
    result = provider.publish(_draft(text="hi"))
    assert graph.posts[0][0] == "/100000000000001/feed"
    assert graph.posts[0][1]["message"] == "hi"
    assert result.post_id == "100000000000001_900000000000099"


def test_link_post_sends_link_field(connected):
    graph = _RecordingGraph({"/100000000000001/feed": load_fixture("post_create.json")})
    provider = facebook.FacebookProvider("main-page", _client=graph)
    provider.publish(
        _draft(kind="link", text="see this", link="https://example.com/x")
    )
    sent = graph.posts[0][1]
    assert sent["link"] == "https://example.com/x"
    assert sent["message"] == "see this"


def _media(url: str, mime: str, etag: str) -> dict:
    return {"source": "url", "key": url, "mime": mime, "etag": etag}


def test_single_photo_returns_post_id_from_photo_create(connected):
    graph = _RecordingGraph(
        {"/100000000000001/photos": load_fixture("photo_create.json")}
    )
    provider = facebook.FacebookProvider("main-page", _client=graph)
    draft = _draft(
        kind="image",
        text="look",
        media=[_media("https://cdn.example.com/a.jpg", "image/jpeg", "a")],
    )
    result = provider.publish(draft)
    assert result.post_id == "100000000000001_900000000000100"


def test_multi_photo_creates_unpublished_children_then_feed(connected):
    graph = _RecordingGraph(
        {
            "/100000000000001/photos": {"id": "500000001"},
            "/100000000000001/feed": load_fixture("multi_photo_feed.json"),
        }
    )
    # Fixture matching is on suffix, so both child uploads come back as the
    # same id; the point of the test is sequence + payload.
    provider = facebook.FacebookProvider("main-page", _client=graph)
    draft = _draft(
        kind="multi_image",
        text="carousel",
        media=[
            _media("https://cdn.example.com/one.jpg", "image/jpeg", "1"),
            _media("https://cdn.example.com/two.jpg", "image/jpeg", "2"),
        ],
    )
    result = provider.publish(draft)
    paths = [p for p, _ in graph.posts]
    assert paths.count("/100000000000001/photos") == 2
    assert paths[-1] == "/100000000000001/feed"
    attached = json.loads(graph.posts[-1][1]["attached_media"])
    assert [a["media_fbid"] for a in attached] == ["500000001", "500000001"]
    assert result.post_id == "100000000000001_900000000000101"


def test_video_sends_file_url_to_videos_endpoint(connected):
    graph = _RecordingGraph({"/100000000000001/videos": {"id": "700000001"}})
    provider = facebook.FacebookProvider("main-page", _client=graph)
    draft = _draft(
        kind="video",
        text="watch",
        media=[_media("https://cdn.example.com/clip.mp4", "video/mp4", "v")],
    )
    result = provider.publish(draft)
    assert graph.posts[0][0] == "/100000000000001/videos"
    assert graph.posts[0][1]["file_url"] == "https://cdn.example.com/clip.mp4"
    assert result.post_id == "700000001"


def test_reel_raises_not_implemented(connected):
    graph = _RecordingGraph({})
    provider = facebook.FacebookProvider("main-page", _client=graph)
    draft = _draft(
        kind="reel",
        text="",
        media=[_media("https://cdn.example.com/r.mp4", "video/mp4", "r")],
    )
    with pytest.raises(NotImplementedError):
        provider.publish(draft)


def test_s3_media_is_refused_in_m2(connected):
    graph = _RecordingGraph({})
    provider = facebook.FacebookProvider("main-page", _client=graph)
    draft = _draft(
        kind="image",
        media=[{"source": "s3", "key": "s3://bucket/key.jpg", "mime": "image/jpeg", "etag": "s"}],
    )
    with pytest.raises(CapabilityError) as exc:
        provider.publish(draft)
    assert "https" in str(exc.value)
