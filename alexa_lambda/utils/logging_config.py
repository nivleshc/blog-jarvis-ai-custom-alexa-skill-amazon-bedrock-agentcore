"""
Structured logging setup, including CloudWatch Embedded Metric Format
(EMF) emission. See solution-design.md Section 8.2 for the metric names,
dimensions, and DenialReason values this implements.

EMF works by writing a specially-shaped JSON blob to stdout (which Lambda
routes to CloudWatch Logs); CloudWatch automatically extracts the declared
metrics from that JSON into real, graphable CloudWatch Metrics under the
given namespace -- no separate PutMetricData API calls, no extra service
cost beyond normal Lambda logging.
"""

import json
import logging
import os
import time
from typing import Optional

logger = logging.getLogger("bedrock_assistant")
logger.setLevel(logging.INFO)


def emit_call_metric(
    user_id: str,
    intent_or_task: str,
    input_tokens: int,
    output_tokens: int,
    estimated_cost_usd: float,
    latency_ms: float,
) -> None:
    """
    Emit an EMF log line for a completed (successful) Bedrock/AgentCore
    call. See solution-design.md Section 8.2 -- dimensions are userId and
    intent_or_task, metrics are InputTokens/OutputTokens/EstimatedCostUSD/
    LatencyMs, plus a RequestsAllowed count of 1 for the dashboard's
    allowed-vs-denied panel (Section 8.3).
    """
    namespace = os.environ.get("EMF_NAMESPACE", "echo-show-bedrock-assistant")

    emf_payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [["userId", "intent_or_task"]],
                    "Metrics": [
                        {"Name": "InputTokens", "Unit": "Count"},
                        {"Name": "OutputTokens", "Unit": "Count"},
                        {"Name": "EstimatedCostUSD", "Unit": "None"},
                        {"Name": "LatencyMs", "Unit": "Milliseconds"},
                        {"Name": "RequestsAllowed", "Unit": "Count"},
                    ],
                }
            ],
        },
        "userId": user_id,
        "intent_or_task": intent_or_task,
        "InputTokens": input_tokens,
        "OutputTokens": output_tokens,
        "EstimatedCostUSD": estimated_cost_usd,
        "LatencyMs": latency_ms,
        "RequestsAllowed": 1,
    }
    print(json.dumps(emf_payload))


def emit_denial_metric(user_id: str, denial_reason: str) -> None:
    """
    Emit an EMF log line for a denied request. `denial_reason` must be one
    of: not_approved, daily_limit, burst_limit, global_ceiling,
    input_too_long -- see solution-design.md Section 8.2.
    """
    namespace = os.environ.get("EMF_NAMESPACE", "echo-show-bedrock-assistant")

    emf_payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": namespace,
                    "Dimensions": [["userId", "DenialReason"]],
                    "Metrics": [
                        {"Name": "RequestsDenied", "Unit": "Count"},
                    ],
                }
            ],
        },
        "userId": user_id,
        "DenialReason": denial_reason,
        "RequestsDenied": 1,
    }
    print(json.dumps(emf_payload))


def log_error(message: str, exc: Optional[Exception] = None) -> None:
    if exc is not None:
        logger.exception(message)
    else:
        logger.error(message)


def log_outgoing_response(event: dict, response: dict) -> None:
    """
    Logs a complete, structured record of the response sent back to
    Alexa for a given request -- spoken text, whether an APL directive
    was included (and its token), shouldEndSession, session attribute
    keys, and the same requestId log_incoming_request logged for this
    same request, so the two lines can be correlated in CloudWatch Logs
    Insights (e.g. `filter requestId = "..."`) to see the full in/out
    picture of one invocation without cross-referencing DynamoDB.

    Why this exists: log_incoming_request (added earlier) covers what
    Alexa sent in, but nothing logged what the skill sent back --
    troubleshooting a bad response (wrong slot handling, an unexpected
    speech string, a missing APL directive) required either reproducing
    the request or reading through response-building code, rather than
    just reading the log. This closes that gap the same way
    log_incoming_request did for the inbound side.

    Session attribute VALUES are deliberately not logged in full --
    Boredom Buster's `boredom_buster_recommendations` attribute can hold
    several full recommendation dicts (poster URLs, overviews, etc.),
    which would bloat every log line for a value already visible in the
    speech/directive fields that matter for troubleshooting. Only the
    attribute *keys* and, for the recommendations list specifically, a
    count, are logged -- enough to confirm state is being carried
    correctly across turns without duplicating the full payload.

    Deliberately defensive, matching log_incoming_request: must never
    raise, since logging must never be the reason a response fails to
    return.
    """
    try:
        request = event.get("request", {})
        resp_body = response.get("response", {})
        output_speech = resp_body.get("outputSpeech", {}) or {}
        directives = resp_body.get("directives", []) or []
        session_attrs = response.get("sessionAttributes", {}) or {}

        directive_summary = [d.get("type") for d in directives]
        apl_token = next((d.get("token") for d in directives if d.get("type") == "Alexa.Presentation.APL.RenderDocument"), None)

        session_attr_summary = dict.fromkeys(session_attrs.keys())
        if "boredom_buster_recommendations" in session_attrs:
            session_attr_summary["boredom_buster_recommendations"] = f"<{len(session_attrs['boredom_buster_recommendations'])} item(s)>"
        if "boredom_buster_current_index" in session_attrs:
            session_attr_summary["boredom_buster_current_index"] = session_attrs["boredom_buster_current_index"]

        logger.info(
            "Outgoing response: requestId=%s speechText=%s shouldEndSession=%s "
            "directives=%s aplToken=%s sessionAttributes=%s",
            request.get("requestId"),
            output_speech.get("text"),
            resp_body.get("shouldEndSession"),
            directive_summary,
            apl_token,
            json.dumps(session_attr_summary),
        )
    except Exception:  # noqa: BLE001 -- see log_incoming_request's docstring
        # for why this must never raise regardless of response shape.
        logger.exception("Failed to log outgoing response (unexpected response shape)")


def log_incoming_request(event: dict) -> None:
    """
    Logs a complete, structured record of every single incoming Alexa
    request -- request type, intent name, every slot with its raw value
    and confirmation status, userId, requestId, locale -- as the very
    first thing lambda_handler does, before ANY pipeline check (skill-ID,
    allowlist, rate limit, routing) runs.

    Why this exists and why it runs first: a real production issue
    showed that CloudWatch Logs only ever captured denials and unhandled
    exceptions -- there was no way to see exactly what Alexa actually
    sent (which intent, which slot values) for a request that succeeded,
    or one that failed partway through routing, without cross-referencing
    SkillCallLog in DynamoDB or asking the user to repeat what they said.
    Logging the full request unconditionally, before any check can
    short-circuit the function, means every single invocation --
    successful, denied, or crashed -- leaves a complete, self-contained
    audit trail in CloudWatch Logs on its own.

    Deliberately defensive: event shapes can be malformed (see
    test_missing_user_id_handled_gracefully and similar edge cases), so
    this never raises -- a malformed event still gets logged with
    whatever fields are actually present, using .get() throughout, and
    any unexpected shape is caught and logged as a fallback rather than
    crashing lambda_handler before its own try/except even starts.
    """
    try:
        request = event.get("request", {})
        intent = request.get("intent", {})
        slots = intent.get("slots", {}) or {}

        slot_summary = {
            name: {
                "value": slot.get("value"),
                "confirmationStatus": slot.get("confirmationStatus"),
            }
            for name, slot in slots.items()
        }

        logger.info(
            "Incoming request: requestId=%s type=%s intent=%s slots=%s "
            "userId=%s locale=%s applicationId=%s",
            request.get("requestId"),
            request.get("type"),
            intent.get("name"),
            json.dumps(slot_summary),
            event.get("session", {}).get("user", {}).get("userId"),
            request.get("locale"),
            event.get("context", {}).get("System", {}).get("application", {}).get("applicationId"),
        )
    except Exception:  # noqa: BLE001 -- logging must never be the reason
        # a request fails; if the event is shaped unexpectedly, log that
        # fact and move on rather than raising out of a logging helper.
        logger.exception("Failed to log incoming request (unexpected event shape)")
