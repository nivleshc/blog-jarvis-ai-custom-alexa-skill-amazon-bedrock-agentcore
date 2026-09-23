"""
Tests for bedrock_client.py. Mocks the underlying boto3 clients directly
(rather than moto, which does not emulate bedrock-runtime/bedrock-agentcore
model inference) -- fixture response shapes mirror AWS's documented
response formats, not invented shapes.
"""

import io
import json
from unittest.mock import patch

import pytest


class _FakeStreamingBody:
    def __init__(self, data: bytes):
        self._stream = io.BytesIO(data)

    def read(self):
        return self._stream.read()


def test_invoke_foundation_model_parses_response_correctly(mocked_aws):
    from bedrock_client import invoke_foundation_model

    fake_response_body = json.dumps(
        {
            "output": {"message": {"role": "assistant", "content": [{"text": "Hello there."}]}},
            "usage": {"inputTokens": 4, "outputTokens": 3, "totalTokens": 7},
        }
    ).encode("utf-8")

    with patch("bedrock_client._bedrock_runtime") as mock_client:
        mock_client.invoke_model.return_value = {"body": _FakeStreamingBody(fake_response_body)}
        result = invoke_foundation_model("Say hello")

    assert result["response_text"] == "Hello there."
    assert result["input_tokens"] == 4
    assert result["output_tokens"] == 3

    call_kwargs = mock_client.invoke_model.call_args.kwargs
    assert call_kwargs["modelId"] == "amazon.nova-lite-v1:0"
    request_body = json.loads(call_kwargs["body"])
    assert request_body["messages"][0]["content"][0]["text"] == "Say hello"
    assert request_body["inferenceConfig"]["maxTokens"] == 512


def test_invoke_foundation_model_wraps_errors(mocked_aws):
    from bedrock_client import BedrockInvocationError, invoke_foundation_model

    with patch("bedrock_client._bedrock_runtime") as mock_client:
        mock_client.invoke_model.side_effect = Exception("ThrottlingException")
        with pytest.raises(BedrockInvocationError):
            invoke_foundation_model("test")


def test_invoke_foundation_model_handles_missing_text_block(mocked_aws):
    """A response with no text content block should return an empty
    string, not raise -- defends against a malformed/unexpected model
    response rather than crashing the pipeline."""
    from bedrock_client import invoke_foundation_model

    fake_response_body = json.dumps(
        {"output": {"message": {"role": "assistant", "content": []}}, "usage": {"inputTokens": 1, "outputTokens": 0}}
    ).encode("utf-8")

    with patch("bedrock_client._bedrock_runtime") as mock_client:
        mock_client.invoke_model.return_value = {"body": _FakeStreamingBody(fake_response_body)}
        result = invoke_foundation_model("test")

    assert result["response_text"] == ""


def test_invoke_agentcore_task_parses_response_correctly(mocked_aws):
    from bedrock_client import invoke_agentcore_task

    fake_response_body = json.dumps(
        {"response_text": "It's sunny.", "input_tokens": 10, "output_tokens": 5}
    ).encode("utf-8")

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.return_value = {"response": _FakeStreamingBody(fake_response_body)}
        result = invoke_agentcore_task(
            agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/weather",
            payload={"input": "Sydney"},
            session_id="amzn1.ask.account.TESTUSER",
        )

    assert result["response_text"] == "It's sunny."
    assert result["input_tokens"] == 10
    assert result["output_tokens"] == 5

    call_kwargs = mock_client.invoke_agent_runtime.call_args.kwargs
    assert call_kwargs["agentRuntimeArn"] == "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/weather"
    assert call_kwargs["runtimeSessionId"] == "amzn1.ask.account.TESTUSER"


def test_invoke_agentcore_task_wraps_errors(mocked_aws):
    """Confirms a failed invoke_agent_runtime call (e.g. an unresolvable
    ARN) is wrapped as BedrockInvocationError rather than propagating the
    raw boto3 exception."""
    from bedrock_client import BedrockInvocationError, invoke_agentcore_task

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.side_effect = Exception("ResourceNotFoundException")
        with pytest.raises(BedrockInvocationError):
            invoke_agentcore_task(
                agent_runtime_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/PLACEHOLDER",
                payload={"input": "test"},
                session_id="amzn1.ask.account.TESTUSER",
            )


def test_invoke_lambda_task_parses_response_correctly(mocked_aws):
    """The 'lambda' backend type -- GetWeather routes through this
    function instead of AgentCore."""
    from bedrock_client import invoke_lambda_task

    fake_payload = json.dumps(
        {"response_text": "It's sunny.", "input_tokens": 0, "output_tokens": 0}
    ).encode("utf-8")

    with patch("bedrock_client._lambda_client") as mock_client:
        mock_client.invoke.return_value = {"Payload": _FakeStreamingBody(fake_payload), "StatusCode": 200}
        result = invoke_lambda_task(
            function_arn="arn:aws:lambda:us-east-1:123456789012:function:get-weather",
            payload={"input": "Sydney", "user_id": "amzn1.ask.account.TESTUSER"},
        )

    assert result["response_text"] == "It's sunny."
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0

    call_kwargs = mock_client.invoke.call_args.kwargs
    assert call_kwargs["FunctionName"] == "arn:aws:lambda:us-east-1:123456789012:function:get-weather"
    assert call_kwargs["InvocationType"] == "RequestResponse"
    sent_payload = json.loads(call_kwargs["Payload"])
    assert sent_payload["input"] == "Sydney"


def test_invoke_lambda_task_wraps_transport_errors(mocked_aws):
    from bedrock_client import BedrockInvocationError, invoke_lambda_task

    with patch("bedrock_client._lambda_client") as mock_client:
        mock_client.invoke.side_effect = Exception("ResourceNotFoundException")
        with pytest.raises(BedrockInvocationError):
            invoke_lambda_task(
                function_arn="arn:aws:lambda:us-east-1:123456789012:function:PLACEHOLDER",
                payload={"input": "test"},
            )


def test_invoke_lambda_task_wraps_function_errors(mocked_aws):
    """A Lambda invoke() call can return HTTP 200 while the function
    itself raised -- surfaced via the FunctionError field, not an
    exception from boto3. Confirms this is still treated as a failure."""
    from bedrock_client import BedrockInvocationError, invoke_lambda_task

    fake_payload = json.dumps({"errorMessage": "something broke"}).encode("utf-8")

    with patch("bedrock_client._lambda_client") as mock_client:
        mock_client.invoke.return_value = {
            "Payload": _FakeStreamingBody(fake_payload),
            "StatusCode": 200,
            "FunctionError": "Unhandled",
        }
        with pytest.raises(BedrockInvocationError):
            invoke_lambda_task(
                function_arn="arn:aws:lambda:us-east-1:123456789012:function:get-weather",
                payload={"input": "test"},
            )
