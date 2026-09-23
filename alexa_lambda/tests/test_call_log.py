"""Tests for call_log.py -- solution-design.md Section 5.4."""


def test_write_call_log_success_entry(mocked_aws):
    from call_log import write_call_log

    write_call_log(
        user_id="amzn1.ask.account.TESTUSER",
        intent_or_task="SmartAssistantIntent",
        input_tokens=10,
        output_tokens=20,
        estimated_cost_usd=0.001,
        latency_ms=150.5,
        success=True,
    )

    table = mocked_aws["dynamodb"].Table("SkillCallLog")
    items = table.scan()["Items"]
    assert len(items) == 1
    item = items[0]
    assert item["userId"] == "amzn1.ask.account.TESTUSER"
    assert item["intent_or_task"] == "SmartAssistantIntent"
    assert item["input_tokens"] == 10
    assert item["output_tokens"] == 20
    assert item["success"] is True
    assert "error_reason" not in item


def test_write_call_log_failure_entry_includes_error_reason(mocked_aws):
    from call_log import write_call_log

    write_call_log(
        user_id="amzn1.ask.account.TESTUSER",
        intent_or_task="GetWeatherIntent",
        input_tokens=0,
        output_tokens=0,
        estimated_cost_usd=0.0,
        latency_ms=50.0,
        success=False,
        error_reason="BedrockInvocationError: fake ARN",
    )

    table = mocked_aws["dynamodb"].Table("SkillCallLog")
    items = table.scan()["Items"]
    assert len(items) == 1
    assert items[0]["success"] is False
    assert "BedrockInvocationError" in items[0]["error_reason"]


def test_multiple_calls_from_same_user_get_distinct_sort_keys(mocked_aws):
    from call_log import write_call_log

    for _ in range(3):
        write_call_log(
            user_id="amzn1.ask.account.TESTUSER",
            intent_or_task="SmartAssistantIntent",
            input_tokens=1,
            output_tokens=1,
            estimated_cost_usd=0.0001,
            latency_ms=10.0,
            success=True,
        )

    table = mocked_aws["dynamodb"].Table("SkillCallLog")
    items = table.scan()["Items"]
    assert len(items) == 3
    timestamps = [item["timestamp"] for item in items]
    assert len(set(timestamps)) == 3, "expected 3 distinct timestamps, got duplicates"
