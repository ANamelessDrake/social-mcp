"""Instagram publish mapping tests (M5).

The provider's HTTP client and `time.sleep` are swapped for test doubles so
the container-flow poll loop runs instantly. Covers:

- image / video / reel single-container flows
- carousel: child containers, parent, 10-item cap, child sequencing
- is_ai_generated flag inclusion
- status poll transitions (IN_PROGRESS -> FINISHED)
- ERROR and EXPIRED paths raise
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from conftest import load_fixture
from sharedModules import dynamo, registry
from sharedModules.errors import AccountError, CapabilityError
from sharedModules.providers import instagram


class _FakeGraphPublish:
    """Script-driven fake: enqueue responses per (method, suffix).

    `get` and `post` pop from a per-key queue so a sequence of poll
    responses can walk a container from IN_PROGRESS to FINISHED.
    """

    def __init__(self, scripted: dict[tuple[str, str], list[dict]]) -> None:
        self._script = {k: list(v) for k, v in scripted.items()}
        self.calls: list[tuple[str, str, dict, dict]] = []

    def _lookup(self, method: str, path: str) -> dict:
        for (m, suffix), queue in self._script.items():
            if m == method and path.endswith(suffix):
                if not queue:
                    raise AssertionError(f"script exhausted for {method} {path}")
                return queue.pop(0)
        raise AssertionError(f"unexpected {method} {path}")

    def get(self, path: str, params: dict | None = None) -> dict:
        self.calls.append(("GET", path, dict(params or {}), {}))
        return self._lookup("GET", path)

    def post(self, path: str, data: dict | None = None, params: dict | None = None) -> dict:
        self.calls.append(("POST", path, dict(params or {}), dict(data or {})))
        return self._lookup("POST", path)

    def delete(self, path: str, params: dict | None = None) -> dict:
        self.calls.append(("DELETE", path, dict(params or {}), {}))
        return self._lookup("DELETE", path)


def _entry(alias: str) -> dict:
    return {
        "alias": alias,
        "provider": "instagram",
        "publish_enabled": True,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "policy": {},
    }


def _connect(scopes: list[str]) -> None:
    from sharedModules import crypto

    dynamo.put_account(
        "main-ig",
        {
            "provider": "instagram",
            "account_id": "17841000000000001",
            "display_name": "Example (Instagram)",
            "scopes": scopes,
            "page_token_ct": crypto.encrypt("page-token-placeholder"),
            "status": "active",
            "linked_page": "main-page",
        },
    )


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    """Collapse the exponential poll backoff so tests run in milliseconds."""
    monkeypatch.setattr(instagram.time, "sleep", lambda *_: None)


@pytest.fixture
def ig_publishing(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry("main-ig")]))
    registry.all_accounts.cache_clear()
    _connect(
        [
            "pages_show_list",
            "pages_read_engagement",
            "instagram_basic",
            "instagram_content_publish",
        ],
    )
    yield


def _draft(
    kind: str,
    media: list[str],
    text: str = "caption",
    is_ai: bool = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        account="main-ig",
        kind=kind,
        text=text,
        link="",
        schedule="",
        media=[{"source": "url", "key": u, "mime": "image/jpeg", "etag": ""} for u in media],
        is_ai_generated=is_ai,
    )


def test_image_publish_posts_image_url_and_publishes(ig_publishing):
    container = load_fixture("ig_media_container.json")
    published = load_fixture("ig_media_publish.json")
    fake = _FakeGraphPublish(
        {
            ("POST", "/17841000000000001/media"): [{"id": "17895000000000500"}],
            ("GET", "/17895000000000500"): [container["status_FINISHED"]],
            ("POST", "/17841000000000001/media_publish"): [published],
            ("GET", "/17895000000000900"): [
                {"permalink": published["permalink"]}
            ],
        }
    )
    provider = instagram.InstagramProvider("main-ig", _client=fake)
    result = provider.publish(_draft("image", ["https://cdn.example.com/a.jpg"]))
    assert result.post_id == "17895000000000900"
    assert result.permalink.endswith("/p/CfakePublishedMedia/")
    create_call = next(c for c in fake.calls if c[0] == "POST" and c[1].endswith("/media"))
    assert create_call[3]["image_url"] == "https://cdn.example.com/a.jpg"
    assert create_call[3]["caption"] == "caption"


def test_video_publish_sends_media_type_video(ig_publishing):
    container = load_fixture("ig_media_container.json")
    fake = _FakeGraphPublish(
        {
            ("POST", "/17841000000000001/media"): [{"id": "17895000000000500"}],
            ("GET", "/17895000000000500"): [container["status_FINISHED"]],
            ("POST", "/17841000000000001/media_publish"): [{"id": "17895000000000900"}],
            ("GET", "/17895000000000900"): [{"permalink": ""}],
        }
    )
    provider = instagram.InstagramProvider("main-ig", _client=fake)
    provider.publish(_draft("video", ["https://cdn.example.com/v.mp4"]))
    create_call = next(c for c in fake.calls if c[0] == "POST" and c[1].endswith("/media"))
    assert create_call[3]["media_type"] == "VIDEO"
    assert create_call[3]["video_url"] == "https://cdn.example.com/v.mp4"


def test_reel_publish_sends_media_type_reels(ig_publishing):
    container = load_fixture("ig_media_container.json")
    fake = _FakeGraphPublish(
        {
            ("POST", "/17841000000000001/media"): [{"id": "17895000000000500"}],
            ("GET", "/17895000000000500"): [container["status_FINISHED"]],
            ("POST", "/17841000000000001/media_publish"): [{"id": "17895000000000900"}],
            ("GET", "/17895000000000900"): [{"permalink": ""}],
        }
    )
    provider = instagram.InstagramProvider("main-ig", _client=fake)
    provider.publish(_draft("reel", ["https://cdn.example.com/r.mp4"]))
    create_call = next(c for c in fake.calls if c[0] == "POST" and c[1].endswith("/media"))
    assert create_call[3]["media_type"] == "REELS"


def test_is_ai_generated_flag_lands_on_the_container_only(ig_publishing):
    container = load_fixture("ig_media_container.json")
    fake = _FakeGraphPublish(
        {
            ("POST", "/17841000000000001/media"): [{"id": "17895000000000500"}],
            ("GET", "/17895000000000500"): [container["status_FINISHED"]],
            ("POST", "/17841000000000001/media_publish"): [{"id": "17895000000000900"}],
            ("GET", "/17895000000000900"): [{"permalink": ""}],
        }
    )
    provider = instagram.InstagramProvider("main-ig", _client=fake)
    provider.publish(_draft("image", ["https://cdn.example.com/a.jpg"], is_ai=True))
    create_call = next(c for c in fake.calls if c[0] == "POST" and c[1].endswith("/media"))
    assert create_call[3]["is_ai_generated"] == "true"
    publish_call = next(c for c in fake.calls if c[0] == "POST" and c[1].endswith("/media_publish"))
    assert "is_ai_generated" not in publish_call[3]


def test_carousel_builds_children_then_parent(ig_publishing):
    container = load_fixture("ig_media_container.json")
    fake = _FakeGraphPublish(
        {
            ("POST", "/17841000000000001/media"): [
                {"id": "child-1"},
                {"id": "child-2"},
                {"id": "parent-1"},
            ],
            ("GET", "/child-1"): [container["status_FINISHED"]],
            ("GET", "/child-2"): [container["status_FINISHED"]],
            ("GET", "/parent-1"): [container["status_FINISHED"]],
            ("POST", "/17841000000000001/media_publish"): [{"id": "17895000000000900"}],
            ("GET", "/17895000000000900"): [{"permalink": ""}],
        }
    )
    provider = instagram.InstagramProvider("main-ig", _client=fake)
    provider.publish(
        _draft("multi_image", ["https://cdn.example.com/a.jpg", "https://cdn.example.com/b.jpg"])
    )
    post_calls = [c for c in fake.calls if c[0] == "POST" and c[1].endswith("/media")]
    # Two child creates + one parent create
    assert len(post_calls) == 3
    assert post_calls[0][3]["is_carousel_item"] == "true"
    assert post_calls[1][3]["is_carousel_item"] == "true"
    parent = post_calls[2][3]
    assert parent["media_type"] == "CAROUSEL"
    assert parent["children"] == "child-1,child-2"
    assert parent["caption"] == "caption"


def test_carousel_caps_at_ten_items(ig_publishing):
    provider = instagram.InstagramProvider(
        "main-ig",
        _client=_FakeGraphPublish({}),
    )
    too_many = [f"https://cdn.example.com/{i}.jpg" for i in range(11)]
    with pytest.raises(AccountError, match="caps at 10"):
        provider.publish(_draft("multi_image", too_many))


def test_poll_walks_in_progress_to_finished(ig_publishing):
    container = load_fixture("ig_media_container.json")
    fake = _FakeGraphPublish(
        {
            ("POST", "/17841000000000001/media"): [{"id": "17895000000000500"}],
            ("GET", "/17895000000000500"): [
                container["status_IN_PROGRESS"],
                container["status_IN_PROGRESS"],
                container["status_FINISHED"],
            ],
            ("POST", "/17841000000000001/media_publish"): [{"id": "17895000000000900"}],
            ("GET", "/17895000000000900"): [{"permalink": ""}],
        }
    )
    provider = instagram.InstagramProvider("main-ig", _client=fake)
    provider.publish(_draft("image", ["https://cdn.example.com/a.jpg"]))
    poll_calls = [c for c in fake.calls if c[0] == "GET" and c[1].endswith("/17895000000000500")]
    assert len(poll_calls) == 3


def test_container_error_raises(ig_publishing):
    container = load_fixture("ig_media_container.json")
    fake = _FakeGraphPublish(
        {
            ("POST", "/17841000000000001/media"): [{"id": "17895000000000500"}],
            ("GET", "/17895000000000500"): [container["status_ERROR"]],
        }
    )
    provider = instagram.InstagramProvider("main-ig", _client=fake)
    with pytest.raises(AccountError, match="failed"):
        provider.publish(_draft("image", ["https://cdn.example.com/a.jpg"]))


def test_container_expired_raises(ig_publishing):
    container = load_fixture("ig_media_container.json")
    fake = _FakeGraphPublish(
        {
            ("POST", "/17841000000000001/media"): [{"id": "17895000000000500"}],
            ("GET", "/17895000000000500"): [container["status_EXPIRED"]],
        }
    )
    provider = instagram.InstagramProvider("main-ig", _client=fake)
    with pytest.raises(AccountError, match="failed"):
        provider.publish(_draft("image", ["https://cdn.example.com/a.jpg"]))


def test_text_only_draft_is_capability_error(ig_publishing):
    provider = instagram.InstagramProvider("main-ig", _client=_FakeGraphPublish({}))
    with pytest.raises(CapabilityError, match="text-only"):
        provider.publish(_draft("text", [], text="hello"))


def test_link_draft_is_capability_error(ig_publishing):
    provider = instagram.InstagramProvider("main-ig", _client=_FakeGraphPublish({}))
    draft = _draft("link", [])
    draft.link = "https://justbardtech.com"
    with pytest.raises(CapabilityError, match="link"):
        provider.publish(draft)


def test_publish_without_scope_errors(aws, monkeypatch):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry("main-ig")]))
    registry.all_accounts.cache_clear()
    _connect(["pages_show_list", "instagram_basic"])  # no instagram_content_publish
    provider = instagram.InstagramProvider("main-ig", _client=_FakeGraphPublish({}))
    with pytest.raises(AccountError, match="instagram_content_publish"):
        provider.publish(_draft("image", ["https://cdn.example.com/a.jpg"]))
