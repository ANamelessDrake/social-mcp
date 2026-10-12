"""Bluesky provider publish-op tests.

Covers text + facet detection, link (text with URL auto-facet), single
image (one blob upload), multi-image (4 blobs, 5 rejected), reply (root
ref resolution), delete, and the two still-unsupported verbs (edit,
hide_comment).
"""

import json

import pytest
from conftest import load_bluesky_fixture
from sharedModules import crypto, dynamo, registry
from sharedModules.errors import CapabilityError
from sharedModules.models import MediaRef
from sharedModules.providers import bluesky


class _FakeClient:
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
        return load_bluesky_fixture("upload_blob.json")


class _Draft:
    def __init__(self, **kwargs):
        self.kind = kwargs.get("kind", "text")
        self.text = kwargs.get("text", "")
        self.link = kwargs.get("link", "")
        self.media = kwargs.get("media", [])
        self.schedule = kwargs.get("schedule", "")
        self.is_ai_generated = kwargs.get("is_ai_generated", False)


def _entry(alias: str) -> dict:
    return {
        "alias": alias,
        "provider": "bluesky",
        "publish_enabled": True,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "policy": {},
    }


@pytest.fixture
def bsky_provider(monkeypatch, aws):
    monkeypatch.setenv("ACCOUNTS", json.dumps([_entry("main-bsky")]))
    registry.all_accounts.cache_clear()
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
            ("POST", "com.atproto.repo.createRecord"): load_bluesky_fixture("create_record.json"),
            ("POST", "com.atproto.repo.deleteRecord"): {},
            ("GET", "app.bsky.feed.getPostThread"): load_bluesky_fixture("post_thread.json"),
        }
    )
    provider = bluesky.BlueskyProvider("main-bsky", _client=fake)
    return provider, fake


def test_publish_text_creates_record_without_facets(bsky_provider):
    provider, fake = bsky_provider
    result = provider.publish(_Draft(kind="text", text="hello bluesky"))
    assert result.post_id.startswith("at://did:plc:")
    assert result.permalink == (
        "https://bsky.app/profile/wilma.bsky.social/post/3la000new1"
    )
    _, nsid, body = fake.calls[-1]
    assert nsid == "com.atproto.repo.createRecord"
    assert body["record"]["$type"] == "app.bsky.feed.post"
    assert "facets" not in body["record"]


def test_publish_text_detects_url_facets(bsky_provider):
    provider, fake = bsky_provider
    text = "deploy notes at https://example.com/notes thanks"
    provider.publish(_Draft(kind="text", text=text))
    body = fake.calls[-1][2]
    facets = body["record"]["facets"]
    assert len(facets) == 1
    index = facets[0]["index"]
    # Byte offsets must match the URL's position in the UTF-8 encoding
    url = "https://example.com/notes"
    byte_start = text.encode("utf-8").index(url.encode("utf-8"))
    assert index["byteStart"] == byte_start
    assert index["byteEnd"] == byte_start + len(url)
    assert facets[0]["features"][0]["uri"] == url


def test_facet_byte_offsets_handle_unicode(bsky_provider):
    provider, fake = bsky_provider
    # Rocket emoji is 4 bytes in UTF-8; the URL that follows must land at
    # byte offset `len("rocket ".encode() + rocket_bytes + " ".encode())`.
    text = "rocket \U0001f680 https://example.com launch"
    provider.publish(_Draft(kind="text", text=text))
    body = fake.calls[-1][2]
    facets = body["record"]["facets"]
    assert len(facets) == 1
    url = "https://example.com"
    expected_start = text.encode("utf-8").index(url.encode("utf-8"))
    assert facets[0]["index"]["byteStart"] == expected_start
    # And byte_end minus byte_start equals the URL byte length
    assert facets[0]["index"]["byteEnd"] - facets[0]["index"]["byteStart"] == len(
        url.encode("utf-8")
    )


def test_publish_link_appends_url_and_facets_it(bsky_provider):
    provider, fake = bsky_provider
    provider.publish(_Draft(kind="link", text="read this", link="https://example.com/x"))
    body = fake.calls[-1][2]
    text = body["record"]["text"]
    assert "https://example.com/x" in text
    assert body["record"]["facets"][0]["features"][0]["uri"] == "https://example.com/x"


def test_publish_image_uploads_blob_and_embeds_it(bsky_provider, monkeypatch):
    provider, fake = bsky_provider
    # Patch httpx.Client.get to return bytes without hitting the network
    monkeypatch.setattr(
        bluesky,
        "_media_urls",
        lambda draft: [
            getattr(m, "key", m if isinstance(m, str) else "") for m in draft.media
        ],
    )

    class _FakeResp:
        status_code = 200
        content = b"image-bytes"
        headers = {"content-type": "image/jpeg"}

    class _FakeHttp:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):  # noqa: ARG002
            return _FakeResp()

    monkeypatch.setattr("httpx.Client", _FakeHttp)
    result = provider.publish(
        _Draft(
            kind="image",
            text="look at this",
            media=[MediaRef(source="url", key="https://example.com/img.jpg", mime="image/jpeg")],
        )
    )
    assert result.post_id.startswith("at://did:plc:")
    body = fake.calls[-1][2]
    embed = body["record"]["embed"]
    assert embed["$type"] == "app.bsky.embed.images"
    assert len(embed["images"]) == 1
    assert embed["images"][0]["image"]["mimeType"] == "image/jpeg"
    assert fake.blob_calls == [(len(b"image-bytes"), "image/jpeg")]


def test_publish_multi_image_up_to_four(bsky_provider, monkeypatch):
    provider, fake = bsky_provider
    monkeypatch.setattr(
        bluesky,
        "_media_urls",
        lambda draft: [
            getattr(m, "key", m if isinstance(m, str) else "") for m in draft.media
        ],
    )

    class _FakeResp:
        status_code = 200
        content = b"image-bytes"
        headers = {"content-type": "image/jpeg"}

    class _FakeHttp:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):  # noqa: ARG002
            return _FakeResp()

    monkeypatch.setattr("httpx.Client", _FakeHttp)
    draft = _Draft(
        kind="multi_image",
        text="four shots",
        media=[
            MediaRef(source="url", key=f"https://example.com/img{i}.jpg", mime="image/jpeg")
            for i in range(4)
        ],
    )
    provider.publish(draft)
    body = fake.calls[-1][2]
    assert len(body["record"]["embed"]["images"]) == 4
    assert len(fake.blob_calls) == 4


def test_publish_multi_image_rejects_more_than_four(bsky_provider):
    provider, _ = bsky_provider
    draft = _Draft(
        kind="multi_image",
        media=[
            MediaRef(source="url", key=f"https://example.com/img{i}.jpg", mime="image/jpeg")
            for i in range(5)
        ],
    )
    from sharedModules.errors import AccountError

    with pytest.raises(AccountError) as exc:
        provider.publish(draft)
    assert "4" in str(exc.value)


def test_publish_rejects_s3_sources(bsky_provider):
    provider, _ = bsky_provider
    draft = _Draft(
        kind="image",
        media=[MediaRef(source="s3", key="staging/main-bsky/x.jpg", mime="image/jpeg")],
    )
    with pytest.raises(CapabilityError) as exc:
        provider.publish(draft)
    assert "presign" in str(exc.value).lower()


def test_reply_to_comment_resolves_thread_root(bsky_provider):
    provider, fake = bsky_provider
    # Register a thread fixture whose post IS the reply, with its record.reply
    # pointing at the thread root. The provider should walk that record to
    # find the root ref for the new reply's root field.
    comment_uri = (
        "at://did:plc:examplereplier0000000001/app.bsky.feed.post/3la000reply1"
    )
    root_uri = "at://did:plc:examplehandle1234567890/app.bsky.feed.post/3la000text1"
    fake._map[("GET", "app.bsky.feed.getPostThread")] = {
        "thread": {
            "post": {
                "uri": comment_uri,
                "cid": "bafyreiexamplereply1",
                "record": {
                    "$type": "app.bsky.feed.post",
                    "text": "Nice work. Rollout time?",
                    "createdAt": "2026-10-10T18:10:00Z",
                    "reply": {
                        "root": {"uri": root_uri, "cid": "bafyreiexampletext1"},
                        "parent": {"uri": root_uri, "cid": "bafyreiexampletext1"},
                    },
                },
            }
        }
    }
    reply = provider.reply_to_comment(comment_uri, "thanks!")
    body = fake.calls[-1][2]
    record = body["record"]
    assert record["reply"]["parent"]["uri"] == comment_uri
    assert record["reply"]["root"]["uri"] == root_uri
    assert reply.thread_id == comment_uri


def test_delete_post_parses_at_uri(bsky_provider):
    provider, fake = bsky_provider
    uri = "at://did:plc:examplehandle1234567890/app.bsky.feed.post/3la000text1"
    provider.delete_post(uri)
    _, nsid, body = fake.calls[-1]
    assert nsid == "com.atproto.repo.deleteRecord"
    assert body["collection"] == "app.bsky.feed.post"
    assert body["rkey"] == "3la000text1"


def test_delete_post_rejects_non_at_uri(bsky_provider):
    provider, _ = bsky_provider
    from sharedModules.errors import AccountError

    with pytest.raises(AccountError):
        provider.delete_post("not-an-at-uri")


def test_edit_and_hide_raise_not_implemented(bsky_provider):
    provider, _ = bsky_provider
    with pytest.raises(NotImplementedError) as exc:
        provider.edit_post("x", "y")
    assert "immutable" in str(exc.value).lower()
    with pytest.raises(NotImplementedError) as exc:
        provider.hide_comment("c")
    assert "mute" in str(exc.value).lower()


def test_bluesky_insights_zero_impressions_but_counts_reactions(bsky_provider):
    provider, _ = bsky_provider
    ins = provider.get_insights(
        "at://did:plc:examplehandle1234567890/app.bsky.feed.post/3la000text1"
    )
    assert ins.impressions == 0
    assert ins.reach == 0
    assert ins.views == 0
    assert ins.reactions == 7
    assert ins.comments == 2
    assert ins.shares == 1  # repost (1) + quote (0)
