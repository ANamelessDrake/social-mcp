"""Provider dispatch: alias in, correct Provider class out."""

import json

import pytest
from sharedModules import registry
from sharedModules.errors import AccountError
from sharedModules.providers import get_provider
from sharedModules.providers.base import Capability
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


def test_unknown_provider_raises(monkeypatch):
    _set(monkeypatch, [{**_entry("x", "mastodon")}])
    with pytest.raises(AccountError) as exc:
        get_provider("x")
    assert "mastodon" in str(exc.value)


def test_capabilities_differ_between_providers():
    fb = FacebookProvider("fb")
    ig = InstagramProvider("ig")
    # LINK and EDIT are Facebook-only (DESIGN.md section 5)
    assert Capability.LINK in fb.capabilities
    assert Capability.LINK not in ig.capabilities
    assert Capability.EDIT in fb.capabilities
    assert Capability.EDIT not in ig.capabilities
    # Both have reels + insights
    assert Capability.REEL in fb.capabilities and Capability.REEL in ig.capabilities
    assert Capability.INSIGHTS in fb.capabilities and Capability.INSIGHTS in ig.capabilities


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
