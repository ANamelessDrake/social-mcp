"""`admin.py connect --remote` flow (M6).

Exercises state-record creation and the poll loop. The browser open and
the time.sleep are patched so the test runs in-process without a real
network or wall clock.
"""

import importlib.util
import os
import sys
from pathlib import Path

import boto3
import pytest


def _load_admin():
    root = Path(__file__).resolve().parents[2]
    path = root / "scripts" / "admin.py"
    spec = importlib.util.spec_from_file_location("social_admin", str(path))
    module = importlib.util.module_from_spec(spec)
    sys.modules["social_admin"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def admin(aws, monkeypatch):
    # Meta app credentials secret with the exact name admin.py expects
    boto3.client("secretsmanager").create_secret(
        Name="Dev-SocialMcp-MetaApp",
        SecretString='{"app_id": "123", "app_secret": "shh"}',
    )

    module = _load_admin()
    # Point admin.py at the moto-backed table and KMS key.
    monkeypatch.setattr(
        module,
        "_oauth_callback_url",
        lambda config, session: "https://example-cb.lambda-url.us-east-1.on.aws",
    )
    stub = type("W", (), {"open": staticmethod(lambda u: None)})()
    monkeypatch.setattr(module, "webbrowser", stub)
    return module


def _config() -> dict:
    # Must match conftest's CORE_TABLE ("test-socialmcp-core") so the
    # moto-mocked table resolves. The Secrets Manager secret name uses
    # the Dev prefix (Dev-SocialMcp-MetaApp) to match the fixture.
    return {
        "environment_name": "test",
        "environment_name_upper": "Dev",
        "project_name": "socialmcp",
        "project_name_upper": "SocialMcp",
        "region": "us-east-1",
        "accounts": [{"alias": "main-page", "provider": "facebook"}],
    }


def _session():
    return boto3.Session(region_name="us-east-1")


def test_remote_writes_pending_state_and_times_out_cleanly(admin, monkeypatch, capsys):
    """When the state never completes, the CLI exits with a timeout message.

    Replace admin.time with a stub; sleep advances the clock enough to
    cross the 10-minute deadline so the poll loop exits in one tick.
    """
    base = 1_000_000.0

    class FakeTime:
        def __init__(self):
            self.now = base

        def time(self):
            return self.now

        def sleep(self, _s):
            self.now += 1200  # jump past the 600s deadline

    monkeypatch.setattr(admin, "time", FakeTime())

    with pytest.raises(SystemExit) as exc:
        admin.connect_remote(
            _config(),
            _session(),
            "main-page",
            scopes_tier="readonly",
            page_id="",
            ig_alias="",
        )
    assert "no completion recorded" in str(exc.value)

    # The state row was written and remains pending.
    rows = boto3.resource("dynamodb").Table(os.environ["CORE_TABLE"]).scan()["Items"]
    state_rows = [r for r in rows if r["PK"].startswith("CONNECT_STATE#")]
    assert len(state_rows) == 1
    assert state_rows[0]["status"] == "pending"
    assert state_rows[0]["alias_prefix"] == "main-page"


def test_remote_detects_completion_and_prints_aliases(admin, monkeypatch, capsys):
    """One poll iteration flips the state to completed and the CLI returns."""
    monkeypatch.setattr(admin.pysecrets, "token_urlsafe", lambda n: "fixed-state")

    def flip_to_completed():
        tbl = boto3.resource("dynamodb").Table(os.environ["CORE_TABLE"])
        tbl.update_item(
            Key={"PK": "CONNECT_STATE#fixed-state", "SK": "META"},
            UpdateExpression="SET #s = :s, aliases = :a",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "completed", ":a": ["main-page"]},
        )

    base = 1_000_000.0

    class FakeTime:
        def __init__(self):
            self.flipped = False

        def time(self):
            # Stay within the deadline so the loop keeps iterating until
            # it sees the completed row.
            return base

        def sleep(self, _s):
            # First sleep (between the pending-first-read and the next
            # iteration) flips the row to completed.
            if not self.flipped:
                self.flipped = True
                flip_to_completed()

    monkeypatch.setattr(admin, "time", FakeTime())

    admin.connect_remote(
        _config(),
        _session(),
        "main-page",
        scopes_tier="readonly",
        page_id="",
        ig_alias="",
    )

    out = capsys.readouterr().out
    assert "connected: main-page" in out


def test_remote_surfaces_error_status(admin, monkeypatch):
    monkeypatch.setattr(admin.pysecrets, "token_urlsafe", lambda n: "err-state")

    def flip_to_error():
        tbl = boto3.resource("dynamodb").Table(os.environ["CORE_TABLE"])
        tbl.update_item(
            Key={"PK": "CONNECT_STATE#err-state", "SK": "META"},
            UpdateExpression="SET #s = :s, message = :m",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "error", ":m": "meta reported: access_denied"},
        )

    base = 1_000_000.0

    class FakeTime:
        def __init__(self):
            self.flipped = False

        def time(self):
            return base

        def sleep(self, _s):
            if not self.flipped:
                self.flipped = True
                flip_to_error()

    monkeypatch.setattr(admin, "time", FakeTime())

    with pytest.raises(SystemExit) as exc:
        admin.connect_remote(
            _config(),
            _session(),
            "main-page",
            scopes_tier="readonly",
            page_id="",
            ig_alias="",
        )
    assert "access_denied" in str(exc.value)
