"""
Task registry loader with S3 hot-reload. Implements solution-design.md
Section 11: maps Alexa intent names to either the default foundation
model path or a specific AgentCore Runtime agent, without requiring a
Lambda redeploy when a task is added or an agent ARN changes.

Hot-reload mechanism: the registry is cached in memory for the lifetime
of the Lambda execution environment (which AWS may reuse across multiple
invocations -- a "warm" Lambda), with a short TTL. Once the TTL expires,
the next invocation re-fetches from S3. This avoids fetching from S3 on
every single request (which would be wasteful) while still picking up
changes within a few minutes without a redeploy.
"""

import json
import os
import time

import boto3

_s3 = boto3.client("s3")

_CACHE_TTL_SECONDS = 300  # 5 minutes
_cache = {"data": None, "fetched_at": 0.0}


def get_task_registry() -> dict:
    """
    Returns the current task registry dict, keyed by intent name. Fetches
    from S3 on first call and whenever the cache has expired; otherwise
    returns the cached copy.
    """
    now = time.time()
    if _cache["data"] is not None and (now - _cache["fetched_at"]) < _CACHE_TTL_SECONDS:
        return _cache["data"]

    bucket = os.environ["TASK_REGISTRY_BUCKET"]
    key = os.environ["TASK_REGISTRY_KEY"]

    response = _s3.get_object(Bucket=bucket, Key=key)
    data = json.loads(response["Body"].read())

    # Drop the JSON file's own documentation comment field -- it's not a
    # task entry and callers shouldn't need to know to skip it.
    data.pop("_comment", None)

    _cache["data"] = data
    _cache["fetched_at"] = now
    return data


def get_task(intent_name: str) -> dict:
    """
    Look up a single task's config by intent name. Raises KeyError if the
    intent has no registry entry -- callers (router.py) are expected to
    handle this as "unknown intent", not a routing bug.
    """
    registry = get_task_registry()
    return registry[intent_name]


def is_task_enabled(task_config: dict) -> bool:
    """
    Returns False for a task entry that still points at a literal
    placeholder ARN (e.g. "PLACEHOLDER-notes-agent") -- i.e. the entry
    exists in the registry but has no real agent/function behind it
    yet. Used by utils/response.py's dynamic launch message so the
    skill never advertises a capability that would fail if invoked.

    Checks whichever ARN/ARN-shaped field the task's backend type uses
    (agent_runtime_arn for agentcore_task, lambda_function_arn for
    lambda) -- foundation_model tasks have neither field and are always
    considered enabled, since they need no external resource at all.
    """
    arn_value = task_config.get("agent_runtime_arn") or task_config.get("lambda_function_arn") or ""
    return "PLACEHOLDER" not in arn_value
