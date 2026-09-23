#!/usr/bin/env python3
"""
usage_report.py -- answers the day-to-day cost/usage questions from
solution-design.md Section 8.1's table without hand-writing CloudWatch
Logs Insights queries or opening the console. Queries SkillUsers and
SkillCallLog directly.

Usage:
    python3 usage_report.py --by-day
    python3 usage_report.py --by-user
    python3 usage_report.py --never-used
    python3 usage_report.py --by-day --by-user --never-used   # all sections
"""

import argparse
import os
import sys
from collections import defaultdict
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr


def _resolve_table(name: str, region_override: str = None):
    """
    Shared table-lookup helper with an explicit, visible region check --
    see approve_user.py's _table() for why: a region mismatch between
    where Terraform deployed (variables.tf's aws_region) and this
    script's boto3 default (AWS CLI profile / AWS_REGION env var)
    produces a ResourceNotFoundException that is indistinguishable from
    a wrong table name unless the resolved region is printed explicitly.
    """
    session = boto3.Session(region_name=region_override) if region_override else boto3.Session()
    resolved_region = session.region_name
    print(f"Using DynamoDB table '{name}' in region '{resolved_region}'.", file=sys.stderr)

    dynamodb = session.resource("dynamodb")
    table = dynamodb.Table(name)
    try:
        table.load()
    except dynamodb.meta.client.exceptions.ResourceNotFoundException:
        print(
            f"\nERROR: table '{name}' does not exist in region "
            f"'{resolved_region}'.\n"
            "This is almost always a REGION MISMATCH, not a wrong table "
            "name -- see --region's help text. Pass --region explicitly "
            "to match where you deployed (variables.tf's aws_region, "
            "default us-east-1), e.g. --region us-east-1.",
            file=sys.stderr,
        )
        sys.exit(1)
    return table


def _users_table(override: str = None, region_override: str = None):
    name = override or os.environ.get("SKILL_USERS_TABLE")
    if not name:
        print("ERROR: set SKILL_USERS_TABLE or pass --users-table.", file=sys.stderr)
        sys.exit(1)
    return _resolve_table(name, region_override)


def _call_log_table(override: str = None, region_override: str = None):
    name = override or os.environ.get("SKILL_CALL_LOG_TABLE")
    if not name:
        print("ERROR: set SKILL_CALL_LOG_TABLE or pass --call-log-table.", file=sys.stderr)
        sys.exit(1)
    return _resolve_table(name, region_override)


def _scan_all(table, **kwargs):
    """DynamoDB Scan is paginated; this helper follows LastEvaluatedKey
    until the whole table has been read. Fine for this project's
    beta-scale volume -- see the same trade-off note in approve_user.py's
    list_pending()."""
    items = []
    response = table.scan(**kwargs)
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"], **kwargs)
        items.extend(response.get("Items", []))
    return items


def report_by_day(call_log_table) -> None:
    """Answers: "Which day was most expensive?" -- solution-design.md
    Section 8.1's first row."""
    items = _scan_all(call_log_table)
    cost_by_day = defaultdict(lambda: Decimal("0"))
    calls_by_day = defaultdict(int)

    for item in items:
        day = item.get("timestamp", "")[:10]  # YYYY-MM-DD prefix
        if not day:
            continue
        cost_by_day[day] += item.get("estimated_cost_usd", Decimal("0"))
        calls_by_day[day] += 1

    if not cost_by_day:
        print("No call log entries found.")
        return

    print("\n=== Cost by Day (worst first) ===")
    print(f"{'Date':<12} {'Calls':>8} {'Cost (USD)':>12}")
    print("-" * 34)
    for day, cost in sorted(cost_by_day.items(), key=lambda kv: kv[1], reverse=True):
        print(f"{day:<12} {calls_by_day[day]:>8} {float(cost):>12.6f}")

    total_cost = sum(cost_by_day.values())
    total_calls = sum(calls_by_day.values())
    print("-" * 34)
    print(f"{'TOTAL':<12} {total_calls:>8} {float(total_cost):>12.6f}")


def report_by_user(users_table) -> None:
    """Answers: "How much usage did each user do?", "How many tokens per
    call?", "When did each user register/last use it?" -- solution-
    design.md Section 8.1."""
    items = _scan_all(users_table)

    if not items:
        print("No users found.")
        return

    print("\n=== Per-User Usage ===")
    header = f"{'userId':<38} {'status':<10} {'calls':>6} {'in_tok':>8} {'out_tok':>8} {'cost_usd':>10} {'last_used':<22}"
    print(header)
    print("-" * len(header))

    items.sort(key=lambda i: i.get("total_estimated_cost_usd", Decimal("0")), reverse=True)
    for item in items:
        print(
            f"{item['userId']:<38} "
            f"{item.get('status', 'unknown'):<10} "
            f"{item.get('total_calls', 0):>6} "
            f"{item.get('total_input_tokens', 0):>8} "
            f"{item.get('total_output_tokens', 0):>8} "
            f"{float(item.get('total_estimated_cost_usd', 0)):>10.6f} "
            f"{item.get('last_used_at', 'never'):<22}"
        )


def report_never_used(users_table) -> None:
    """Answers: "Which users haven't used the solution?" -- solution-
    design.md Section 8.1."""
    items = _scan_all(users_table, FilterExpression=Attr("status").eq("approved"))
    never_used = [i for i in items if i.get("total_calls", 0) == 0]

    if not never_used:
        print("\n=== Never Used ===\nAll approved users have used the skill at least once.")
        return

    print("\n=== Approved Users Who Have Never Used the Skill ===")
    for item in never_used:
        approved_at = item.get("approved_at", "unknown")
        print(f"{item['userId']:<40} approved_at={approved_at}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--users-table", help="Override SkillUsers table name (default: $SKILL_USERS_TABLE)")
    parser.add_argument("--call-log-table", help="Override SkillCallLog table name (default: $SKILL_CALL_LOG_TABLE)")
    parser.add_argument(
        "--region",
        help="AWS region the tables live in. Defaults to AWS_REGION/AWS_DEFAULT_REGION or your AWS CLI profile's default region -- NOT the Terraform aws_region variable. Pass this explicitly if your local AWS CLI default region differs from where you deployed.",
    )
    parser.add_argument("--by-day", action="store_true", help="Cost/calls by day, worst first")
    parser.add_argument("--by-user", action="store_true", help="Per-user usage table")
    parser.add_argument("--never-used", action="store_true", help="Approved users who have never made a call")
    args = parser.parse_args()

    if not any([args.by_day, args.by_user, args.never_used]):
        parser.print_help()
        sys.exit(1)

    if args.by_day:
        report_by_day(_call_log_table(args.call_log_table, args.region))
    if args.by_user:
        report_by_user(_users_table(args.users_table, args.region))
    if args.never_used:
        report_never_used(_users_table(args.users_table, args.region))


if __name__ == "__main__":
    main()
