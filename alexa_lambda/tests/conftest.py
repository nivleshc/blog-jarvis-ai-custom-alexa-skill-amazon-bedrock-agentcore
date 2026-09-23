"""
Shared pytest fixtures for the Lambda test suite. All AWS calls are
mocked via moto -- no real AWS account or credentials are used or
required to run this suite.

Modules under test (allowlist.py, rate_limit.py, call_log.py,
task_registry.py, bedrock_client.py) create their boto3 clients/resources
at module import time. To ensure each test gets a client bound to that
test's moto mock (not a client created before the mock started, or
left over from a previous test), affected modules are removed from
sys.modules and re-imported fresh inside the mocked_aws fixture.
"""

import json
import sys
from pathlib import Path

import pytest
from moto import mock_aws

ALEXA_LAMBDA_ROOT = Path(__file__).resolve().parent.parent
if str(ALEXA_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(ALEXA_LAMBDA_ROOT))

_MODULES_TO_RELOAD = [
    "allowlist",
    "rate_limit",
    "call_log",
    "task_registry",
    "bedrock_client",
    "cost",
    "router",
    "handlers.handler",
    "handlers",
    "watchlist.watchlist",
    "watchlist",
]


@pytest.fixture(autouse=True)
def _base_env(monkeypatch):
    """Environment variables every test needs, regardless of which
    module it's testing. Individual tests can override specific values
    via monkeypatch.setenv."""
    monkeypatch.setenv("SKILL_USERS_TABLE", "SkillUsers")
    monkeypatch.setenv("SKILL_USAGE_COUNTERS_TABLE", "SkillUsageCounters")
    monkeypatch.setenv("GLOBAL_USAGE_COUNTERS_TABLE", "GlobalUsageCounters")
    monkeypatch.setenv("SKILL_CALL_LOG_TABLE", "SkillCallLog")
    monkeypatch.setenv("TASK_REGISTRY_BUCKET", "test-task-registry-bucket")
    monkeypatch.setenv("TASK_REGISTRY_KEY", "task_registry.json")
    monkeypatch.setenv("ALEXA_SKILL_ID", "amzn1.ask.skill.test123")
    monkeypatch.setenv("MAX_INPUT_CHARS", "800")
    monkeypatch.setenv("GLOBAL_DEFAULT_DAILY_LIMIT", "20")
    monkeypatch.setenv("GLOBAL_DEFAULT_BURST_LIMIT", "5")
    monkeypatch.setenv("GLOBAL_DAILY_CEILING", "200")
    monkeypatch.setenv("USAGE_COUNTER_TTL_DAYS", "2")
    monkeypatch.setenv("CALL_LOG_TTL_DAYS", "0")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
    monkeypatch.setenv("BEDROCK_MAX_TOKENS", "512")
    monkeypatch.setenv("INPUT_PRICE_PER_MILLION_TOKENS", "0.06")
    monkeypatch.setenv("OUTPUT_PRICE_PER_MILLION_TOKENS", "0.24")
    monkeypatch.setenv("EMF_NAMESPACE", "test-namespace")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("SKILL_WATCHLIST_TABLE", "test-skill-watchlist-dev")


def _reload_modules():
    for mod in _MODULES_TO_RELOAD:
        sys.modules.pop(mod, None)


@pytest.fixture
def mocked_aws(_base_env):
    """
    Starts a moto mock_aws context, creates the 4 DynamoDB tables and the
    task registry S3 bucket/object with a minimal default registry, then
    force-reloads all AWS-client-holding modules so they bind to the
    mocked clients. Yields a dict of boto3 resources/clients test bodies
    can use directly.
    """
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
            TableName="SkillUsageCounters",
            KeySchema=[{"AttributeName": "counter_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "counter_key", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ddb.create_table(
            TableName="GlobalUsageCounters",
            KeySchema=[{"AttributeName": "counter_key", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "counter_key", "AttributeType": "S"}],
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
        ddb.create_table(
            TableName="test-skill-watchlist-dev",
            KeySchema=[
                {"AttributeName": "userId", "KeyType": "HASH"},
                {"AttributeName": "itemId", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "userId", "AttributeType": "S"},
                {"AttributeName": "itemId", "AttributeType": "S"},
                {"AttributeName": "title", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "TitleIndex",
                    "KeySchema": [{"AttributeName": "title", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                    "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-task-registry-bucket")
        default_registry = {
            "AskBedrock": {"type": "foundation_model", "description": "test", "parameters": []},
            "GetWeather": {
                "type": "lambda",
                "description": "test placeholder",
                "lambda_function_arn": "arn:aws:lambda:us-east-1:123456789012:function:FAKE",
                "parameters": ["location"],
            },
            "BoredomBuster": {
                "type": "agentcore_task",
                "description": "test placeholder",
                "agent_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/FAKE",
                "parameters": ["mood_or_genre"],
            },
        }
        s3.put_object(
            Bucket="test-task-registry-bucket",
            Key="task_registry.json",
            Body=json.dumps(default_registry),
        )

        _reload_modules()
        import task_registry as tr

        tr._cache["data"] = None  # force a fresh fetch, ignore any stale module cache

        yield {
            "dynamodb": boto3.resource("dynamodb", region_name="us-east-1"),
            "s3": s3,
        }

    _reload_modules()
