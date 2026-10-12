import json
import os
import sys
from pathlib import Path

import boto3
import pytest

ROOT = Path(__file__).resolve().parents[2]
LAMBDA_DIR = ROOT / "infrastructure" / "lambdaFunctions"
FIXTURES = ROOT / "tests" / "fixtures" / "graph"

# Mirror the deployed bundle layout: function code at the root,
# sharedModules beside it.
sys.path.insert(0, str(LAMBDA_DIR))
sys.path.insert(0, str(LAMBDA_DIR / "mcpServer"))

# Local-mode auth and account roster, set before app modules are imported
os.environ.setdefault("DEV_BEARER_TOKEN", "test-token")
os.environ.setdefault(
    "ACCOUNTS",
    json.dumps(
        [
            {
                "alias": "main-page",
                "provider": "facebook",
                "publish_enabled": False,
                "publish_mode": "manual",
                "auto_for_kinds": [],
                "policy": {},
            }
        ]
    ),
)
os.environ.setdefault("CORE_TABLE", "test-socialmcp-core")
os.environ.setdefault("PUBLISHING_ENABLED", "false")

# CI runners have no AWS config; moto still needs a region and fake
# credentials for boto3 clients created inside the code under test.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def clear_caches() -> None:
    """Drop every cached boto3 client/resource so each test gets moto's."""
    from sharedModules import crypto, dynamo, registry

    dynamo.core_table.cache_clear()
    crypto._kms.cache_clear()
    registry.all_accounts.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_registry():
    """Neither the roster cache nor a pinned account scope may leak between
    tests; a stray ContextVar would silently narrow an unrelated test."""
    from sharedModules import registry, scope

    registry.all_accounts.cache_clear()
    scope.set_scope(None)
    yield
    registry.all_accounts.cache_clear()
    scope.set_scope(None)


@pytest.fixture
def aws(monkeypatch):
    """moto-backed DynamoDB table and KMS key matching the deployed layout."""
    from moto import mock_aws

    with mock_aws():
        clear_caches()
        boto3.resource("dynamodb").create_table(
            TableName=os.environ["CORE_TABLE"],
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
        key_id = boto3.client("kms").create_key()["KeyMetadata"]["KeyId"]
        monkeypatch.setenv("TOKEN_KMS_KEY_ID", key_id)
        yield {"key_id": key_id}
