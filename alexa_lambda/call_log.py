"""
SkillCallLog writer. Implements solution-design.md Section 5.4 -- one item
per completed call (success or failure), used as the source of truth for
exact per-call token/cost/latency answers (see Section 8.1's table mapping
questions to data sources).
"""

import os
import time
import uuid
from decimal import Decimal
from typing import Optional

import boto3

_dynamodb = boto3.resource("dynamodb")


def _table():
    return _dynamodb.Table(os.environ["SKILL_CALL_LOG_TABLE"])


def write_call_log(
    user_id: str,
    intent_or_task: str,
    input_tokens: int,
    output_tokens: int,
    estimated_cost_usd: float,
    latency_ms: float,
    success: bool,
    error_reason: Optional[str] = None,
) -> None:
    """
    Write one SkillCallLog item. Called from the pipeline's step 6
    (solution-design.md Section 4) regardless of whether step 5 (the
    Bedrock/AgentCore call) succeeded or raised -- callers are expected to
    wrap step 5 in a try/except and call this in both branches so failures
    are visible in the same table as successes.
    """
    item = {
        "userId": user_id,
        "timestamp": _now_iso(),
        "intent_or_task": intent_or_task,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": Decimal(str(estimated_cost_usd)),
        "latency_ms": Decimal(str(latency_ms)),
        "success": success,
    }
    if error_reason is not None:
        item["error_reason"] = error_reason

    ttl_days = int(os.environ.get("CALL_LOG_TTL_DAYS", 0))
    if ttl_days > 0:
        item["ttl"] = int(time.time()) + (ttl_days * 86400)

    _table().put_item(Item=item)


def _now_iso() -> str:
    """
    Includes fractional seconds AND a short random suffix. The fractional
    seconds alone are NOT sufficient to guarantee a unique sort key --
    millisecond resolution collides easily under rapid successive calls
    (confirmed while writing this project's test suite: 5 calls in a
    tight loop produced only 2 distinct millisecond values). Since
    userId+timestamp is this table's full primary key, a collision means
    DynamoDB silently overwrites one call's log entry with another's,
    rather than erroring -- a real, silent data-loss bug, not just a
    sorting nicety. The random suffix makes collisions astronomically
    unlikely while keeping the timestamp prefix meaningful for the
    time-range queries and by-day reports described in solution-design.md
    Section 5.4/8.1.
    """
    now = time.time()
    millis = int(now * 1000) % 1000
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)) + f".{millis:03d}Z"
    return f"{timestamp}#{uuid.uuid4().hex[:8]}"
