"""Shared tool plumbing: alias resolution, serialization, and the envelope."""

from collections.abc import Callable
from dataclasses import asdict

import httpx
from sharedModules.errors import AccountError, CapabilityError
from sharedModules.providers import get_provider
from sharedModules.registry import resolve_alias


def serialize(obj) -> dict:
    return asdict(obj)


def provider_action(account: str, action: Callable) -> dict:
    """Run `action(provider, alias)`; normalize failures into the tool envelope."""
    try:
        alias = resolve_alias(account)
        result = action(get_provider(alias), alias)
    except AccountError as e:
        return {"ok": False, "error": str(e)}
    except CapabilityError as e:
        return {"ok": False, "error": str(e)}
    except NotImplementedError as e:
        return {"ok": False, "error": str(e) or "this provider does not support that operation"}
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"network error talking to the provider: {e}"}
    return {"ok": True, **(result or {})}
