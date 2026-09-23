"""
Shared fixtures for scripts/ tests. Separate from alexa_lambda/tests/
conftest.py since these scripts are standalone CLI tools, not Lambda
modules -- they import boto3 directly with no shared module state to
reset between tests.
"""

import sys
from pathlib import Path

import pytest
from moto import mock_aws

SCRIPTS_ROOT = Path(__file__).resolve().parent.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


@pytest.fixture
def mocked_ddb(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("SKILL_USERS_TABLE", "SkillUsers")
    monkeypatch.setenv("SKILL_CALL_LOG_TABLE", "SkillCallLog")

    with mock_aws():
        import boto3

        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="SkillUsers",
            KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "userId", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="SkillCallLog",
            KeySchema=[
                {"AttributeName": "userId", "KeyType": "HASH"},
                {"AttributeName": "timestamp", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "userId", "AttributeType": "S"},
                {"AttributeName": "timestamp", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name="us-east-1")
