"""Provider dispatch: alias in, correct Provider class out."""

import json

import pytest
from sharedModules import registry
from sharedModules.errors import AccountError
from sharedModules.providers import get_provider
from sharedModules.providers.base import Capability
from sharedModules.providers.bluesky import BlueskyProvider
from sharedModules.providers.facebook import FacebookProvider
from sharedModules.providers.instagram import InstagramProvider


def _set(monkeypatch, entries):
    monkeypatch.setenv("ACCOUNTS", json.dumps(entries))
    registry.all_accounts.cache_clear()


def _entry(alias: str, provider: str) -> dict:
    return {
        "alias": alias,
        "provider": provider,
        "publish_enabled": False,
        "publish_mode": "manual",
        "auto_for_kinds": [],
        "policy": {},
    }


def test_dispatches_facebook(monkeypatch):
    _set(monkeypatch, [_entry("fb", "facebook")])
    assert isinstance(get_provider("fb"), FacebookProvider)


def test_dispatches_instagram(monkeypatch):
    _set(monkeypatch, [_entry("ig", "instagram")])
    assert isinstance(get_provider("ig"), InstagramProvider)


def test_dispatches_bluesky(monkeypatch):
    _set(monkeypatch, [_entry("bsky", "bluesky")])
    assert isinstance(get_provider("bsky"), BlueskyProvider)


def test_unknown_provider_raises(monkeypatch):
    _set(monkeypatch, [{**_entry("x", "mastodon")}])
    with pytest.raises(AccountError) as exc:
        get_provider("x")
    assert "mastodon" in str(exc.value)


def test_capabilities_differ_between_providers():
    fb = FacebookProvider("fb")
    ig = InstagramProvider("ig")
    bsky = BlueskyProvider("bsky")
    # LINK and EDIT are Facebook-only vs IG (DESIGN.md section 5)
    assert Capability.LINK in fb.capabilities
    assert Capability.LINK not in ig.capabilities
    assert Capability.EDIT in fb.capabilities
    assert Capability.EDIT not in ig.capabilities
    # Both FB and IG have reels + insights
    assert Capability.REEL in fb.capabilities and Capability.REEL in ig.capabilities
    assert Capability.INSIGHTS in fb.capabilities and Capability.INSIGHTS in ig.capabilities
    # Bluesky: text + link + image + multi_image, no video, no reel, no edit
    assert Capability.TEXT in bsky.capabilities
    assert Capability.LINK in bsky.capabilities
    assert Capability.MULTI_IMAGE in bsky.capabilities
    assert Capability.VIDEO not in bsky.capabilities
    assert Capability.REEL not in bsky.capabilities
    assert Capability.EDIT not in bsky.capabilities
    assert Capability.DELETE in bsky.capabilities
    assert Capability.INSIGHTS in bsky.capabilities


def test_bluesky_edit_and_hide_raise_specific_messages(monkeypatch):
    _set(monkeypatch, [_entry("bsky", "bluesky")])
    bsky = get_provider("bsky")
    with pytest.raises(NotImplementedError) as exc:
        bsky.edit_post("at://x", "new text")
    assert "immutable" in str(exc.value).lower()
    with pytest.raises(NotImplementedError) as exc:
        bsky.hide_comment("at://y")
    assert "mute" in str(exc.value).lower()


def test_instagram_reads_and_publish_are_implemented(monkeypatch):
    """IG reads landed in M4; publish and delete landed in M5."""
    import json as _json

    monkeypatch.setenv(
        "ACCOUNTS",
        _json.dumps(
            [
                {
                    "alias": "ig",
                    "provider": "instagram",
                    "publish_enabled": False,
                    "publish_mode": "manual",
                    "auto_for_kinds": [],
                    "policy": {},
                }
            ]
        ),
    )
    from sharedModules import registry

    registry.all_accounts.cache_clear()

    ig = InstagramProvider("ig")
    assert callable(ig.list_posts)
    assert callable(ig.get_post)
    assert callable(ig.list_comments)
    assert callable(ig.get_insights)
    assert callable(ig.publish)
    assert callable(ig.delete_post)
    # Edits still raise: IG media endpoints never supported content edits.
    with pytest.raises(NotImplementedError):
        ig.edit_post("123", "text")
