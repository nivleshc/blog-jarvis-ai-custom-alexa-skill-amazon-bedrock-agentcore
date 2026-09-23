"""
Tests for allowlist.py -- the deny-by-default access control from
solution-design.md Section 4, step 2 and Section 5.1.
"""


def test_new_user_is_denied_and_gets_pending_record(mocked_aws):
    from allowlist import check_allowlist

    result = check_allowlist("amzn1.ask.account.NEWUSER")
    assert result is None

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    item = users_table.get_item(Key={"userId": "amzn1.ask.account.NEWUSER"})["Item"]
    assert item["status"] == "pending"
    assert "first_seen_at" in item
    assert item["total_calls"] == 0


def test_pending_user_is_still_denied(mocked_aws):
    from allowlist import check_allowlist

    check_allowlist("amzn1.ask.account.STILLPENDING")  # creates the pending record
    result = check_allowlist("amzn1.ask.account.STILLPENDING")  # second contact, still pending
    assert result is None


def test_approved_user_is_allowed(mocked_aws):
    from allowlist import check_allowlist

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    result = check_allowlist("amzn1.ask.account.APPROVED")
    assert result is not None
    assert result.user_id == "amzn1.ask.account.APPROVED"
    assert result.status == "approved"


def test_revoked_user_is_denied(mocked_aws):
    from allowlist import check_allowlist

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.REVOKED", "status": "revoked", "total_calls": 5})

    result = check_allowlist("amzn1.ask.account.REVOKED")
    assert result is None


def test_approved_user_with_limit_overrides_returned(mocked_aws):
    from allowlist import check_allowlist

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(
        Item={
            "userId": "amzn1.ask.account.CUSTOM",
            "status": "approved",
            "total_calls": 0,
            "daily_limit_override": 50,
            "burst_limit_override": 10,
        }
    )

    result = check_allowlist("amzn1.ask.account.CUSTOM")
    assert result.daily_limit_override == 50
    assert result.burst_limit_override == 10


def test_record_successful_call_updates_running_totals(mocked_aws):
    from allowlist import record_successful_call

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    record_successful_call("amzn1.ask.account.APPROVED", input_tokens=10, output_tokens=20, estimated_cost_usd=0.001234)
    record_successful_call("amzn1.ask.account.APPROVED", input_tokens=5, output_tokens=15, estimated_cost_usd=0.000567)

    item = users_table.get_item(Key={"userId": "amzn1.ask.account.APPROVED"})["Item"]
    assert item["total_calls"] == 2
    assert item["total_input_tokens"] == 15
    assert item["total_output_tokens"] == 35
    assert abs(float(item["total_estimated_cost_usd"]) - 0.001801) < 1e-9
    assert "last_used_at" in item
