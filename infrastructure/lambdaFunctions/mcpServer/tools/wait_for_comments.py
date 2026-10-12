"""Block until the next comment arrives on one of your posts (DESIGN.md M7).

Subscribes to AppSync Events, drains the store past the per-identity
cursor so the subscribe-then-store race is harmless, and either returns
the first matching event (immediate, store or stream) or returns empty
on timeout. Mirrors slack-mcp's wait_for_messages. Delivery survives
offline because webhookIngest's DynamoDB write is unaffected by this
subscription, so a missed stream wake still lands on the next check.
"""

import time

from sharedModules import dynamo
from sharedModules.events import EventSubscription

from .check_comments import _identity_from_scope, check_comments

MAX_WAIT_SECONDS = 840  # stays under the Lambda 900s cap


def wait_for_comments(
    timeout_seconds: int = 60,
    account: str = "",
    post_id: str = "",
) -> dict:
    """Block until a new comment arrives on your posts or `timeout_seconds` elapse.

    Leave `account` and `post_id` empty to wake on any comment on any
    configured account. Pass `account` to narrow to one Page or IG
    account; pass `post_id` to narrow further to one post. Returns as
    soon as one matching comment is available. `timeout_seconds` is
    clamped to [5, 840]. When the timeout fires with no new comment, the
    return is {"ok": true, "comments": [], "timed_out": true}.
    """
    timeout = max(5, min(int(timeout_seconds), MAX_WAIT_SECONDS))
    deadline = time.monotonic() + timeout

    # One subscription per call. For a single-account single-post wait
    # we subscribe to the exact channel; otherwise an account wildcard
    # or the top-level comments wildcard.
    if account and post_id:
        channel = f"social-mcp/comments/{account}/{post_id}"
    elif account:
        channel = f"social-mcp/comments/{account}/*"
    else:
        channel = "social-mcp/comments/*"

    subscription = EventSubscription(channel)
    identity = _identity_from_scope()
    try:
        subscription.connect()
        # Subscribe first, then drain: a comment that lands between the
        # two is caught by the drain, so the "subscribe then store"
        # race window is zero.
        backlog = check_comments(account=account, post_id=post_id)
        if not backlog.get("ok", True):
            return backlog
        if backlog.get("comments"):
            return {"ok": True, "comments": backlog["comments"]}

        while True:
            event = subscription.next_event(deadline)
            if event is None:
                return {"ok": True, "comments": [], "timed_out": True}
            # Advance the matching cursor so a later check does not
            # re-report this one.
            alias = event.get("account", "")
            event_post_id = event.get("post_id", "")
            if alias:
                scope = dynamo.comment_cursor_scope(alias, post_id)
                sk = _sk_from_event(event)
                if sk:
                    dynamo.set_comment_cursor(identity, scope, sk)
            # Apply the account/post filter in case the wildcard
            # subscription delivered an unrelated event.
            if account and alias != account:
                continue
            if post_id and event_post_id != post_id:
                continue
            return {"ok": True, "comments": [event]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"wait failed: {e}"}
    finally:
        subscription.close()


def _sk_from_event(event: dict) -> str:
    """Reconstruct the stored SK from the pushed event shape.

    Ingest publishes the inner item minus PK/SK; the TS#<time>#<id>
    pattern is reproducible from the two fields we did ship."""
    created_time = event.get("created_time", "")
    comment_id = event.get("comment_id", "")
    if not created_time or not comment_id:
        return ""
    return f"TS#{created_time}#{comment_id}"
