"""
Tests for rate_limit.py -- the global/per-user limit hierarchy and
resolution logic from solution-design.md Section 4, step 3 and Section 6.
"""

import pytest


def _user(**overrides):
    from allowlist import UserRecord

    defaults = {"user_id": "amzn1.ask.account.TESTUSER", "status": "approved"}
    defaults.update(overrides)
    return UserRecord(**defaults)


def test_resolve_daily_limit_uses_global_default_when_no_override(mocked_aws):
    from rate_limit import resolve_daily_limit

    user = _user()
    assert resolve_daily_limit(user) == 20  # from GLOBAL_DEFAULT_DAILY_LIMIT in conftest


def test_resolve_daily_limit_uses_override_when_set(mocked_aws):
    from rate_limit import resolve_daily_limit

    user = _user(daily_limit_override=99)
    assert resolve_daily_limit(user) == 99


def test_resolve_burst_limit_uses_global_default_when_no_override(mocked_aws):
    from rate_limit import resolve_burst_limit

    user = _user()
    assert resolve_burst_limit(user) == 5  # from GLOBAL_DEFAULT_BURST_LIMIT in conftest


def test_resolve_burst_limit_uses_override_when_set(mocked_aws):
    from rate_limit import resolve_burst_limit

    user = _user(burst_limit_override=1)
    assert resolve_burst_limit(user) == 1


def test_burst_limit_enforced_after_limit_requests(mocked_aws, monkeypatch):
    monkeypatch.setenv("GLOBAL_DEFAULT_BURST_LIMIT", "3")
    import rate_limit
    import importlib
    importlib.reload(rate_limit)  # pick up the new env var value

    user = _user()
    rate_limit.check_and_increment(user)
    rate_limit.check_and_increment(user)
    rate_limit.check_and_increment(user)

    with pytest.raises(rate_limit.RateLimitExceeded) as exc_info:
        rate_limit.check_and_increment(user)
    assert exc_info.value.reason == "burst_limit"


def test_daily_limit_checked_after_burst_limit_would_pass(mocked_aws, monkeypatch):
    """A user with a very low daily limit but a high burst limit should
    be stopped by the daily check, not slip through."""
    monkeypatch.setenv("GLOBAL_DEFAULT_BURST_LIMIT", "1000")
    import rate_limit
    import importlib
    importlib.reload(rate_limit)

    user = _user(daily_limit_override=2)
    rate_limit.check_and_increment(user)
    rate_limit.check_and_increment(user)

    with pytest.raises(rate_limit.RateLimitExceeded) as exc_info:
        rate_limit.check_and_increment(user)
    assert exc_info.value.reason == "daily_limit"


def test_global_ceiling_enforced_across_multiple_users(mocked_aws, monkeypatch):
    monkeypatch.setenv("GLOBAL_DAILY_CEILING", "2")
    monkeypatch.setenv("GLOBAL_DEFAULT_DAILY_LIMIT", "1000")
    monkeypatch.setenv("GLOBAL_DEFAULT_BURST_LIMIT", "1000")
    import rate_limit
    import importlib
    importlib.reload(rate_limit)

    user_a = _user(user_id="amzn1.ask.account.USERA")
    user_b = _user(user_id="amzn1.ask.account.USERB")

    rate_limit.check_and_increment(user_a)  # global count: 1
    rate_limit.check_and_increment(user_b)  # global count: 2, at ceiling

    with pytest.raises(rate_limit.RateLimitExceeded) as exc_info:
        rate_limit.check_and_increment(user_a)  # would be 3rd, over ceiling
    assert exc_info.value.reason == "global_ceiling"
