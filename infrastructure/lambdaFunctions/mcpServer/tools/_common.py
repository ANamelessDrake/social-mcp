"""Shared tool plumbing: alias resolution, serialization, Publisher invoke."""

import json
import os
from collections.abc import Callable
from dataclasses import asdict
from functools import lru_cache

import boto3
import httpx
from sharedModules.errors import AccountError, CapabilityError
from sharedModules.providers import get_provider
from sharedModules.registry import resolve_alias

# One module-level constant so draft_post, publish_draft, schedule_draft, and
# admin.py approve agree on the string.
PUBLISH_SCOPE = "publish"
SCHEDULE_SCOPE = "schedule"


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


@lru_cache(maxsize=1)
def _lambda_client():
    return boto3.client("lambda")


def invoke_publisher(event: dict) -> dict:
    """Synchronously invoke the Publisher Lambda and parse its envelope."""
    arn = os.environ.get("PUBLISHER_FUNCTION_ARN")
    if not arn:
        return {"ok": False, "error": "PUBLISHER_FUNCTION_ARN is unset (M2 deploy incomplete)"}
    resp = _lambda_client().invoke(
        FunctionName=arn,
        InvocationType="RequestResponse",
        Payload=json.dumps(event).encode(),
    )
    body = resp.get("Payload").read().decode() if resp.get("Payload") else "{}"
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"ok": False, "error": "publisher returned invalid JSON"}
