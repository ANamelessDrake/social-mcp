"""Facebook comment-reply + hide provider mapping (M3)."""

import json

import pytest
from conftest import load_fixture
from sharedModules import crypto, dynamo, registry
from sharedModules.providers import facebook


class _RecordingGraph:
    def __init__(self, mapping: dict) -> None:
        self._map = mapping
        self.posts: list[tuple[str, dict]] = []

    def post(self, path: str, data: dict | None = None, params: dict | None = None) -> dict:
        self.posts.append((path, dict(data or {})))
        for suffix, payload in self._map.items():
            if path.endswith(suffix):
                return payload
        return {}

    def get(self, *_a, **_k):
        return {}

    def delete(self, *_a, **_k):
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
    dynamo.put_account(
        "main-page",
        {
            "provider": "facebook",
            "account_id": "100000000000001",
            "display_name": "Example Page",
            "scopes": ["pages_manage_engagement"],
            "page_token_ct": crypto.encrypt("token"),
            "status": "active",
        },
    )


COMMENT = "100000000000001_900000000000400"


def test_reply_posts_to_comment_edge_and_returns_new_comment(connected):
    graph = _RecordingGraph({f"/{COMMENT}/comments": load_fixture("comment_reply.json")})
    provider = facebook.FacebookProvider("main-page", _client=graph)
    reply = provider.reply_to_comment(COMMENT, "thanks")
    assert graph.posts[0][0] == f"/{COMMENT}/comments"
    assert graph.posts[0][1]["message"] == "thanks"
    assert reply.comment_id == "100000000000001_900000000000500"
    assert reply.thread_id == COMMENT


def test_hide_posts_is_hidden_true(connected):
    graph = _RecordingGraph({f"/{COMMENT}": load_fixture("comment_hide.json")})
    provider = facebook.FacebookProvider("main-page", _client=graph)
    provider.hide_comment(COMMENT)
    assert graph.posts[0][0] == f"/{COMMENT}"
    assert graph.posts[0][1]["is_hidden"] == "true"
