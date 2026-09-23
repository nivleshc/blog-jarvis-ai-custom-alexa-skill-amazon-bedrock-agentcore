"""Tests for usage_report.py's core report functions."""

from decimal import Decimal


def test_report_by_day_aggregates_correctly(mocked_ddb, capsys):
    from usage_report import report_by_day, _call_log_table

    table = _call_log_table()
    table.put_item(
        Item={
            "userId": "u1",
            "timestamp": "2026-01-01T10:00:00.000Z#aaa",
            "estimated_cost_usd": Decimal("0.001"),
        }
    )
    table.put_item(
        Item={
            "userId": "u2",
            "timestamp": "2026-01-01T11:00:00.000Z#bbb",
            "estimated_cost_usd": Decimal("0.002"),
        }
    )
    table.put_item(
        Item={
            "userId": "u1",
            "timestamp": "2026-01-02T10:00:00.000Z#ccc",
            "estimated_cost_usd": Decimal("0.0005"),
        }
    )

    report_by_day(table)

    captured = capsys.readouterr()
    assert "2026-01-01" in captured.out
    assert "2026-01-02" in captured.out
    # 2026-01-01 ($0.003 total) should appear before 2026-01-02 ($0.0005),
    # since the report is sorted worst-day-first.
    pos_day1 = captured.out.index("2026-01-01")
    pos_day2 = captured.out.index("2026-01-02")
    assert pos_day1 < pos_day2


def test_report_by_day_with_no_entries(mocked_ddb, capsys):
    from usage_report import report_by_day, _call_log_table

    table = _call_log_table()
    report_by_day(table)

    captured = capsys.readouterr()
    assert "No call log entries" in captured.out


def test_report_by_user_shows_all_users(mocked_ddb, capsys):
    from usage_report import report_by_user, _users_table

    table = _users_table()
    table.put_item(
        Item={
            "userId": "amzn1.ask.account.USER1",
            "status": "approved",
            "total_calls": 5,
            "total_input_tokens": 100,
            "total_output_tokens": 200,
            "total_estimated_cost_usd": Decimal("0.01"),
            "last_used_at": "2026-01-01T00:00:00Z",
        }
    )

    report_by_user(table)

    captured = capsys.readouterr()
    assert "amzn1.ask.account.USER1" in captured.out
    assert "5" in captured.out


def test_report_never_used_finds_approved_zero_call_users(mocked_ddb, capsys):
    from usage_report import report_never_used, _users_table

    table = _users_table()
    table.put_item(Item={"userId": "amzn1.ask.account.UNUSED", "status": "approved", "total_calls": 0, "approved_at": "2026-01-01T00:00:00Z"})
    table.put_item(Item={"userId": "amzn1.ask.account.USED", "status": "approved", "total_calls": 5})
    table.put_item(Item={"userId": "amzn1.ask.account.PENDING", "status": "pending", "total_calls": 0})

    report_never_used(table)

    captured = capsys.readouterr()
    assert "amzn1.ask.account.UNUSED" in captured.out
    assert "amzn1.ask.account.USED" not in captured.out
    assert "amzn1.ask.account.PENDING" not in captured.out  # not approved, out of scope for this report


def test_report_never_used_with_all_users_active(mocked_ddb, capsys):
    from usage_report import report_never_used, _users_table

    table = _users_table()
    table.put_item(Item={"userId": "amzn1.ask.account.USED", "status": "approved", "total_calls": 5})

    report_never_used(table)

    captured = capsys.readouterr()
    assert "have used the skill at least once" in captured.out
