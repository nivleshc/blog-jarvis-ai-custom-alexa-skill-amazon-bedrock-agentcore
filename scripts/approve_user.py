#!/usr/bin/env python3
"""
approve_user.py -- the manual gate the account owner operates for the
deny-by-default allowlist. Implements solution-design.md Section 8.6 and
the staggered beta rollout workflow in Section 10.

Reads the SkillUsers table name from the SKILL_USERS_TABLE environment
variable (matching the same variable name the Lambda uses), or accept
--table as an override for one-off use against a different environment.

Usage:
    python3 approve_user.py --list-pending
    python3 approve_user.py --approve <userId>
    python3 approve_user.py --revoke <userId>
    python3 approve_user.py --approve <userId> --set-daily-limit 50
    python3 approve_user.py --approve <userId> --set-burst-limit 10
"""

import argparse
import os
import sys
import time

import boto3
from boto3.dynamodb.conditions import Attr


def _table(table_name_override: str = None, region_override: str = None):
    table_name = table_name_override or os.environ.get("SKILL_USERS_TABLE")
    if not table_name:
        print(
            "ERROR: no table name given. Set SKILL_USERS_TABLE or pass --table.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Resolve the region the SAME way boto3 will, so what's printed here is
    # actually what gets used -- not just an env var that boto3 might
    # override with a profile default. This is deliberately printed to
    # stderr on every run (not just on error) -- a silent region mismatch
    # between where Terraform deployed the tables (variables.tf's
    # aws_region, default us-east-1) and this script's boto3 default
    # (whatever ~/.aws/config or AWS_REGION/AWS_DEFAULT_REGION resolves
    # to) produces a ResourceNotFoundException that gives no hint about
    # WHY the table can't be found -- it looks identical to a wrong table
    # name. Printing the resolved region up front turns that into an
    # immediately visible mismatch instead of a guessing game.
    session = boto3.Session(region_name=region_override) if region_override else boto3.Session()
    resolved_region = session.region_name
    print(f"Using DynamoDB table '{table_name}' in region '{resolved_region}'.", file=sys.stderr)

    dynamodb = session.resource("dynamodb")
    table = dynamodb.Table(table_name)
    try:
        table.load()
    except dynamodb.meta.client.exceptions.ResourceNotFoundException:
        print(
            f"\nERROR: table '{table_name}' does not exist in region "
            f"'{resolved_region}'.\n"
            "This is almost always a REGION MISMATCH, not a wrong table "
            "name: this script's AWS region comes from --region, then "
            "AWS_REGION/AWS_DEFAULT_REGION, then your AWS CLI profile's "
            "default region (~/.aws/config) -- NOT from the aws_region "
            "Terraform variable. If you deployed with a non-default "
            "aws_region (variables.tf defaults to us-east-1) and your "
            "local AWS CLI profile defaults to a different region, this "
            "script silently looks in the wrong place.\n"
            "Fix: pass --region explicitly, matching the region you "
            "deployed into, e.g.:\n"
            f"  python3 {sys.argv[0]} --region us-east-1 --list-pending\n"
            "or check what region Terraform actually deployed into with:\n"
            "  terraform -chdir=terraform output -raw skill_users_table_name\n"
            "  (then cross-check against your terraform.tfvars aws_region, "
            "or the default in variables.tf if unset)",
            file=sys.stderr,
        )
        sys.exit(1)
    return table


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def list_pending(table) -> None:
    """
    Scan for status=pending users. A Scan is used deliberately here (not
    a Query) -- SkillUsers has no secondary index on status, and this
    command is an infrequent, manual, low-volume operation, so the cost
    and latency of a table scan is a reasonable trade-off against adding
    an index that would only serve this one operational script. See
    solution-design.md Section 10 for the staggered-rollout workflow this
    supports.
    """
    response = table.scan(FilterExpression=Attr("status").eq("pending"))
    items = response.get("Items", [])

    if not items:
        print("No pending users.")
        return

    items.sort(key=lambda i: i.get("first_seen_at", ""))
    print(f"{'userId':<40} {'first_seen_at':<25}")
    print("-" * 65)
    for item in items:
        print(f"{item['userId']:<40} {item.get('first_seen_at', 'unknown'):<25}")


def approve_user(table, user_id: str, daily_limit: int = None, burst_limit: int = None) -> None:
    update_expr_parts = ["#status = :approved", "approved_at = :now"]
    expr_values = {":approved": "approved", ":now": _now_iso()}
    expr_names = {"#status": "status"}

    if daily_limit is not None:
        update_expr_parts.append("daily_limit_override = :dl")
        expr_values[":dl"] = daily_limit
    if burst_limit is not None:
        update_expr_parts.append("burst_limit_override = :bl")
        expr_values[":bl"] = burst_limit

    table.update_item(
        Key={"userId": user_id},
        UpdateExpression="SET " + ", ".join(update_expr_parts),
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
        ConditionExpression="attribute_exists(userId)",
    )
    print(f"Approved {user_id}" + (f" (daily_limit={daily_limit})" if daily_limit else "") + (
        f" (burst_limit={burst_limit})" if burst_limit else ""
    ))


def revoke_user(table, user_id: str) -> None:
    table.update_item(
        Key={"userId": user_id},
        UpdateExpression="SET #status = :revoked",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":revoked": "revoked"},
        ConditionExpression="attribute_exists(userId)",
    )
    print(f"Revoked {user_id}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--table", help="Override the SkillUsers table name (default: $SKILL_USERS_TABLE)")
    parser.add_argument(
        "--region",
        help="AWS region the table lives in. Defaults to AWS_REGION/AWS_DEFAULT_REGION or your AWS CLI profile's default region -- NOT the Terraform aws_region variable. Pass this explicitly if your local AWS CLI default region differs from where you deployed.",
    )
    parser.add_argument("--list-pending", action="store_true", help="List users awaiting approval")
    parser.add_argument("--approve", metavar="USER_ID", help="Approve a userId")
    parser.add_argument("--revoke", metavar="USER_ID", help="Revoke a previously-approved userId")
    parser.add_argument("--set-daily-limit", type=int, metavar="N", help="Set a per-user daily limit override (use with --approve)")
    parser.add_argument("--set-burst-limit", type=int, metavar="N", help="Set a per-user burst limit override (use with --approve)")
    args = parser.parse_args()

    if not any([args.list_pending, args.approve, args.revoke]):
        parser.print_help()
        sys.exit(1)

    table = _table(args.table, args.region)

    try:
        if args.list_pending:
            list_pending(table)
        if args.approve:
            approve_user(table, args.approve, args.set_daily_limit, args.set_burst_limit)
        if args.revoke:
            revoke_user(table, args.revoke)
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        print("ERROR: userId not found in the table. Has this user ever contacted the skill?", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
