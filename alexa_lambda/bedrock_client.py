"""
Thin wrapper around the three task-registry backend types this project
supports: the Nova foundation model (via bedrock-runtime.invoke_model)
for free-form Q&A, Bedrock AgentCore Runtime (via
bedrock-agentcore.invoke_agent_runtime) for AgentCore-backed named tasks,
and a plain AWS Lambda function (via lambda.invoke) for named tasks that
are deterministic lookups with no need for an LLM or AgentCore's
containerized-agent machinery. See solution-design.md Section 4, step 5,
Section 7, and Section 11 (task registry / three backend types) for the
model choice, cost-control, and extensibility reasoning this implements.

Request/response shapes below were verified directly against AWS's own
documentation (Nova Invoke API guide, the boto3 bedrock-agentcore client
reference for invoke_agent_runtime, and the boto3 Lambda client reference
for invoke), not assumed.
"""

import json
import os

import boto3

_bedrock_runtime = boto3.client("bedrock-runtime")
_agentcore_runtime = boto3.client("bedrock-agentcore")
_lambda_client = boto3.client("lambda")


class BedrockInvocationError(Exception):
    """Raised when either API call fails or returns an unexpected shape."""


def invoke_foundation_model(prompt_text: str) -> dict:
    """
    Call the configured Nova model via the Invoke API for free-form Q&A
    (the AskBedrock task). Returns a dict with response_text,
    input_tokens, output_tokens -- the actual token counts as reported by
    Bedrock, not an estimate (see solution-design.md Section 7.1's
    distinction between the pre-call character estimate used only for
    the input-length cap, and the real post-call token counts used for
    cost tracking).
    """
    model_id = os.environ["BEDROCK_MODEL_ID"]
    max_tokens = int(os.environ.get("BEDROCK_MAX_TOKENS", 512))

    request_body = {
        "messages": [
            {"role": "user", "content": [{"text": prompt_text}]},
        ],
        "inferenceConfig": {"maxTokens": max_tokens},
    }

    try:
        response = _bedrock_runtime.invoke_model(
            modelId=model_id,
            body=json.dumps(request_body),
        )
        response_body = json.loads(response["body"].read())
    except Exception as exc:  # noqa: BLE001 -- re-raised as our own type
        # so callers (router.py) only need to catch one exception class
        # regardless of which underlying AWS API failed.
        raise BedrockInvocationError(f"invoke_model failed: {exc}") from exc

    content_list = response_body.get("output", {}).get("message", {}).get("content", [])
    text_block = next((item for item in content_list if "text" in item), None)
    response_text = text_block["text"] if text_block else ""

    usage = response_body.get("usage", {})
    input_tokens = usage.get("inputTokens", 0)
    output_tokens = usage.get("outputTokens", 0)

    return {
        "response_text": response_text,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        # AskBedrock is a plain foundation-model call, never produces
        # structured recommendation data -- always empty. See
        # invoke_agentcore_task's docstring for where recommendations
        # actually get populated (Boredom Buster only).
        "recommendations": [],
    }


def invoke_agentcore_task(agent_runtime_arn: str, payload: dict, session_id: str) -> dict:
    """
    Call a Bedrock AgentCore Runtime agent for a named task (weather,
    notes, knowledge search). `session_id` should be stable per Alexa
    user so multi-turn context works if/when the agent supports it --
    the Alexa userId is used for this (see router.py).

    NOTE: a task registry entry whose ARN is still a literal placeholder
    (see task_registry.py's is_task_enabled) will fail this call with a
    real AWS error (ResourceNotFoundException or similar) -- that
    failure is expected for a not-yet-implemented task and handled by
    router.py's error path, not a bug in this function.
    """
    try:
        response = _agentcore_runtime.invoke_agent_runtime(
            agentRuntimeArn=agent_runtime_arn,
            runtimeSessionId=session_id,
            contentType="application/json",
            accept="application/json",
            payload=json.dumps(payload).encode("utf-8"),
        )
        response_bytes = response["response"].read()
        response_body = json.loads(response_bytes)
    except Exception as exc:  # noqa: BLE001
        raise BedrockInvocationError(f"invoke_agent_runtime failed: {exc}") from exc

    # AgentCore Runtime's response body shape is agent-specific (it's
    # whatever the agent's own code returns), unlike invoke_model's fixed
    # schema. This assumes a simple {"response_text": ..., "input_tokens":
    # ..., "output_tokens": ...} convention that any real agent built for
    # this project should follow -- documented here rather than silently
    # assumed, since there is no AWS-mandated schema to point to.
    #
    # "recommendations" is an OPTIONAL addition to that base contract,
    # used only by agents/boredom_buster_agent to carry structured
    # movie/TV data for the Echo Show's APL screens (solution-design.md
    # Section 13) alongside the spoken response. Defaults to an empty
    # list for every other AgentCore agent, which never populates this
    # field and doesn't need to know it exists.
    return {
        "response_text": response_body.get("response_text", ""),
        "input_tokens": response_body.get("input_tokens", 0),
        "output_tokens": response_body.get("output_tokens", 0),
        "recommendations": response_body.get("recommendations", []),
        # True when the agent asked a clarifying question this turn
        # (currently only Boredom Buster sets this) -- see agent.py's
        # response contract docstring and handlers/handler.py's use of
        # this field to bias Alexa's NLU toward capturing the next
        # short reply into the right slot, instead of a repeat-forever
        # loop. Defaults to False for any agent that doesn't set it.
        "needs_clarification": response_body.get("needs_clarification", False),
    }


def invoke_lambda_task(function_arn: str, payload: dict) -> dict:
    """
    Call a plain AWS Lambda function for a named task that is a
    deterministic lookup with no LLM reasoning involved -- e.g.
    `GetWeather`. This backend type exists alongside `foundation_model`
    and `agentcore_task` so tasks that don't need AgentCore-specific
    capabilities can use a simpler, cheaper Lambda function instead of
    routing everything through AgentCore Runtime regardless of whether
    the task actually benefits from it.

    Uses a synchronous ("RequestResponse") invocation since the caller
    (router.py, ultimately handler.py) needs the result before it can
    build an Alexa response -- there is no fire-and-forget path here.

    RESPONSE CONTRACT: the target Lambda function must return the same
    {"response_text", "input_tokens", "output_tokens"} shape as an
    AgentCore agent (see invoke_agentcore_task's docstring) -- this
    keeps router.py's post-invocation handling (cost calculation,
    response building) identical regardless of which of the 3 backend
    types actually served the request.
    """
    try:
        response = _lambda_client.invoke(
            FunctionName=function_arn,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode("utf-8"),
        )
        response_payload = json.loads(response["Payload"].read())
    except Exception as exc:  # noqa: BLE001
        raise BedrockInvocationError(f"lambda invoke failed: {exc}") from exc

    # A Lambda invoke() call can "succeed" at the API level (HTTP 200)
    # while the function itself raised an unhandled exception -- that
    # shows up as a FunctionError field on the response, with the
    # payload containing the exception details rather than our expected
    # response shape. Treat that the same as a transport-level failure,
    # since either way the task didn't produce a usable result.
    if response.get("FunctionError"):
        raise BedrockInvocationError(f"lambda function raised an error: {response_payload}")

    return {
        "response_text": response_payload.get("response_text", ""),
        "input_tokens": response_payload.get("input_tokens", 0),
        "output_tokens": response_payload.get("output_tokens", 0),
        # Lambda-backed tasks (e.g. GetWeather) are deterministic
        # lookups, never produce recommendation data -- always empty.
        "recommendations": [],
        # Optional signal (currently only GetWeather sets this) telling
        # the caller that a required slot value couldn't be resolved
        # (e.g. an unrecognized location) and the user should be asked
        # to re-supply it, rather than the conversation just ending --
        # see lambda_tasks/get_weather/handler.py's docstring. Defaults
        # to False for any task that doesn't set it.
        "needs_clarification": response_payload.get("needs_clarification", False),
    }
