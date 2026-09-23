"""
Tests for router.py -- intent routing to foundation model or AgentCore
task, solution-design.md Section 4 step 5 and Section 11.
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


def test_ask_bedrock_routes_to_foundation_model(mocked_aws):
    from router import route_request

    fake_response_body = json.dumps(
        {
            "output": {"message": {"role": "assistant", "content": [{"text": "42."}]}},
            "usage": {"inputTokens": 6, "outputTokens": 1},
        }
    ).encode("utf-8")

    with patch("bedrock_client._bedrock_runtime") as mock_client:
        mock_client.invoke_model.return_value = {"body": _FakeStreamingBody(fake_response_body)}
        result = route_request(
            intent_name="SmartAssistantIntent",
            slots={"Query": {"name": "Query", "value": "what is the answer"}},
            user_id="amzn1.ask.account.TESTUSER",
        )

    assert result.response_text == "42."
    assert result.input_tokens == 6
    assert result.output_tokens == 1
    assert result.estimated_cost_usd > 0


def test_agentcore_task_failure_propagates_as_bedrock_invocation_error(mocked_aws):
    """Confirms a failed AgentCore Runtime call (e.g. a bad ARN, an
    unreachable agent) surfaces cleanly as BedrockInvocationError rather
    than an unhandled exception type."""
    from bedrock_client import BedrockInvocationError
    from router import route_request

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.side_effect = Exception("ResourceNotFoundException")
        with pytest.raises(BedrockInvocationError):
            route_request(
                intent_name="BoredomBusterIntent",
                slots={"MoodOrGenre": {"name": "MoodOrGenre", "value": "something funny"}},
                user_id="amzn1.ask.account.TESTUSER",
            )


def test_lambda_task_routes_to_lambda_invoke(mocked_aws):
    """GetWeather routes via the 'lambda' backend type, not AgentCore --
    confirms router.py calls bedrock_client.invoke_lambda_task() with
    the registry's lambda_function_arn and returns its response shape
    correctly."""
    from router import route_request

    fake_payload = json.dumps(
        {"response_text": "It's sunny in Sydney.", "input_tokens": 0, "output_tokens": 0}
    ).encode("utf-8")

    with patch("bedrock_client._lambda_client") as mock_client:
        mock_client.invoke.return_value = {
            "Payload": _FakeStreamingBody(fake_payload),
            "StatusCode": 200,
        }
        result = route_request(
            intent_name="GetWeatherIntent",
            slots={"Location": {"name": "Location", "value": "Sydney"}},
            user_id="amzn1.ask.account.TESTUSER",
        )

    assert result.response_text == "It's sunny in Sydney."
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.estimated_cost_usd == 0.0

    call_kwargs = mock_client.invoke.call_args.kwargs
    assert call_kwargs["FunctionName"] == "arn:aws:lambda:us-east-1:123456789012:function:FAKE"
    sent_payload = json.loads(call_kwargs["Payload"])
    assert sent_payload["input"] == "Sydney"
    assert sent_payload["user_id"] == "amzn1.ask.account.TESTUSER"


def test_lambda_task_failure_propagates_as_bedrock_invocation_error(mocked_aws):
    from bedrock_client import BedrockInvocationError
    from router import route_request

    with patch("bedrock_client._lambda_client") as mock_client:
        mock_client.invoke.side_effect = Exception("ResourceNotFoundException")
        with pytest.raises(BedrockInvocationError):
            route_request(
                intent_name="GetWeatherIntent",
                slots={"Location": {"name": "Location", "value": "Sydney"}},
                user_id="amzn1.ask.account.TESTUSER",
            )


def test_unknown_intent_returns_graceful_fallback(mocked_aws):
    from router import route_request

    result = route_request(intent_name="SomeUnmappedIntent", slots={}, user_id="amzn1.ask.account.TESTUSER")
    assert "don't know how to do that" in result.response_text
    assert result.estimated_cost_usd == 0.0


def test_intent_mapped_to_missing_registry_task_handled_gracefully(mocked_aws, monkeypatch):
    """
    Simulates an intent that IS mapped in _INTENT_TO_TASK_KEY but whose
    task key is missing from the registry -- e.g. a hot-reload that
    removed a task after the interaction model/router mapping was
    already deployed. Monkeypatches the mapping directly (rather than
    relying on a real, currently-unmapped intent name) so this test
    stays meaningful regardless of which intents happen to be mapped at
    any given time -- TakeNoteIntent/SearchKnowledgeBaseIntent, this
    test's original scenario, were removed entirely (see
    task_registry.json.tpl's _comment) since they never had a real
    agent behind them.
    """
    import router

    monkeypatch.setitem(router._INTENT_TO_TASK_KEY, "SomeIntentIntent", "SomeTaskKeyNotInRegistry")

    from router import route_request

    result = route_request(intent_name="SomeIntentIntent", slots={}, user_id="amzn1.ask.account.TESTUSER")
    assert "isn't available" in result.response_text
    assert result.estimated_cost_usd == 0.0


# --------------------------------------------------------------------------
# build_query_text / resolve_media_type -- BoredomBusterIntent's two-slot
# combination. "recommend a movie" fills MediaType but not MoodOrGenre
# (no free text left over after the carrier phrase), so both slots need
# to reach the agent together rather than the media type being dropped.
# --------------------------------------------------------------------------


def _media_type_slot(value: str, resolved_id: str = None):
    slot = {"name": "MediaType", "value": value}
    if resolved_id:
        slot["resolutions"] = {
            "resolutionsPerAuthority": [
                {
                    "status": {"code": "ER_SUCCESS_MATCH"},
                    "values": [{"value": {"id": resolved_id, "name": value}}],
                }
            ]
        }
    return slot


def test_build_query_text_media_type_only():
    """"recommend a movie" fills NO AMAZON.SearchQuery slot -- there's no
    free text after the carrier phrase. When MoodOrGenre is empty but
    MediaType is filled (e.g. user tapped "Movie Ideas" card), we return
    empty string so the agent's empty-input fallback triggers and asks for
    mood. The media_type is passed separately in the AgentCore payload."""
    from router import build_query_text

    slots = {"MediaType": _media_type_slot("movie", "MOVIE"), "MoodOrGenre": {"name": "MoodOrGenre"}}

    assert build_query_text("BoredomBusterIntent", slots) == ""


def test_build_query_text_resolves_media_type_synonyms():
    from router import build_query_text

    slots = {"MediaType": _media_type_slot("series", "TV"), "MoodOrGenre": {"name": "MoodOrGenre"}}

    # When MoodOrGenre is empty, query is empty so agent asks for mood
    assert build_query_text("BoredomBusterIntent", slots) == ""


def test_build_query_text_combines_mood_and_media_type():
    """The old _first_slot_value returned only the FIRST non-empty slot,
    so once both could be filled one was silently thrown away."""
    from router import build_query_text

    slots = {
        "MoodOrGenre": {"name": "MoodOrGenre", "value": "funny"},
        "MediaType": _media_type_slot("tv show", "TV"),
    }

    assert build_query_text("BoredomBusterIntent", slots) == "funny tv"


def test_build_query_text_avoids_duplicating_media_word_already_in_mood():
    """AMAZON.SearchQuery greedily captures the trailing noun, so
    "recommend a funny movie" can fill MoodOrGenre with "funny movie" --
    appending MediaType on top would send "funny movie movie"."""
    from router import build_query_text

    slots = {
        "MoodOrGenre": {"name": "MoodOrGenre", "value": "funny movie"},
        "MediaType": _media_type_slot("movie", "MOVIE"),
    }

    assert build_query_text("BoredomBusterIntent", slots) == "funny movie"


def test_build_query_text_mood_only():
    from router import build_query_text

    slots = {"MoodOrGenre": {"name": "MoodOrGenre", "value": "something scary"}, "MediaType": {"name": "MediaType"}}

    assert build_query_text("BoredomBusterIntent", slots) == "something scary"


def test_build_query_text_no_slots_filled():
    from router import build_query_text

    assert build_query_text("BoredomBusterIntent", {}) == ""


def test_build_query_text_other_intents_use_first_slot_value():
    from router import build_query_text

    assert build_query_text("GetWeatherIntent", {"Location": {"name": "Location", "value": "Sydney"}}) == "Sydney"
    assert build_query_text("SmartAssistantIntent", {"Query": {"name": "Query", "value": "why is the sky blue"}}) == "why is the sky blue"


def test_resolve_media_type_falls_back_to_raw_value_without_resolutions():
    """Covers a slot filled with a synonym the slot type doesn't list, or
    a request shape with no `resolutions` block at all."""
    from router import resolve_media_type

    assert resolve_media_type({"name": "MediaType", "value": "Film"}) == "movie"
    assert resolve_media_type({"name": "MediaType", "value": "TV series"}) == "tv"
    assert resolve_media_type({"name": "MediaType", "value": "podcast"}) == ""
    assert resolve_media_type({}) == ""


def test_resolve_media_type_ignores_failed_entity_resolution():
    from router import resolve_media_type

    slot = {
        "name": "MediaType",
        "value": "documentary",
        "resolutions": {"resolutionsPerAuthority": [{"status": {"code": "ER_SUCCESS_NO_MATCH"}, "values": []}]},
    }

    assert resolve_media_type(slot) == ""
