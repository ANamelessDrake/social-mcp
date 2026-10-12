"""Instagram profile provider mapping (M9).

The Graph API exposes a limited set of IG profile fields and no write
endpoint for them; the provider reflects that honestly.
"""

import json

import pytest
from sharedModules import crypto, dynamo, registry
from sharedModules.errors import CapabilityError
from sharedModules.models import ProfilePatch
from sharedModules.providers import instagram


class _RecordingGraph:
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
        return {}


@pytest.fixture
def connected(monkeypatch, aws):
    entry = {
        "alias": "main-ig",
        "provider": "instagram",
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
        "main-ig",
        {
            "provider": "instagram",
            "account_id": "17841400000000001",
            "display_name": "Example (IG)",
            "scopes": [
                "instagram_basic",
                "instagram_manage_insights",
            ],
            "page_token_ct": token_ct,
            "status": "active",
        },
    )


def test_get_profile_maps_bio_counts_and_website(connected):
    graph = _RecordingGraph(
        {
            "/17841400000000001": {
                "username": "example_ig",
                "name": "Example IG",
                "biography": "makers of stuff",
                "profile_picture_url": "https://cdn.example/ig.jpg",
                "website": "https://justbardtech.com",
                "followers_count": 1234,
                "follows_count": 56,
                "media_count": 89,
            }
        }
    )
    provider = instagram.InstagramProvider("main-ig", _client=graph)
    info = provider.get_profile()
    assert info.handle == "example_ig"
    assert info.display_name == "Example IG"
    assert info.about == "makers of stuff"
    assert info.description == "makers of stuff"
    assert info.picture_url == "https://cdn.example/ig.jpg"
    assert info.website == "https://justbardtech.com"
    assert info.extras["followers_count"] == 1234
    assert info.extras["follows_count"] == 56
    assert info.extras["media_count"] == 89
    # No cover URL on IG; no phone or emails
    assert info.cover_url == ""
    assert info.phone == ""
    assert info.emails == []


def test_update_profile_refuses_cleanly(connected):
    graph = _RecordingGraph()
    provider = instagram.InstagramProvider("main-ig", _client=graph)
    with pytest.raises(CapabilityError) as exc:
        provider.update_profile(ProfilePatch(about="new bio"))
    assert "Instagram app" in str(exc.value) or "Facebook Page" in str(exc.value)
    assert graph.posts == []
