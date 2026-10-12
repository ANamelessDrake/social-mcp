"""check_comments: pull-based inbox drain with per-filter cursors."""

import boto3
from sharedModules import dynamo


def _write_event(table, alias: str, created_time: str, comment_id: str, post_id: str = "P"):
    table.put_item(
        Item={
            "PK": f"COMMENT_EVENT#{alias}",
            "SK": f"TS#{created_time}#{comment_id}",
            "account": alias,
            "provider": "facebook",
            "post_id": post_id,
            "comment_id": comment_id,
            "thread_id": comment_id,
            "created_time": created_time,
            "author": "A",
            "text": f"c{comment_id}",
            "is_hidden": False,
            "body_fetched": True,
            "ingested_at": created_time,
        }
    )


def test_returns_events_oldest_first_and_advances_cursor(aws):
    table = boto3.resource("dynamodb").Table("test-socialmcp-core")
    for t, cid in [("2026-10-01T00:00:00", "1"), ("2026-10-01T00:01:00", "2")]:
        _write_event(table, "main-page", t, cid)

    from tools.check_comments import check_comments

    out = check_comments(account="main-page")
    assert out["ok"] is True
    assert [c["comment_id"] for c in out["comments"]] == ["1", "2"]

    # Second call: nothing new.
    out2 = check_comments(account="main-page")
    assert out2["comments"] == []


def test_post_id_filter_keeps_its_own_cursor(aws):
    table = boto3.resource("dynamodb").Table("test-socialmcp-core")
    _write_event(table, "main-page", "2026-10-01T00:00:00", "1", post_id="P1")
    _write_event(table, "main-page", "2026-10-01T00:01:00", "2", post_id="P2")

    from tools.check_comments import check_comments

    filtered = check_comments(account="main-page", post_id="P1")
    assert [c["comment_id"] for c in filtered["comments"]] == ["1"]

    unfiltered = check_comments(account="main-page")
    assert sorted(c["comment_id"] for c in unfiltered["comments"]) == ["1", "2"]


def test_empty_account_sweeps_known_aliases(aws):
    table = boto3.resource("dynamodb").Table("test-socialmcp-core")
    _write_event(table, "main-page", "2026-10-01T00:00:00", "1")
    _write_event(table, "news-ig", "2026-10-01T00:01:00", "2")

    aliases = dynamo.list_comment_accounts()
    assert set(aliases) == {"main-page", "news-ig"}

    from tools.check_comments import check_comments

    out = check_comments()
    assert sorted(c["comment_id"] for c in out["comments"]) == ["1", "2"]


def test_unknown_account_errors(aws):
    from tools.check_comments import check_comments

    out = check_comments(account="missing")
    assert out["ok"] is False
    assert "missing" in out["error"]
