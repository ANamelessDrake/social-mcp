"""Read an account's public-facing profile (DESIGN.md section 7.7).

The provider must declare `Capability.PROFILE_READ`; otherwise the tool
refuses with a specific error naming the provider.
"""

from __future__ import annotations

from sharedModules import registry
from sharedModules.errors import AccountError
from sharedModules.providers import get_provider
from sharedModules.providers.base import Capability

from ._common import serialize


def get_profile(account: str = "") -> dict:
    """Fetch the account's profile metadata.

    - `account`: alias from `list_accounts`. Omit when only one account
      is configured. The caller must have scope for this alias.

    Returns `{ok, profile}` where `profile` carries the unified fields:
    handle, display_name, about, description, website, phone, emails,
    picture_url, cover_url, category, plus provider-specific counters
    under `extras`. Facebook populates every text field; Instagram
    populates the subset the Graph API exposes (no cover, no phone, no
    emails) plus follower/following/media counts; Bluesky populates
    display_name, description, avatar, banner, and the three bsky.app
    counters.
    """
    try:
        alias = registry.resolve_alias(account)
    except AccountError as e:
        return {"ok": False, "error": str(e)}

    provider = get_provider(alias)
    if Capability.PROFILE_READ not in provider.capabilities:
        return {
            "ok": False,
            "error": f"provider '{provider.name}' does not expose profile metadata",
        }

    try:
        info = provider.get_profile()
    except AccountError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "profile": serialize(info)}
