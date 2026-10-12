"""Facebook profile provider mapping (M9) against a recorded Graph fixture."""

import json

import pytest
from sharedModules import crypto, dynamo, registry
from sharedModules.models import ProfilePatch
from sharedModules.providers import facebook


class _RecordingGraph:
    """Tiny GraphClient fake: records POSTs, returns fixture JSON for GETs."""

    def __init__(self, mapping: dict | None = None) -> None:
        self._map = mapping or {}
        self.gets: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []

    def get(self, path: str, params: dict | None = None) -> dict:
        self.gets.append((path, dict(params or {})))
        for suffix, payload in self._map.items():
            if path.endswith(suffix):
                return payload
        return {}

    def post(self, path: str, data: dict | None = None, params: dict | None = None) -> dict:
        self.posts.append((path, dict(data or {})))
        return {"success": True}


@pytest.fixture
def connected(monkeypatch, aws):
    entry = {
        "alias": "main-page",
        "provider": "facebook",
        "publish_enabled": True,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "profile_edits_enabled": True,
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
                "pages_manage_metadata",
            ],
            "page_token_ct": token_ct,
            "status": "active",
        },
    )


def test_get_profile_maps_text_fields_and_picture(connected):
    graph = _RecordingGraph(
        {
            "/100000000000001": {
                "id": "100000000000001",
                "name": "Example Page",
                "username": "example",
                "about": "A short about line",
                "description": "A longer description",
                "website": "https://justbardtech.com",
                "phone": "+1 212 555 0100",
                "emails": ["hi@justbardtech.com"],
                "category": "Software",
                "picture": {"data": {"url": "https://cdn.example/pic.jpg"}},
                "cover": {"source": "https://cdn.example/cover.jpg"},
            }
        }
    )
    provider = facebook.FacebookProvider("main-page", _client=graph)
    info = provider.get_profile()
    assert info.handle == "example"
    assert info.display_name == "Example Page"
    assert info.about == "A short about line"
    assert info.description == "A longer description"
    assert info.website == "https://justbardtech.com"
    assert info.phone == "+1 212 555 0100"
    assert info.emails == ["hi@justbardtech.com"]
    assert info.picture_url == "https://cdn.example/pic.jpg"
    assert info.cover_url == "https://cdn.example/cover.jpg"
    assert info.category == "Software"
    # Should request the writable text fields and the picture/cover edges
    _, params = graph.gets[0]
    assert "about" in params["fields"]
    assert "picture" in params["fields"]
    assert "cover" in params["fields"]


def test_update_profile_joins_emails_with_commas(connected):
    graph = _RecordingGraph()
    provider = facebook.FacebookProvider("main-page", _client=graph)
    provider.update_profile(
        ProfilePatch(
            about="new about",
            website="https://justbardtech.com",
            emails=["a@x.com", "b@x.com"],
        )
    )
    assert graph.posts, "update_profile should POST to the Page node"
    path, data = graph.posts[0]
    assert path == "/100000000000001"
    assert data["about"] == "new about"
    assert data["website"] == "https://justbardtech.com"
    assert data["emails"] == "a@x.com,b@x.com"


def test_update_profile_drops_display_name(connected):
    """Meta Page name changes do not run through Graph POST; keep it dropped."""
    graph = _RecordingGraph()
    provider = facebook.FacebookProvider("main-page", _client=graph)
    provider.update_profile(ProfilePatch(display_name="rename me", about="x"))
    _, data = graph.posts[0]
    assert "name" not in data
    assert "display_name" not in data
    assert data == {"about": "x"}


def test_update_profile_noop_on_empty_patch(connected):
    graph = _RecordingGraph()
    provider = facebook.FacebookProvider("main-page", _client=graph)
    provider.update_profile(ProfilePatch())
    assert graph.posts == []
