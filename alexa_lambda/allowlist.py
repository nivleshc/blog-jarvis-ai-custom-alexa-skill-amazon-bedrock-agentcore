"""
Deny-by-default per-user allowlist. Implements solution-design.md Section 4,
step 2, and the SkillUsers table schema from Section 5.1.

This module is intentionally the FIRST real check in the request pipeline
(after the near-zero-cost skill-ID check) -- it must run, and reject
unapproved users, before any rate-limit check or Bedrock/AgentCore call.
"""

import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import boto3
from botocore.exceptions import ClientError

_dynamodb = boto3.resource("dynamodb")


def _table():
    table_name = os.environ["SKILL_USERS_TABLE"]
    return _dynamodb.Table(table_name)


@dataclass
class UserRecord:
    user_id: str
    status: str
    daily_limit_override: Optional[int] = None
    burst_limit_override: Optional[int] = None


def check_allowlist(user_id: str) -> Optional[UserRecord]:
    """
    Look up userId in SkillUsers. Returns a UserRecord if the user is
    approved, or None if they are not found or not approved.

    If the user is not found at all, this also creates a `pending` row
    with first_seen_at set -- see solution-design.md Section 4, step 2:
    "Not found: write a pending row ... and stop."
    """
    table = _table()
    response = table.get_item(Key={"userId": user_id})
    item = response.get("Item")

    if item is None:
        _create_pending_record(table, user_id)
        return None

    if item.get("status") != "approved":
        return None

    return UserRecord(
        user_id=user_id,
        status=item["status"],
        daily_limit_override=item.get("daily_limit_override"),
        burst_limit_override=item.get("burst_limit_override"),
    )


def _create_pending_record(table, user_id: str) -> None:
    """
    Write a pending row for a brand-new userId. Uses a conditional put so
    that a race between two near-simultaneous first requests from the same
    user doesn't overwrite an already-created pending (or since-approved)
    record.
    """
    now = _now_iso()
    try:
        table.put_item(
            Item={
                "userId": user_id,
                "status": "pending",
                "first_seen_at": now,
                "total_calls": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "total_estimated_cost_usd": 0,
            },
            ConditionExpression="attribute_not_exists(userId)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # Someone else's request already created this record between our
        # get_item and this put_item -- fine, nothing to do.


def record_successful_call(
    user_id: str,
    input_tokens: int,
    output_tokens: int,
    estimated_cost_usd: float,
) -> None:
    """
    Update SkillUsers running totals after a successful Bedrock/AgentCore
    call. See solution-design.md Section 5.1's total_calls,
    total_input_tokens, total_output_tokens, total_estimated_cost_usd,
    last_used_at attributes -- all atomic ADD/SET so concurrent requests
    from the same user don't clobber each other.
    """
    table = _table()
    table.update_item(
        Key={"userId": user_id},
        UpdateExpression=(
            "ADD total_calls :one, "
            "total_input_tokens :in_tok, "
            "total_output_tokens :out_tok, "
            "total_estimated_cost_usd :cost "
            "SET last_used_at = :now"
        ),
        ExpressionAttributeValues={
            ":one": 1,
            ":in_tok": input_tokens,
            ":out_tok": output_tokens,
            # DynamoDB's boto3 resource API rejects native Python floats
            # (it requires exact decimal representation) -- must convert
            # via str() first to avoid binary-float precision artifacts,
            # then to Decimal.
            ":cost": Decimal(str(estimated_cost_usd)),
            ":now": _now_iso(),
        },
    )


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
