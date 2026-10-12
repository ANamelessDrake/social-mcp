"""Provider dispatch: alias in, Provider out.

The registry says which provider an account is; nothing above this line ever
branches on provider again (DESIGN.md section 5).
"""

from sharedModules import registry
from sharedModules.errors import AccountError

from .base import Provider
from .bluesky import BlueskyProvider
from .facebook import FacebookProvider
from .instagram import InstagramProvider


def get_provider(alias: str) -> Provider:
    provider = registry.configured_accounts()[alias]["provider"]
    if provider == "facebook":
        return FacebookProvider(alias)
    if provider == "instagram":
        return InstagramProvider(alias)
    if provider == "bluesky":
        return BlueskyProvider(alias)
    raise AccountError(
        f"provider '{provider}' is not implemented; see providers/stub.py for "
        "the starting point for a new one"
    )
