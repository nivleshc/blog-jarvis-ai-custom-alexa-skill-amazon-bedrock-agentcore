"""
Global + per-user rate limiting. Implements solution-design.md Section 6
(limit resolution) and Section 4, step 3 (the check order: global ceiling
-> per-user daily -> per-user burst).

Uses DynamoDB's atomic conditional UpdateItem (ADD with a ConditionExpression)
so concurrent requests from the same user, or from many different users at
once, can never race past a limit -- this is the standard DynamoDB
rate-limiting pattern, not a custom invention.
"""

import os
import time

import boto3
from botocore.exceptions import ClientError

from allowlist import UserRecord

_dynamodb = boto3.resource("dynamodb")

# How far out to set the TTL on counter items, in seconds. Configurable via
# USAGE_COUNTER_TTL_DAYS (see solution-design.md Section 5.2/5.3).
_DEFAULT_TTL_DAYS = 2


class RateLimitExceeded(Exception):
    """Raised with `reason` set to one of: daily_limit, burst_limit,
    global_ceiling -- matches the DenialReason values in
    solution-design.md Section 8.2."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _usage_counters_table():
    return _dynamodb.Table(os.environ["SKILL_USAGE_COUNTERS_TABLE"])


def _global_counters_table():
    return _dynamodb.Table(os.environ["GLOBAL_USAGE_COUNTERS_TABLE"])


def _ttl_epoch() -> int:
    ttl_days = int(os.environ.get("USAGE_COUNTER_TTL_DAYS", _DEFAULT_TTL_DAYS))
    return int(time.time()) + (ttl_days * 86400)


def resolve_daily_limit(user: UserRecord) -> int:
    """See solution-design.md Section 6's resolution formula: per-user
    override wins if set, else the global default applies."""
    if user.daily_limit_override is not None:
        return int(user.daily_limit_override)
    return int(os.environ.get("GLOBAL_DEFAULT_DAILY_LIMIT", 20))


def resolve_burst_limit(user: UserRecord) -> int:
    if user.burst_limit_override is not None:
        return int(user.burst_limit_override)
    return int(os.environ.get("GLOBAL_DEFAULT_BURST_LIMIT", 5))


def _global_daily_ceiling() -> int:
    return int(os.environ.get("GLOBAL_DAILY_CEILING", 200))


def _increment_with_ceiling(table, key: str, limit: int) -> None:
    """
    Atomic ADD request_count :incr, conditional on the pre-increment value
    being below `limit`. Raises botocore's ConditionalCheckFailedException
    (translated by the caller into RateLimitExceeded) if the limit has
    already been reached.

    The condition checks `request_count < :limit` OR the attribute not
    existing yet (first request in this bucket) -- DynamoDB's ADD on a
    nonexistent item/attribute initializes it to the operand, so this is
    safe for the very first request in a new time bucket.
    """
    table.update_item(
        Key={"counter_key": key},
        UpdateExpression="ADD request_count :incr SET #ttl = :ttl",
        ConditionExpression=(
            "attribute_not_exists(request_count) OR request_count < :limit"
        ),
        ExpressionAttributeNames={"#ttl": "ttl"},
        ExpressionAttributeValues={
            ":incr": 1,
            ":limit": limit,
            ":ttl": _ttl_epoch(),
        },
    )


def check_and_increment(user: UserRecord) -> None:
    """
    Runs all three checks in the order specified by
    solution-design.md Section 4, step 3: global ceiling first, then
    per-user daily, then per-user burst. Raises RateLimitExceeded on the
    first one that fails -- later checks are not attempted once an
    earlier one fails.

    NOTE on a trade-off of this ordering: because each check both
    validates AND increments atomically in one step (this is what makes
    the pattern race-free), a request that passes the global check but
    then fails a per-user check still consumes one slot of the global
    ceiling's daily budget, even though it never reaches
    Bedrock/AgentCore. This is an accepted trade-off, not a bug: the
    global ceiling is a safety cap on total request volume hitting this
    check, not an exact tracker of Bedrock spend, which is tracked
    separately and precisely in SkillCallLog (Section 5.4). If this
    becomes a real problem in practice, the fix is to check per-user
    limits first instead.
    """
    now = time.gmtime()
    date_str = time.strftime("%Y-%m-%d", now)
    hour_str = time.strftime("%Y-%m-%dT%H", now)

    global_key = f"GLOBAL#{date_str}"
    daily_key = f"{user.user_id}#{date_str}"
    burst_key = f"{user.user_id}#{hour_str}"

    global_table = _global_counters_table()
    usage_table = _usage_counters_table()

    try:
        _increment_with_ceiling(global_table, global_key, _global_daily_ceiling())
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise RateLimitExceeded("global_ceiling") from e
        raise

    try:
        _increment_with_ceiling(usage_table, daily_key, resolve_daily_limit(user))
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise RateLimitExceeded("daily_limit") from e
        raise

    try:
        _increment_with_ceiling(usage_table, burst_key, resolve_burst_limit(user))
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise RateLimitExceeded("burst_limit") from e
        raise
