"""Tests for approve_user.py's core functions, called directly rather
than via subprocess -- exercises the same code the CLI entry point calls."""



def test_approve_user_sets_status_and_timestamp(mocked_ddb):
    from approve_user import approve_user, _table

    table = _table()
    table.put_item(Item={"userId": "amzn1.ask.account.PENDING1", "status": "pending", "first_seen_at": "x"})

    approve_user(table, "amzn1.ask.account.PENDING1")

    item = table.get_item(Key={"userId": "amzn1.ask.account.PENDING1"})["Item"]
    assert item["status"] == "approved"
    assert "approved_at" in item


def test_approve_user_with_limit_overrides(mocked_ddb):
    from approve_user import approve_user, _table

    table = _table()
    table.put_item(Item={"userId": "amzn1.ask.account.PENDING2", "status": "pending"})

    approve_user(table, "amzn1.ask.account.PENDING2", daily_limit=50, burst_limit=10)

    item = table.get_item(Key={"userId": "amzn1.ask.account.PENDING2"})["Item"]
    assert item["daily_limit_override"] == 50
    assert item["burst_limit_override"] == 10


def test_approve_nonexistent_user_raises(mocked_ddb):
    from approve_user import approve_user, _table

    table = _table()
    try:
        approve_user(table, "amzn1.ask.account.NEVEREXISTED")
        assert False, "expected ConditionalCheckFailedException"
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        pass


def test_revoke_user_sets_status_to_revoked(mocked_ddb):
    from approve_user import revoke_user, _table

    table = _table()
    table.put_item(Item={"userId": "amzn1.ask.account.APPROVED1", "status": "approved"})

    revoke_user(table, "amzn1.ask.account.APPROVED1")

    item = table.get_item(Key={"userId": "amzn1.ask.account.APPROVED1"})["Item"]
    assert item["status"] == "revoked"


def test_list_pending_finds_only_pending_users(mocked_ddb, capsys):
    from approve_user import list_pending, _table

    table = _table()
    table.put_item(Item={"userId": "amzn1.ask.account.PEND1", "status": "pending", "first_seen_at": "2026-01-01T00:00:00Z"})
    table.put_item(Item={"userId": "amzn1.ask.account.APPR1", "status": "approved", "first_seen_at": "2026-01-01T00:00:00Z"})

    list_pending(table)

    captured = capsys.readouterr()
    assert "amzn1.ask.account.PEND1" in captured.out
    assert "amzn1.ask.account.APPR1" not in captured.out


def test_list_pending_with_no_pending_users(mocked_ddb, capsys):
    from approve_user import list_pending, _table

    table = _table()
    list_pending(table)

    captured = capsys.readouterr()
    assert "No pending users" in captured.out
