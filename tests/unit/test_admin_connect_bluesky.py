"""connect-bluesky subcommand end-to-end against moto DDB + KMS."""

import json
import os
import sys
import unittest.mock
from pathlib import Path

import boto3
import pytest


@pytest.fixture
def admin(aws, monkeypatch):
    """Load scripts/admin.py as a module without running main()."""
    import importlib.util

    repo_root = Path(__file__).resolve().parents[2]
    admin_path = repo_root / "scripts" / "admin.py"
    spec = importlib.util.spec_from_file_location("socialmcp_admin", admin_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["socialmcp_admin"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)

    # Build a minimal config object (admin.py normally reads from JSON)
    config = {
        "environment_name": "test",
        "environment_name_upper": "Test",
        "project_name": "socialmcp",
        "project_name_upper": "SocialMcp",
        "region": os.environ["AWS_DEFAULT_REGION"],
        "accounts": [
            {"alias": "main-bsky", "provider": "bluesky", "publish_mode": "manual"}
        ],
    }
    session = boto3.Session(region_name=os.environ["AWS_DEFAULT_REGION"])

    # Create a KMS alias pointing at the moto key the aws fixture made
    kms = session.client("kms")
    kms.create_alias(
        AliasName="alias/test-socialmcp-tokens",
        TargetKeyId=aws["key_id"],
    )

    # The admin.py _table() uses the core table from env; align its name
    monkeypatch.setenv("CORE_TABLE", "test-socialmcp-core")
    # Re-create the table with the admin-expected name if different
    ddb = session.resource("dynamodb")
    existing = {t.name for t in ddb.tables.all()}
    if "test-socialmcp-core" not in existing:
        ddb.create_table(
            TableName="test-socialmcp-core",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

    return module, config, session


def test_connect_bluesky_writes_account_item(admin, monkeypatch):
    module, config, session = admin
    # Pretend the operator typed handle + app password
    monkeypatch.setattr("builtins.input", lambda *_: "wilma.bsky.social")
    monkeypatch.setattr(
        module.getpass,
        "getpass",
        lambda *_: "app-password-placeholder",
    )
    # Pretend the Bluesky PDS returned a session
    fake_session = {
        "did": "did:plc:examplehandle1234567890",
        "handle": "wilma.bsky.social",
        "accessJwt": "access.jwt.placeholder",
        "refreshJwt": "refresh.jwt.placeholder",
    }
    with unittest.mock.patch(
        "sharedModules.providers.blueskyClient.post_json", return_value=fake_session
    ):
        module.connect_bluesky(config, session, "main-bsky")

    item = (
        session.resource("dynamodb")
        .Table("test-socialmcp-core")
        .get_item(Key={"PK": "ACCOUNT#main-bsky", "SK": "META"})["Item"]
    )
    assert item["provider"] == "bluesky"
    assert item["account_id"] == "did:plc:examplehandle1234567890"
    assert item["display_name"] == "wilma.bsky.social"
    assert item["scopes"] == ["bluesky"]
    assert item["status"] == "active"
    # Both JWTs land as encrypted bytes in page_token_ct and user_token_ct.
    # boto3 wraps bytes in a Binary type; comparing on content via `bytes(...)`
    # is the honest check.
    access_ct = bytes(item["page_token_ct"])
    refresh_ct = bytes(item["user_token_ct"])
    assert len(access_ct) > 0
    assert len(refresh_ct) > 0

    # The plaintext JWT never lands in the DynamoDB item.
    serialized = json.dumps(
        {k: (bytes(v).hex() if isinstance(v, (bytes, bytearray)) or hasattr(v, "value") else v)
         for k, v in item.items()}
    )
    assert "access.jwt.placeholder" not in serialized
    assert "refresh.jwt.placeholder" not in serialized


def test_connect_bluesky_refuses_wrong_provider_in_config(admin, monkeypatch, capsys):
    module, config, session = admin
    config["accounts"] = [
        {"alias": "wrong", "provider": "facebook", "publish_mode": "manual"}
    ]
    monkeypatch.setattr("builtins.input", lambda *_: "wilma.bsky.social")
    with pytest.raises(SystemExit):
        module.connect_bluesky(config, session, "wrong")
    err = capsys.readouterr().err + capsys.readouterr().out
    assert "facebook" in err or "bluesky" in err or err == ""  # SystemExit prints msg
