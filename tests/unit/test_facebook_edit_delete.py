"""Facebook edit + delete provider mapping (M3) against recorded fixtures."""

import json

import pytest
from conftest import load_fixture
from sharedModules import crypto, dynamo, registry
from sharedModules.errors import AccountError
from sharedModules.providers import facebook


class _RecordingGraph:
    def __init__(self, mapping: dict) -> None:
        self._map = mapping
        self.posts: list[tuple[str, dict]] = []
        self.deletes: list[tuple[str, dict]] = []
        self.gets: list[tuple[str, dict]] = []
        self.raise_on_post: Exception | None = None

    def post(self, path: str, data: dict | None = None, params: dict | None = None) -> dict:
        if self.raise_on_post is not None:
            raise self.raise_on_post
        self.posts.append((path, dict(data or {})))
        for suffix, payload in self._map.items():
            if path.endswith(suffix):
                return payload
        return {}

    def get(self, path: str, params: dict | None = None) -> dict:
        self.gets.append((path, dict(params or {})))
        return {}

    def delete(self, path: str, params: dict | None = None) -> dict:
        self.deletes.append((path, dict(params or {})))
        return {"success": True}


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
                "pages_manage_engagement",
            ],
            "page_token_ct": token_ct,
            "status": "active",
        },
    )


def test_edit_post_sends_message_field(connected):
    graph = _RecordingGraph(
        {"/100000000000001_900000000000099": load_fixture("post_edit.json")}
    )
    provider = facebook.FacebookProvider("main-page", _client=graph)
    provider.edit_post("100000000000001_900000000000099", "new text")
    assert graph.posts[0][0] == "/100000000000001_900000000000099"
    assert graph.posts[0][1]["message"] == "new text"


def test_edit_post_translates_foreign_app_error(connected):
    graph = _RecordingGraph({})
    graph.raise_on_post = AccountError(
        "The Page token does not belong to this Page; the account needs reconnecting"
    )
    provider = facebook.FacebookProvider("main-page", _client=graph)
    with pytest.raises(AccountError) as exc:
        provider.edit_post("100000000000001_900000000000099", "new")
    assert "same Meta app created" in str(exc.value)


def test_delete_post_hits_delete_verb(connected):
    graph = _RecordingGraph({})
    provider = facebook.FacebookProvider("main-page", _client=graph)
    provider.delete_post("100000000000001_900000000000099")
    assert graph.deletes[0][0] == "/100000000000001_900000000000099"
