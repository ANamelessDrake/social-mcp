"""Bluesky profile provider mapping (M9).

Profile data comes from two XRPC calls (actor.getProfile for counts and
avatar/banner URLs; repo.getRecord for the raw record values); update
preserves avatar/banner refs and sends `swapRecord` for the cid guard.
"""

import json

import pytest
from sharedModules import crypto, dynamo, registry
from sharedModules.errors import AccountError
from sharedModules.models import ProfilePatch
from sharedModules.providers import bluesky


class _FakeClient:
    def __init__(self, get_mapping: dict, post_mapping: dict | None = None) -> None:
        self._gets = get_mapping
        self._posts = post_mapping or {}
        self.get_calls: list[tuple[str, dict]] = []
        self.post_calls: list[tuple[str, dict]] = []
        self.raise_get_on: dict[str, Exception] = {}

    def get(self, nsid: str, params: dict | None = None) -> dict:
        self.get_calls.append((nsid, dict(params or {})))
        if nsid in self.raise_get_on:
            raise self.raise_get_on[nsid]
        if nsid in self._gets:
            return self._gets[nsid]
        raise AssertionError(f"unexpected GET {nsid}")

    def post(self, nsid: str, body: dict | None = None) -> dict:
        self.post_calls.append((nsid, dict(body or {})))
        return self._posts.get(nsid, {})


DID = "did:plc:examplehandle1234567890"


@pytest.fixture
def connected(monkeypatch, aws):
    entry = {
        "alias": "main-bsky",
        "provider": "bluesky",
        "publish_enabled": True,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "profile_edits_enabled": True,
        "policy": {},
    }
    monkeypatch.setenv("ACCOUNTS", json.dumps([entry]))
    registry.all_accounts.cache_clear()
    access_ct = crypto.encrypt("access")
    refresh_ct = crypto.encrypt("refresh")
    dynamo.put_account(
        "main-bsky",
        {
            "provider": "bluesky",
            "account_id": DID,
            "display_name": "wilma.bsky.social",
            "scopes": ["bluesky"],
            "page_token_ct": access_ct,
            "user_token_ct": refresh_ct,
            "status": "active",
        },
    )


def test_get_profile_merges_actor_and_record(connected):
    actor_resp = {
        "handle": "wilma.bsky.social",
        "displayName": "actor-level",
        "description": "actor-level bio",
        "avatar": "https://cdn.bsky.app/img/avatar/abc",
        "banner": "https://cdn.bsky.app/img/banner/def",
        "followersCount": 42,
        "followsCount": 7,
        "postsCount": 100,
    }
    record_resp = {
        "value": {
            "$type": "app.bsky.actor.profile",
            "displayName": "record-level name",
            "description": "record-level bio",
            "avatar": {"$type": "blob", "ref": {"$link": "sha256-xxxx"}},
            "banner": {"$type": "blob", "ref": {"$link": "sha256-yyyy"}},
        },
        "cid": "bafycid1234",
    }
    client = _FakeClient(
        {
            "app.bsky.actor.getProfile": actor_resp,
            "com.atproto.repo.getRecord": record_resp,
        }
    )
    provider = bluesky.BlueskyProvider("main-bsky", _client=client)
    info = provider.get_profile()
    # Record values win over actor; avatar/banner URLs come from the actor
    assert info.display_name == "record-level name"
    assert info.description == "record-level bio"
    assert info.about == "record-level bio"
    assert info.picture_url == "https://cdn.bsky.app/img/avatar/abc"
    assert info.cover_url == "https://cdn.bsky.app/img/banner/def"
    assert info.handle == "wilma.bsky.social"
    assert info.extras == {
        "followers_count": 42,
        "follows_count": 7,
        "posts_count": 100,
    }


def test_get_profile_tolerates_missing_record(connected):
    actor_resp = {
        "handle": "wilma.bsky.social",
        "displayName": "fallback",
        "description": "fallback bio",
    }
    client = _FakeClient({"app.bsky.actor.getProfile": actor_resp})
    client.raise_get_on["com.atproto.repo.getRecord"] = AccountError(
        "The requested record does not exist on this PDS"
    )
    provider = bluesky.BlueskyProvider("main-bsky", _client=client)
    info = provider.get_profile()
    assert info.display_name == "fallback"
    assert info.description == "fallback bio"


def test_update_profile_preserves_avatar_and_sends_swap_cid(connected):
    existing_record = {
        "value": {
            "$type": "app.bsky.actor.profile",
            "displayName": "old name",
            "description": "old bio",
            "avatar": {"$type": "blob", "ref": {"$link": "sha256-xxxx"}},
            "banner": {"$type": "blob", "ref": {"$link": "sha256-yyyy"}},
        },
        "cid": "bafycid5678",
    }
    client = _FakeClient({"com.atproto.repo.getRecord": existing_record})
    provider = bluesky.BlueskyProvider("main-bsky", _client=client)
    provider.update_profile(ProfilePatch(display_name="New Name", description="new bio"))
    nsid, body = client.post_calls[0]
    assert nsid == "com.atproto.repo.putRecord"
    assert body["repo"] == DID
    assert body["collection"] == "app.bsky.actor.profile"
    assert body["rkey"] == "self"
    assert body["swapRecord"] == "bafycid5678"
    rec = body["record"]
    assert rec["displayName"] == "New Name"
    assert rec["description"] == "new bio"
    # Avatar + banner blob refs preserved from the existing record
    assert rec["avatar"] == {"$type": "blob", "ref": {"$link": "sha256-xxxx"}}
    assert rec["banner"] == {"$type": "blob", "ref": {"$link": "sha256-yyyy"}}


def test_update_profile_creates_fresh_record_when_none_exists(connected):
    client = _FakeClient({})
    client.raise_get_on["com.atproto.repo.getRecord"] = AccountError(
        "The requested record does not exist on this PDS"
    )
    provider = bluesky.BlueskyProvider("main-bsky", _client=client)
    provider.update_profile(ProfilePatch(display_name="New", description="hi"))
    nsid, body = client.post_calls[0]
    assert nsid == "com.atproto.repo.putRecord"
    # No swapRecord when there is no existing cid
    assert "swapRecord" not in body
    assert body["record"]["displayName"] == "New"
    assert body["record"]["description"] == "hi"
    # No avatar/banner because the record was fresh
    assert "avatar" not in body["record"]
    assert "banner" not in body["record"]
