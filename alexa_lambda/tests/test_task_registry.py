"""Tests for task_registry.py -- S3-backed hot-reload, solution-design.md
Section 11."""

import json
import time


def test_get_task_registry_loads_from_s3(mocked_aws):
    from task_registry import get_task_registry

    registry = get_task_registry()
    assert "AskBedrock" in registry
    assert registry["AskBedrock"]["type"] == "foundation_model"


def test_comment_field_is_excluded_from_returned_registry(mocked_aws):
    from task_registry import get_task_registry

    registry = get_task_registry()
    assert "_comment" not in registry


def test_get_task_returns_specific_entry(mocked_aws):
    """GetWeather is on the 'lambda' backend type, not AgentCore."""
    from task_registry import get_task

    task = get_task("GetWeather")
    assert task["type"] == "lambda"
    assert "lambda_function_arn" in task


def test_get_task_raises_keyerror_for_unknown_task(mocked_aws):
    from task_registry import get_task

    try:
        get_task("NoSuchTask")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_registry_is_cached_between_calls(mocked_aws):
    """A second call within the cache TTL should not re-fetch from S3 --
    verified by changing the S3 object and confirming the OLD value is
    still returned until the cache is cleared."""
    import task_registry as tr

    tr.get_task_registry()  # populate the cache
    original_fetch_time = tr._cache["fetched_at"]

    mocked_aws["s3"].put_object(
        Bucket="test-task-registry-bucket",
        Key="task_registry.json",
        Body=json.dumps({"SomethingNew": {"type": "foundation_model", "description": "x", "parameters": []}}),
    )

    registry = tr.get_task_registry()
    assert "AskBedrock" in registry, "expected the cached (old) registry, not the freshly-uploaded one"
    assert tr._cache["fetched_at"] == original_fetch_time


def test_registry_refetches_after_cache_expires(mocked_aws):
    import task_registry as tr

    tr.get_task_registry()

    mocked_aws["s3"].put_object(
        Bucket="test-task-registry-bucket",
        Key="task_registry.json",
        Body=json.dumps({"SomethingNew": {"type": "foundation_model", "description": "x", "parameters": []}}),
    )

    # Force the cache to look expired without actually sleeping for
    # _CACHE_TTL_SECONDS in the test.
    tr._cache["fetched_at"] = time.time() - tr._CACHE_TTL_SECONDS - 1

    registry = tr.get_task_registry()
    assert "SomethingNew" in registry
    assert "AskBedrock" not in registry


def test_is_task_enabled_true_for_real_arn():
    """is_task_enabled is what lets the welcome message only advertise
    tasks that have a real agent/function behind them, rather than
    hardcoding task names regardless of whether they actually work."""
    from task_registry import is_task_enabled

    assert is_task_enabled({"type": "lambda", "lambda_function_arn": "arn:aws:lambda:us-east-1:123456789012:function:real"})
    assert is_task_enabled({"type": "agentcore_task", "agent_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/real-agent"})


def test_is_task_enabled_false_for_placeholder_arn():
    from task_registry import is_task_enabled

    assert not is_task_enabled({"type": "agentcore_task", "agent_runtime_arn": "arn:aws:bedrock-agentcore:REGION:ACCOUNT_ID:runtime/PLACEHOLDER-notes-agent"})
    assert not is_task_enabled({"type": "lambda", "lambda_function_arn": "PLACEHOLDER-something"})


def test_is_task_enabled_true_for_foundation_model_with_no_arn_field():
    """foundation_model tasks (e.g. AskBedrock) have neither
    agent_runtime_arn nor lambda_function_arn -- they need no external
    resource, so they're always enabled."""
    from task_registry import is_task_enabled

    assert is_task_enabled({"type": "foundation_model", "description": "x", "parameters": []})
