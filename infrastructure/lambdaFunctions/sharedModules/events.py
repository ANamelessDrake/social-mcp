"""AppSync Events WebSocket subscriber (DESIGN.md M7).

Implements the aws-appsync-event-ws protocol with API key auth: the
connection carries a base64url-encoded auth header as a WebSocket
subprotocol, then JSON frames handle init / subscribe / data. Used by
`wait_for_comments` to block until a comment event is published or the
deadline passes.

The pattern is adapted from slack-mcp's events.py; it is kept small and
dependency-light (just `websocket-client`) so the MCP server bundle
stays modest.
"""

from __future__ import annotations

import base64
import json
import os
import time
import uuid

import websocket


def _auth_header() -> dict:
    return {
        "host": os.environ["EVENTS_HTTP_HOST"],
        "x-api-key": os.environ["EVENTS_API_KEY"],
    }


def _encode_subprotocol(header: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    return f"header-{encoded}"


class EventSubscription:
    """One WebSocket subscription to an AppSync Events channel.

    Supports concrete channels (`social-mcp/comments/<alias>/<post>`) and
    account-wide wildcards (`social-mcp/comments/<alias>/*`).
    """

    def __init__(self, channel: str):
        self.channel = channel
        self._ws = None

    def connect(self, timeout: float = 10.0) -> None:
        self._ws = websocket.create_connection(
            os.environ["EVENTS_REALTIME_ENDPOINT"],
            subprotocols=["aws-appsync-event-ws", _encode_subprotocol(_auth_header())],
            timeout=timeout,
        )
        self._ws.send(json.dumps({"type": "connection_init"}))
        self._await("connection_ack", timeout)

        sub_id = str(uuid.uuid4())
        self._ws.send(
            json.dumps(
                {
                    "type": "subscribe",
                    "id": sub_id,
                    "channel": self.channel,
                    "authorization": _auth_header(),
                }
            )
        )
        self._await("subscribe_success", timeout)

    def _await(self, expected: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = json.loads(self._ws.recv())
            kind = frame.get("type")
            if kind == expected:
                return
            if kind in ("connection_error", "subscribe_error", "error"):
                raise ConnectionError(f"events api returned {kind}: {frame}")
        raise TimeoutError(f"timed out waiting for {expected}")

    def next_event(self, deadline: float) -> dict | None:
        """Block until a published event arrives or the deadline passes."""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self._ws.settimeout(remaining)
            try:
                frame = json.loads(self._ws.recv())
            except websocket.WebSocketTimeoutException:
                return None
            if frame.get("type") == "data":
                return json.loads(frame["event"])
            # ka (keepalive) and other frames: keep waiting

    def close(self) -> None:
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:  # noqa: BLE001
                pass
