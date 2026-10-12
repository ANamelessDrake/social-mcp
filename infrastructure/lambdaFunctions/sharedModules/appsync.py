"""Thin AppSync Events publish helper (DESIGN.md M7).

Used by webhookIngest to fan-out comment events to live
`wait_for_comments` sessions. The HTTP endpoint and the API key come
from env variables set by `lambdaFunctionStack`.

Publish is best-effort: a failure is logged and swallowed because the
durable DynamoDB write in ingest is the delivery guarantee.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

log = logging.getLogger(__name__)


def publish(channel: str, event: dict) -> None:
    endpoint = os.environ.get("EVENTS_HTTP_ENDPOINT", "")
    api_key = os.environ.get("EVENTS_API_KEY", "")
    if not endpoint or not api_key:
        return
    try:
        httpx.post(
            endpoint,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
            },
            json={"channel": channel, "events": [json.dumps(event)]},
            timeout=3.0,
        )
    except Exception:  # noqa: BLE001
        log.warning("events publish failed on %s", channel, exc_info=True)
