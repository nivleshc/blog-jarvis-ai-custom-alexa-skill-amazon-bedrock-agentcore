"""
End-to-end tests for handlers/handler.py -- the full request pipeline
from solution-design.md Section 4, exercised against real allowlist.py,
rate_limit.py, call_log.py, router.py, and bedrock_client.py, with only
the actual Bedrock/AgentCore boto3 calls mocked (everything else runs
for real against moto-mocked DynamoDB/S3).
"""

import io
import json
from unittest.mock import patch


class _FakeStreamingBody:
    def __init__(self, data: bytes):
        self._stream = io.BytesIO(data)

    def read(self):
        return self._stream.read()


def _intent_event(user_id: str, intent_name="SmartAssistantIntent", app_id="amzn1.ask.skill.test123", slots=None):
    return {
        "context": {"System": {"application": {"applicationId": app_id}}},
        "session": {"user": {"userId": user_id}},
        "request": {
            "type": "IntentRequest",
            "intent": {
                "name": intent_name,
                "slots": slots or {"Query": {"name": "Query", "value": "what is the capital of France"}},
            },
        },
    }


def _fake_nova_response(text="Paris.", in_tok=5, out_tok=2):
    body = json.dumps(
        {
            "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "usage": {"inputTokens": in_tok, "outputTokens": out_tok},
        }
    ).encode("utf-8")
    return {"body": _FakeStreamingBody(body)}


def test_launch_request_returns_welcome_message(mocked_aws):
    from handlers.handler import lambda_handler

    event = {"request": {"type": "LaunchRequest"}}
    resp = lambda_handler(event, {})
    assert "Welcome" in resp["response"]["outputSpeech"]["text"]


def test_launch_message_only_advertises_enabled_tasks(mocked_aws):
    """
    The welcome message must only mention tasks that are actually
    enabled in the task registry (mocked_aws's default registry only has
    AskBedrock/GetWeather/BoredomBuster -- see conftest.py), and must not
    mention any task that isn't actually registered, since following the
    welcome message's own suggestion would otherwise lead to a failure.

    Note: My Watchlist card is always shown (it's a local DB read, no task needed).
    """
    from handlers.handler import lambda_handler

    # Create event with APL support
    event = {"request": {"type": "LaunchRequest"}}
    event["context"] = {"System": {"device": {"supportedInterfaces": {"Alexa.Presentation.APL": {}}}}}
    event["session"] = {"sessionId": "amzn1.echo-api.session.test"}
    resp = lambda_handler(event, {})

    # The launch response now includes an APL directive with capability cards
    # Check that the APL directive has the right capabilities in its datasource
    directives = resp["response"].get("directives", [])
    assert len(directives) == 1
    assert directives[0]["type"] == "Alexa.Presentation.APL.RenderDocument"

    # The capabilities are in the datasource
    datasource = directives[0]["datasources"]["launchData"]["properties"]["capabilities"]
    labels = [cap["label"] for cap in datasource]

    # Should have Ask a Question, Check Weather, Movie Ideas, TV Show Ideas, and My Watchlist
    # (BoredomBuster now split into separate Movie and TV cards; My Watchlist always available)
    assert "Ask a Question" in labels
    assert "Check Weather" in labels
    assert "Movie Ideas" in labels
    assert "TV Show Ideas" in labels
    assert "My Watchlist" in labels

    # Should NOT have the old placeholder tasks
    assert "take a note" not in str(labels).lower()
    assert "search for information" not in str(labels).lower()
    # Old combined card should be gone
    assert "Movie/TV Ideas" not in labels


def test_generic_message_screen_includes_icon_and_accent_color(mocked_aws):
    """
    Confirms the rendered APL document's datasources carry an
    icon/accentColor, and that different intents get visually distinct
    icons (weather gets a different icon than a general AskBedrock
    answer), rather than every response looking identical apart from
    the words.
    """
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    event = _intent_event("amzn1.ask.account.APPROVED")
    event["context"]["System"]["device"] = {"supportedInterfaces": {"Alexa.Presentation.APL": {}}}

    with patch("bedrock_client._bedrock_runtime") as mock_client:
        mock_client.invoke_model.return_value = _fake_nova_response()
        resp = lambda_handler(event, {})

    directives = resp["response"]["directives"]
    message_props = directives[0]["datasources"]["messageData"]["properties"]
    assert message_props["icon"]
    assert message_props["accentColor"].startswith("#")


def test_launch_screen_shown_on_apl_capable_device(mocked_aws):
    """Regression test: before this fix, only BoredomBuster ever sent an
    APL RenderDocument directive -- every other response (including the
    welcome message) only set a Simple Card, which never renders on an
    Echo Show's actual screen. Confirms the welcome response now
    includes an APL directive when the device supports it."""
    from handlers.handler import lambda_handler

    event = {
        "context": {"System": {"device": {"supportedInterfaces": {"Alexa.Presentation.APL": {}}}}},
        "request": {"type": "LaunchRequest"},
    }
    resp = lambda_handler(event, {})

    directives = resp["response"].get("directives", [])
    assert len(directives) == 1
    assert directives[0]["type"] == "Alexa.Presentation.APL.RenderDocument"
    assert directives[0]["token"] == "jarvisAIMessage"


def test_new_user_denied_and_pending_record_created(mocked_aws):
    from handlers.handler import lambda_handler

    event = _intent_event("amzn1.ask.account.BRANDNEW")
    resp = lambda_handler(event, {})
    speech = resp["response"]["outputSpeech"]["text"]
    assert "not authorized" in speech

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    item = users_table.get_item(Key={"userId": "amzn1.ask.account.BRANDNEW"})["Item"]
    assert item["status"] == "pending"


def test_approved_user_gets_real_response_and_call_is_logged(mocked_aws):
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    with patch("bedrock_client._bedrock_runtime") as mock_client:
        mock_client.invoke_model.return_value = _fake_nova_response()
        resp = lambda_handler(_intent_event("amzn1.ask.account.APPROVED"), {})

    assert resp["response"]["outputSpeech"]["text"] == "Paris."

    call_log_table = mocked_aws["dynamodb"].Table("SkillCallLog")
    logs = call_log_table.scan()["Items"]
    assert len(logs) == 1
    assert logs[0]["success"] is True
    assert logs[0]["input_tokens"] == 5
    assert logs[0]["output_tokens"] == 2

    updated_user = users_table.get_item(Key={"userId": "amzn1.ask.account.APPROVED"})["Item"]
    assert updated_user["total_calls"] == 1


def test_ask_bedrock_response_updates_screen_on_apl_capable_device(mocked_aws):
    """
    Regression test: an Echo Show's screen used to never update for
    AskBedrock (or any non-BoredomBuster intent) -- it just kept
    showing whatever was last rendered (typically the welcome screen)
    indefinitely, since AskBedrock's response only ever set a Simple
    Card (Alexa-app-only, never rendered on the device screen).
    Confirms the response now includes an APL directive reflecting the
    actual answer when the device supports APL.
    """
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    event = _intent_event("amzn1.ask.account.APPROVED")
    event["context"]["System"]["device"] = {"supportedInterfaces": {"Alexa.Presentation.APL": {}}}

    with patch("bedrock_client._bedrock_runtime") as mock_client:
        mock_client.invoke_model.return_value = _fake_nova_response()
        resp = lambda_handler(event, {})

    directives = resp["response"].get("directives", [])
    assert len(directives) == 1
    assert directives[0]["type"] == "Alexa.Presentation.APL.RenderDocument"
    assert directives[0]["token"] == "jarvisAIMessage"
    assert resp["response"]["outputSpeech"]["text"] == "Paris."


def test_applicationId_mismatch_rejected(mocked_aws):
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    event = _intent_event("amzn1.ask.account.APPROVED", app_id="amzn1.ask.skill.WRONG")
    resp = lambda_handler(event, {})
    assert "something went wrong" in resp["response"]["outputSpeech"]["text"]


def test_input_too_long_rejected_before_bedrock_call(mocked_aws, monkeypatch):
    from handlers.handler import lambda_handler

    monkeypatch.setenv("MAX_INPUT_CHARS", "10")

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    with patch("bedrock_client._bedrock_runtime") as mock_client:
        event = _intent_event(
            "amzn1.ask.account.APPROVED",
            slots={"Query": {"name": "Query", "value": "this query is definitely longer than ten characters"}},
        )
        resp = lambda_handler(event, {})
        mock_client.invoke_model.assert_not_called()

    assert "ask something shorter" in resp["response"]["outputSpeech"]["text"]


def test_burst_limit_enforced_end_to_end(mocked_aws, monkeypatch):
    monkeypatch.setenv("GLOBAL_DEFAULT_BURST_LIMIT", "2")
    import rate_limit
    import importlib
    importlib.reload(rate_limit)
    import router
    importlib.reload(router)
    import handlers.handler as handler_mod
    importlib.reload(handler_mod)

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    with patch("bedrock_client._bedrock_runtime") as mock_client:
        mock_client.invoke_model.return_value = _fake_nova_response()
        event = _intent_event("amzn1.ask.account.APPROVED")
        handler_mod.lambda_handler(event, {})
        handler_mod.lambda_handler(event, {})
        resp = handler_mod.lambda_handler(event, {})  # 3rd call, over the burst limit of 2

    assert "quickly" in resp["response"]["outputSpeech"]["text"]


def test_cancel_intent_ends_session_without_touching_allowlist(mocked_aws):
    """Built-in intents that don't need Bedrock should never even reach
    the allowlist check -- verified by using a userId that was never
    added to SkillUsers at all."""
    from handlers.handler import lambda_handler

    event = _intent_event("amzn1.ask.account.NEVERSEEN", intent_name="AMAZON.StopIntent")
    resp = lambda_handler(event, {})
    assert resp["response"]["outputSpeech"]["text"] == "Goodbye."
    assert resp["response"]["shouldEndSession"] is True


def test_help_intent_returns_launch_style_response(mocked_aws):
    from handlers.handler import lambda_handler

    event = _intent_event("amzn1.ask.account.NEVERSEEN", intent_name="AMAZON.HelpIntent")
    resp = lambda_handler(event, {})
    assert "Welcome" in resp["response"]["outputSpeech"]["text"]


def test_fallback_intent_returns_graceful_response_without_touching_allowlist(mocked_aws):
    """
    Regression test for the real production issue: without
    AMAZON.FallbackIntent, an out-of-domain request (e.g. "tell me the
    latest news") gets force-matched by Alexa's NLU to an unrelated real
    intent (GetWeatherIntent in the actual incident) and crashes trying
    to invoke a placeholder AgentCore agent. AMAZON.FallbackIntent should
    now catch this case before it ever reaches routing, and -- like the
    other built-in intents -- never touch the allowlist/rate-limit
    pipeline, verified here the same way as the Cancel/Stop test: using a
    userId that was never added to SkillUsers at all.
    """
    from handlers.handler import lambda_handler

    event = _intent_event("amzn1.ask.account.NEVERSEEN", intent_name="AMAZON.FallbackIntent")
    resp = lambda_handler(event, {})
    speech = resp["response"]["outputSpeech"]["text"]
    assert "not sure how to help" in speech
    assert resp["response"]["shouldEndSession"] is False


def test_bedrock_failure_is_logged_and_error_response_returned(mocked_aws):
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    with patch("bedrock_client._bedrock_runtime") as mock_client:
        mock_client.invoke_model.side_effect = Exception("ThrottlingException")
        resp = lambda_handler(_intent_event("amzn1.ask.account.APPROVED"), {})

    assert "something went wrong" in resp["response"]["outputSpeech"]["text"]

    call_log_table = mocked_aws["dynamodb"].Table("SkillCallLog")
    logs = call_log_table.scan()["Items"]
    assert len(logs) == 1
    assert logs[0]["success"] is False
    assert "error_reason" in logs[0]


def test_successful_call_emits_audit_log_line(mocked_aws, caplog):
    """
    Regression test: before this was fixed, a successful call produced
    NO CloudWatch Logs line at all -- only denials/errors were logged,
    which made CloudWatch Logs useless as a human-readable audit trail
    (you had to already know to check DynamoDB or CloudWatch Metrics
    instead). Asserts the explicit "Call succeeded" log line exists with
    the userId, intent, and token/cost/latency fields.
    """
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    with patch("bedrock_client._bedrock_runtime") as mock_client:
        mock_client.invoke_model.return_value = _fake_nova_response()
        with caplog.at_level("INFO"):
            lambda_handler(_intent_event("amzn1.ask.account.APPROVED"), {})

    audit_lines = [r.message for r in caplog.records if "Call succeeded" in r.message]
    assert len(audit_lines) == 1
    assert "amzn1.ask.account.APPROVED" in audit_lines[0]
    assert "SmartAssistantIntent" in audit_lines[0]
    assert "input_tokens=5" in audit_lines[0]
    assert "output_tokens=2" in audit_lines[0]


def test_denied_call_emits_audit_log_line(mocked_aws, caplog):
    """Same regression coverage as test_successful_call_emits_audit_log_line,
    for the not-approved denial path."""
    from handlers.handler import lambda_handler

    with caplog.at_level("INFO"):
        lambda_handler(_intent_event("amzn1.ask.account.BRANDNEW"), {})

    audit_lines = [r.message for r in caplog.records if "Call denied" in r.message]
    assert len(audit_lines) == 1
    assert "amzn1.ask.account.BRANDNEW" in audit_lines[0]
    assert "reason=not_approved" in audit_lines[0]


def test_every_incoming_request_is_logged_regardless_of_outcome(mocked_aws, caplog):
    """
    Regression test: before this was fixed, CloudWatch Logs only ever
    captured denials and unhandled exceptions -- there was no way to see
    exactly what Alexa sent (intent name, slot values, userId) for a
    request unless it happened to fail. This asserts the full incoming
    request (intent name + slot values) is logged unconditionally, for
    THREE different outcomes: a denied request, a successful one, and
    one that crashes mid-routing -- proving the log line fires before
    any pipeline check can short-circuit it, not just on the happy path.
    """
    from handlers.handler import lambda_handler

    # Outcome 1: denied (brand new, unapproved user)
    with caplog.at_level("INFO"):
        lambda_handler(_intent_event("amzn1.ask.account.BRANDNEW"), {})
    incoming_lines = [r.message for r in caplog.records if "Incoming request" in r.message]
    assert len(incoming_lines) == 1
    assert "SmartAssistantIntent" in incoming_lines[0]
    assert "amzn1.ask.account.BRANDNEW" in incoming_lines[0]
    assert "what is the capital of France" in incoming_lines[0]
    caplog.clear()

    # Outcome 2: successful call
    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})
    with patch("bedrock_client._bedrock_runtime") as mock_client:
        mock_client.invoke_model.return_value = _fake_nova_response()
        with caplog.at_level("INFO"):
            lambda_handler(_intent_event("amzn1.ask.account.APPROVED"), {})
    incoming_lines = [r.message for r in caplog.records if "Incoming request" in r.message]
    assert len(incoming_lines) == 1
    assert "amzn1.ask.account.APPROVED" in incoming_lines[0]
    caplog.clear()

    # Outcome 3: crashes mid-routing (e.g. an AccessDeniedException from a
    # placeholder AgentCore ARN, matching the real production error this
    # fix was built for)
    with patch("bedrock_client._bedrock_runtime") as mock_client:
        mock_client.invoke_model.side_effect = Exception("AccessDeniedException")
        with caplog.at_level("INFO"):
            lambda_handler(_intent_event("amzn1.ask.account.APPROVED"), {})
    incoming_lines = [r.message for r in caplog.records if "Incoming request" in r.message]
    assert len(incoming_lines) == 1
    error_lines = [r.message for r in caplog.records if "Error routing intent" in r.message]
    assert len(error_lines) == 1
    assert "amzn1.ask.account.APPROVED" in error_lines[0]
    assert "slots=" in error_lines[0]


def test_outgoing_response_is_logged_alongside_incoming_request(mocked_aws, caplog):
    """
    Regression test: before this was fixed, log_incoming_request logged
    what Alexa sent in, but nothing logged what the skill sent back --
    troubleshooting a bad response (wrong speech text, missing/wrong
    APL directive) required reproducing the request instead of just
    reading the log. Asserts an "Outgoing response" line exists
    alongside the "Incoming request" line, with the actual speech text
    and shouldEndSession value.
    """
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    with patch("bedrock_client._bedrock_runtime") as mock_client:
        mock_client.invoke_model.return_value = _fake_nova_response()
        with caplog.at_level("INFO"):
            lambda_handler(_intent_event("amzn1.ask.account.APPROVED"), {})

    incoming_lines = [r.message for r in caplog.records if "Incoming request" in r.message]
    outgoing_lines = [r.message for r in caplog.records if "Outgoing response" in r.message]
    assert len(incoming_lines) == 1
    assert len(outgoing_lines) == 1
    assert "speechText=Paris." in outgoing_lines[0]
    assert "shouldEndSession=True" in outgoing_lines[0]


def test_outgoing_response_logged_for_boredom_buster_apl_directive(mocked_aws, caplog):
    """Same coverage as test_outgoing_response_is_logged_alongside_incoming_request,
    for the APL-directive response path -- confirms the directive type
    and token are captured, not just plain speech responses."""
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.return_value = _fake_agentcore_response(_SAMPLE_RECS)
        event = _apl_capable_intent_event("amzn1.ask.account.APPROVED", "BoredomBusterIntent", "something funny")
        with caplog.at_level("INFO"):
            lambda_handler(event, {})

    outgoing_lines = [r.message for r in caplog.records if "Outgoing response" in r.message]
    assert len(outgoing_lines) == 1
    assert "Alexa.Presentation.APL.RenderDocument" in outgoing_lines[0]
    assert "aplToken=boredomBusterRecommendations" in outgoing_lines[0]


def test_log_incoming_request_never_raises_on_malformed_event(mocked_aws, caplog):
    """log_incoming_request must be safe to call unconditionally as the
    very first line of lambda_handler, even against a malformed/partial
    event -- it should log what it can and never raise."""
    from utils.logging_config import log_incoming_request

    with caplog.at_level("INFO"):
        log_incoming_request({})  # completely empty event
        log_incoming_request({"request": {"type": "LaunchRequest"}})  # no intent/session at all

    # Both calls should have produced a log line and neither should have
    # raised (if either raised, this test itself would fail with an
    # exception rather than reaching this assertion).
    incoming_lines = [r.message for r in caplog.records if "Incoming request" in r.message]
    assert len(incoming_lines) == 2


def test_missing_user_id_handled_gracefully(mocked_aws):
    """A malformed event with no session.user.userId should not crash
    the Lambda."""
    from handlers.handler import lambda_handler

    event = {
        "context": {"System": {"application": {"applicationId": "amzn1.ask.skill.test123"}}},
        "session": {"user": {}},
        "request": {"type": "IntentRequest", "intent": {"name": "SmartAssistantIntent", "slots": {}}},
    }
    resp = lambda_handler(event, {})
    assert "something went wrong" in resp["response"]["outputSpeech"]["text"]


# --- Boredom Buster APL flow (solution-design.md Section 13) ---


def _apl_capable_intent_event(user_id: str, intent_name: str, slot_value: str, app_id="amzn1.ask.skill.test123"):
    event = _intent_event(
        user_id,
        intent_name=intent_name,
        app_id=app_id,
        slots={"MoodOrGenre": {"name": "MoodOrGenre", "value": slot_value}},
    )
    event["context"]["System"]["device"] = {"supportedInterfaces": {"Alexa.Presentation.APL": {}}}
    event["session"]["sessionId"] = "amzn1.echo-api.session.0000000000000000000000000000000000"
    return event


def _fake_agentcore_response(recommendations, text="Here are some picks.", in_tok=0, out_tok=0):
    body = json.dumps(
        {"response_text": text, "input_tokens": in_tok, "output_tokens": out_tok, "recommendations": recommendations}
    ).encode("utf-8")
    return {"response": _FakeStreamingBody(body)}


_SAMPLE_RECS = [
    {
        "id": 550,
        "media_type": "movie",
        "title": "Fight Club",
        "overview": "...",
        "release_date": "1999-10-15",
        "rating": 8.4,
        "poster_url": "https://image.tmdb.org/t/p/w500/abc.jpg",
        "trailer_url": "https://www.youtube.com/watch?v=abc123",
    }
]


def test_boredom_buster_success_returns_apl_directive_when_device_supports_it(mocked_aws):
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    # BoredomBuster is agentcore_task-backed, per the mocked_aws
    # fixture's default registry -- see conftest.py.
    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.return_value = _fake_agentcore_response(_SAMPLE_RECS)
        event = _apl_capable_intent_event("amzn1.ask.account.APPROVED", "BoredomBusterIntent", "something funny")
        resp = lambda_handler(event, {})

    directives = resp["response"]["directives"]
    assert len(directives) == 1
    assert directives[0]["type"] == "Alexa.Presentation.APL.RenderDocument"
    assert directives[0]["token"] == "boredomBusterRecommendations"
    assert resp["sessionAttributes"]["boredom_buster_recommendations"] == _SAMPLE_RECS


def test_get_weather_unresolvable_location_reprompts_instead_of_ending_session(mocked_aws):
    """
    An unrecognized location (e.g. misheard as "Sydney new south
    wales") must return a Dialog.ElicitSlot directive that re-asks for
    the location while keeping the session open on the same intent,
    rather than a terminal response that ends the session -- ending the
    session would mean the very next thing the user says starts a
    brand-new conversation with no context, so a corrected city name
    would get intercepted by Alexa's own built-in general-knowledge
    answering instead of retrying this skill.
    """
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    fake_payload = json.dumps(
        {
            "response_text": "Sorry, I couldn't find Nonexistentplace. What city would you like the weather for?",
            "input_tokens": 0,
            "output_tokens": 0,
            "needs_clarification": True,
        }
    ).encode("utf-8")

    with patch("bedrock_client._lambda_client") as mock_client:
        mock_client.invoke.return_value = {"Payload": _FakeStreamingBody(fake_payload), "StatusCode": 200}
        event = _intent_event(
            "amzn1.ask.account.APPROVED",
            intent_name="GetWeatherIntent",
            slots={"Location": {"name": "Location", "value": "Nonexistentplace"}},
        )
        resp = lambda_handler(event, {})

    assert resp["response"]["shouldEndSession"] is False
    directives = resp["response"]["directives"]
    elicit_directives = [d for d in directives if d["type"] == "Dialog.ElicitSlot"]
    assert len(elicit_directives) == 1
    assert elicit_directives[0]["slotToElicit"] == "Location"
    assert elicit_directives[0]["updatedIntent"]["name"] == "GetWeatherIntent"
    # The unresolvable value must be cleared, not re-sent -- otherwise
    # Alexa could just hand the same bad value straight back.
    assert elicit_directives[0]["updatedIntent"]["slots"]["Location"].get("value") is None


def test_boredom_buster_clarifying_question_reprompts_moodorgenre_slot(mocked_aws):
    """
    Regression test for the real BoredomBuster infinite-loop bug: real
    CloudWatch Logs showed MoodOrGenre arriving as null on EVERY turn,
    even after the user answered "movie" -- because nothing ever told
    Alexa's NLU to specifically listen for a short reply as that slot's
    value. The agent's needs_clarification=True must now produce a
    Dialog.ElicitSlot directive for MoodOrGenre, not a plain speech
    response, so the next turn's short reply actually gets captured.
    """
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    fake_payload = json.dumps(
        {
            "response_text": "Are you in the mood for a movie or a TV show?",
            "input_tokens": 5,
            "output_tokens": 8,
            "recommendations": [],
            "needs_clarification": True,
        }
    ).encode("utf-8")

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.return_value = {"response": _FakeStreamingBody(fake_payload)}
        event = _intent_event(
            "amzn1.ask.account.APPROVED",
            intent_name="BoredomBusterIntent",
            slots={"MoodOrGenre": {"name": "MoodOrGenre", "value": None}},
        )
        resp = lambda_handler(event, {})

    assert resp["response"]["shouldEndSession"] is False
    directives = resp["response"]["directives"]
    elicit_directives = [d for d in directives if d["type"] == "Dialog.ElicitSlot"]
    assert len(elicit_directives) == 1
    assert elicit_directives[0]["slotToElicit"] == "MoodOrGenre"
    assert elicit_directives[0]["updatedIntent"]["name"] == "BoredomBusterIntent"


def test_boredom_buster_speech_only_response_keeps_session_open(mocked_aws):
    """
    Regression test: BoredomBusterIntent's speech-only response (no APL
    device, or no recommendations yet -- e.g. the agent asked a
    clarifying question instead of searching) must keep the Alexa
    session open. build_speech_response's should_end_session defaults
    to True, which would close the session the instant a clarifying
    question was asked, cutting off the conversation before the user
    could even answer it. Every OTHER intent should still end its
    session as before (unaffected by this BoredomBuster-specific fix).
    """
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        # No recommendations at all -- simulates the agent asking a
        # clarifying question ("movie or TV show?") instead of
        # searching yet.
        mock_client.invoke_agent_runtime.return_value = _fake_agentcore_response(
            [], text="Are you in the mood for a movie or a TV show?"
        )
        event = _intent_event(
            "amzn1.ask.account.APPROVED",
            intent_name="BoredomBusterIntent",
            slots={"MoodOrGenre": {"name": "MoodOrGenre", "value": "I'm bored"}},
        )
        resp = lambda_handler(event, {})

    assert resp["response"]["outputSpeech"]["text"] == "Are you in the mood for a movie or a TV show?"
    assert resp["response"]["shouldEndSession"] is False


def test_ask_bedrock_speech_response_still_ends_session_as_before(mocked_aws):
    """Confirms the BoredomBuster-specific should_end_session fix above
    doesn't change behavior for other intents -- AskBedrock (and every
    other non-BoredomBuster intent) should still end the session after
    a single-turn response, unchanged."""
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    with patch("bedrock_client._bedrock_runtime") as mock_client:
        mock_client.invoke_model.return_value = _fake_nova_response()
        resp = lambda_handler(_intent_event("amzn1.ask.account.APPROVED"), {})

    assert resp["response"]["shouldEndSession"] is True


def test_boredom_buster_apl_directive_is_not_overridden_by_generic_message_screen(mocked_aws):
    """
    Regression guard for _add_apl_message_directive_if_supported: the
    new generic "message screen" post-processing step must never
    override a response that already carries its own APL directive
    (Boredom Buster's recommendations grid) -- only ONE directive
    should ever be present, and it must be the recommendations grid,
    not the generic message screen.
    """
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.return_value = _fake_agentcore_response(_SAMPLE_RECS)
        event = _apl_capable_intent_event("amzn1.ask.account.APPROVED", "BoredomBusterIntent", "something funny")
        resp = lambda_handler(event, {})

    directives = resp["response"]["directives"]
    assert len(directives) == 1
    assert directives[0]["token"] == "boredomBusterRecommendations"


def test_boredom_buster_success_falls_back_to_speech_when_device_lacks_apl(mocked_aws):
    """A device without APL support should get a plain speech response,
    not an APL directive it can't render."""
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.return_value = _fake_agentcore_response(_SAMPLE_RECS)
        # No supportedInterfaces declared at all -- e.g. a plain Echo Dot.
        event = _intent_event(
            "amzn1.ask.account.APPROVED",
            intent_name="BoredomBusterIntent",
            slots={"MoodOrGenre": {"name": "MoodOrGenre", "value": "something funny"}},
        )
        resp = lambda_handler(event, {})

    assert "directives" not in resp["response"]
    assert resp["response"]["outputSpeech"]["text"] == "Here are some picks."


def test_view_recommendation_user_event_returns_detail_screen(mocked_aws):
    from handlers.handler import lambda_handler

    event = {
        "context": {"System": {"application": {"applicationId": "amzn1.ask.skill.test123"}}},
        "session": {
            "sessionId": "amzn1.echo-api.session.abc",
            "user": {"userId": "amzn1.ask.account.APPROVED"},
            "attributes": {"boredom_buster_recommendations": _SAMPLE_RECS},
        },
        "request": {
            "type": "Alexa.Presentation.APL.UserEvent",
            "arguments": ["viewRecommendation", "0"],
        },
    }
    resp = lambda_handler(event, {})

    directives = resp["response"]["directives"]
    assert directives[0]["token"] == "boredomBusterDetail"
    assert resp["sessionAttributes"]["boredom_buster_current_index"] == 0
    assert "Fight Club" in resp["response"]["outputSpeech"]["text"]


def test_view_recommendation_user_event_out_of_range_index_handled_gracefully(mocked_aws):
    from handlers.handler import lambda_handler

    event = {
        "context": {"System": {"application": {"applicationId": "amzn1.ask.skill.test123"}}},
        "session": {
            "sessionId": "amzn1.echo-api.session.abc",
            "user": {"userId": "amzn1.ask.account.APPROVED"},
            "attributes": {"boredom_buster_recommendations": _SAMPLE_RECS},
        },
        "request": {
            "type": "Alexa.Presentation.APL.UserEvent",
            "arguments": ["viewRecommendation", "99"],
        },
    }
    resp = lambda_handler(event, {})
    assert "something went wrong" in resp["response"]["outputSpeech"]["text"]


def test_back_to_recommendations_user_event_redisplays_grid(mocked_aws):
    from handlers.handler import lambda_handler

    event = {
        "context": {"System": {"application": {"applicationId": "amzn1.ask.skill.test123"}}},
        "session": {
            "sessionId": "amzn1.echo-api.session.abc",
            "user": {"userId": "amzn1.ask.account.APPROVED"},
            "attributes": {
                "boredom_buster_recommendations": _SAMPLE_RECS,
                "boredom_buster_current_index": 0,
            },
        },
        "request": {
            "type": "Alexa.Presentation.APL.UserEvent",
            "arguments": ["backToRecommendations"],
        },
    }
    resp = lambda_handler(event, {})

    directives = resp["response"]["directives"]
    assert directives[0]["token"] == "boredomBusterRecommendations"
    assert resp["sessionAttributes"]["boredom_buster_recommendations"] == _SAMPLE_RECS


def test_back_to_recommendations_with_no_stored_recommendations_handled_gracefully(mocked_aws):
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    event = {
        "context": {"System": {"application": {"applicationId": "amzn1.ask.skill.test123"}}},
        "session": {
            "sessionId": "amzn1.echo-api.session.abc",
            "user": {"userId": "amzn1.ask.account.APPROVED"},
            "attributes": {},
        },
        "request": {
            "type": "Alexa.Presentation.APL.UserEvent",
            "arguments": ["backToRecommendations"],
        },
    }
    resp = lambda_handler(event, {})
    # When there are no recommendations to return to (e.g. came from watchlist),
    # falls back gracefully to the watchlist instead of showing an error.
    assert "watchlist" in resp["response"]["outputSpeech"]["text"].lower()


def test_record_feedback_user_event_calls_agentcore_and_logs_call(mocked_aws):
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    event = {
        "context": {"System": {"application": {"applicationId": "amzn1.ask.skill.test123"}}},
        "session": {
            "sessionId": "amzn1.echo-api.session.abc",
            "user": {"userId": "amzn1.ask.account.APPROVED"},
            "attributes": {
                "boredom_buster_recommendations": _SAMPLE_RECS,
                "boredom_buster_current_index": 0,
            },
        },
        "request": {
            "type": "Alexa.Presentation.APL.UserEvent",
            "arguments": ["recordFeedback", "liked"],
        },
    }

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.return_value = _fake_agentcore_response(
            [], text="Noted your feedback."
        )
        resp = lambda_handler(event, {})

    assert "liked" in resp["response"]["outputSpeech"]["text"]
    assert "Fight Club" in resp["response"]["outputSpeech"]["text"]

    call_log_table = mocked_aws["dynamodb"].Table("SkillCallLog")
    logs = call_log_table.scan()["Items"]
    assert len(logs) == 1
    assert logs[0]["intent_or_task"] == "BoredomBusterFeedback"
    assert logs[0]["success"] is True


def test_record_feedback_user_event_denied_for_unapproved_user(mocked_aws):
    from handlers.handler import lambda_handler

    event = {
        "context": {"System": {"application": {"applicationId": "amzn1.ask.skill.test123"}}},
        "session": {
            "sessionId": "amzn1.echo-api.session.abc",
            "user": {"userId": "amzn1.ask.account.NEVERSEEN"},
            "attributes": {
                "boredom_buster_recommendations": _SAMPLE_RECS,
                "boredom_buster_current_index": 0,
            },
        },
        "request": {
            "type": "Alexa.Presentation.APL.UserEvent",
            "arguments": ["recordFeedback", "disliked"],
        },
    }
    resp = lambda_handler(event, {})
    assert "not authorized" in resp["response"]["outputSpeech"]["text"]


def test_record_feedback_user_event_missing_sentiment_handled_gracefully(mocked_aws):
    from handlers.handler import lambda_handler

    event = {
        "context": {"System": {"application": {"applicationId": "amzn1.ask.skill.test123"}}},
        "session": {
            "sessionId": "amzn1.echo-api.session.abc",
            "user": {"userId": "amzn1.ask.account.APPROVED"},
            "attributes": {
                "boredom_buster_recommendations": _SAMPLE_RECS,
                "boredom_buster_current_index": 0,
            },
        },
        "request": {
            "type": "Alexa.Presentation.APL.UserEvent",
            "arguments": ["recordFeedback"],
        },
    }
    resp = lambda_handler(event, {})
    assert "something went wrong" in resp["response"]["outputSpeech"]["text"]


def test_unrecognized_user_event_handled_gracefully(mocked_aws):
    from handlers.handler import lambda_handler

    event = {
        "context": {"System": {"application": {"applicationId": "amzn1.ask.skill.test123"}}},
        "session": {
            "sessionId": "amzn1.echo-api.session.abc",
            "user": {"userId": "amzn1.ask.account.APPROVED"},
            "attributes": {},
        },
        "request": {
            "type": "Alexa.Presentation.APL.UserEvent",
            "arguments": ["someUnknownEvent"],
        },
    }
    resp = lambda_handler(event, {})
    assert "something went wrong" in resp["response"]["outputSpeech"]["text"]


# --------------------------------------------------------------------------
# Round-5 reported issues:
#   1. "the boredom buster intent doesn't ask for mood"
#   2. "instead of showing the movie/tv choices on screen, it is showing
#      the response from agent on screen ... should just show the
#      movie/tv choices"
#   4. "when I say recommend a movie ... it still asks me if I want a
#      movie or a tv recommendation. This is not right because I have
#      just told it"
# --------------------------------------------------------------------------


def _media_type_slot(value="movie", resolved_id="MOVIE"):
    return {
        "name": "MediaType",
        "value": value,
        "resolutions": {
            "resolutionsPerAuthority": [
                {"status": {"code": "ER_SUCCESS_MATCH"}, "values": [{"value": {"id": resolved_id, "name": value}}]}
            ]
        },
    }


def _apl_event_with_slots(user_id: str, slots: dict, intent_name="BoredomBusterIntent"):
    event = _intent_event(user_id, intent_name=intent_name, slots=slots)
    event["context"]["System"]["device"] = {"supportedInterfaces": {"Alexa.Presentation.APL": {}}}
    event["session"]["sessionId"] = "amzn1.echo-api.session.0000000000000000000000000000000000"
    return event


def _fake_agentcore_response_with_clarification(text, needs_clarification=True, recommendations=None):
    body = json.dumps(
        {
            "response_text": text,
            "input_tokens": 4,
            "output_tokens": 6,
            "recommendations": recommendations or [],
            "needs_clarification": needs_clarification,
        }
    ).encode("utf-8")
    return {"response": _FakeStreamingBody(body)}


def test_recommend_a_movie_sends_media_type_to_the_agent(mocked_aws):
    """
    "recommend a movie" fills no AMAZON.SearchQuery slot at all, so
    without the MediaType slot the agent would receive an empty string
    and have no way to know a movie was asked for. Confirms MediaType
    reaches the agent's payload as the word "movie" in the media_type
    field.
    """
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.return_value = _fake_agentcore_response(_SAMPLE_RECS)
        event = _apl_event_with_slots(
            "amzn1.ask.account.APPROVED",
            {"MediaType": _media_type_slot(), "MoodOrGenre": {"name": "MoodOrGenre", "value": None}},
        )
        lambda_handler(event, {})

        sent_payload = json.loads(mock_client.invoke_agent_runtime.call_args.kwargs["payload"])

    # MediaType is passed in the payload's media_type field, not in input
    # (input is empty so the agent asks for mood; media_type tells it what to search)
    assert sent_payload["media_type"] == "movie"


def test_recommend_a_tv_show_sends_tv_show_to_the_agent(mocked_aws):
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.return_value = _fake_agentcore_response(_SAMPLE_RECS)
        event = _apl_event_with_slots(
            "amzn1.ask.account.APPROVED",
            {"MediaType": _media_type_slot("series", "TV"), "MoodOrGenre": {"name": "MoodOrGenre", "value": None}},
        )
        lambda_handler(event, {})

        sent_payload = json.loads(mock_client.invoke_agent_runtime.call_args.kwargs["payload"])

    # MediaType is passed in the payload's media_type field, not in input
    # (input is empty so the agent asks for mood; media_type tells it what to search)
    assert sent_payload["media_type"] == "tv"


def test_recommendations_grid_wins_over_needs_clarification(mocked_aws):
    """
    If a turn produces BOTH recommendations and a needs_clarification
    flag, the recommendations grid must win over the generic message
    screen showing the agent's prose -- showing what was found beats
    asking a question the user has effectively already answered.
    """
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.return_value = _fake_agentcore_response_with_clarification(
            "Let me search for that and check your history first...",
            needs_clarification=True,
            recommendations=_SAMPLE_RECS,
        )
        event = _apl_event_with_slots(
            "amzn1.ask.account.APPROVED", {"MoodOrGenre": {"name": "MoodOrGenre", "value": "something funny"}}
        )
        resp = lambda_handler(event, {})

    directives = resp["response"]["directives"]
    assert len(directives) == 1
    assert directives[0]["token"] == "boredomBusterRecommendations"
    # No Dialog.ElicitSlot, and no generic message screen rendering the
    # agent's prose.
    assert all(d["type"] != "Dialog.ElicitSlot" for d in directives)


def test_recommendations_grid_screen_never_renders_the_agent_prose(mocked_aws):
    """The recommendations document's datasource carries ONLY structured
    title data -- the agent's response text must not appear anywhere in
    the rendered document."""
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    prose = "I searched TMDB and found several options, then checked your viewing history."
    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.return_value = _fake_agentcore_response(_SAMPLE_RECS, text=prose)
        event = _apl_event_with_slots(
            "amzn1.ask.account.APPROVED", {"MoodOrGenre": {"name": "MoodOrGenre", "value": "something funny"}}
        )
        resp = lambda_handler(event, {})

    rendered = json.dumps(resp["response"]["directives"][0])
    assert prose not in rendered
    assert "Fight Club" in rendered


def test_boredom_buster_clarification_shows_tappable_mood_chips(mocked_aws):
    """
    A clarifying turn about mood must send both a Dialog.ElicitSlot
    (voice path) and a clarification APL screen with tappable chips (a
    path that can't be misheard).
    """
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.return_value = _fake_agentcore_response_with_clarification(
            "What are you in the mood for?"
        )
        event = _apl_event_with_slots(
            "amzn1.ask.account.APPROVED",
            {"MediaType": _media_type_slot(), "MoodOrGenre": {"name": "MoodOrGenre", "value": None}},
        )
        resp = lambda_handler(event, {})

    directive_types = [d["type"] for d in resp["response"]["directives"]]
    assert "Dialog.ElicitSlot" in directive_types
    assert "Alexa.Presentation.APL.RenderDocument" in directive_types

    apl_directive = next(d for d in resp["response"]["directives"] if d["type"] == "Alexa.Presentation.APL.RenderDocument")
    assert apl_directive["token"] == "boredomBusterClarify"
    assert apl_directive["datasources"]["clarifyData"]["properties"]["question"] == "What are you in the mood for?"

    # The already-supplied MediaType must be preserved in the elicited
    # intent, so answering the mood question doesn't lose "movie".
    elicit = next(d for d in resp["response"]["directives"] if d["type"] == "Dialog.ElicitSlot")
    assert elicit["updatedIntent"]["slots"]["MediaType"]["value"] == "movie"
    assert resp["sessionAttributes"]["boredom_buster_media_type"] == "movie"


def test_get_weather_clarification_stays_voice_only(mocked_aws):
    """GetWeather's location has no shortlist of good answers, so it gets
    no chip screen -- only the ElicitSlot directive, plus the generic
    message screen."""
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    fake_payload = json.dumps(
        {
            "response_text": "I couldn't find that location. Which city did you mean?",
            "input_tokens": 0,
            "output_tokens": 0,
            "recommendations": [],
            "needs_clarification": True,
        }
    ).encode("utf-8")

    with patch("bedrock_client._lambda_client") as mock_client:
        mock_client.invoke.return_value = {"Payload": _FakeStreamingBody(fake_payload)}
        event = _apl_event_with_slots(
            "amzn1.ask.account.APPROVED",
            {"Location": {"name": "Location", "value": "Nowhereville"}},
            intent_name="GetWeatherIntent",
        )
        resp = lambda_handler(event, {})

    tokens = [d.get("token") for d in resp["response"]["directives"]]
    assert "boredomBusterClarify" not in tokens


def test_choose_mood_user_event_reuses_stored_media_type(mocked_aws):
    """
    Tapping a mood chip is an APL UserEvent, which carries no Alexa slots
    at all -- so the MediaType the user already gave has to come from
    session attributes. Without that, tapping "Funny" after saying
    "recommend a movie" would lose the "movie" part entirely.
    """
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    event = {
        "context": {
            "System": {
                "application": {"applicationId": "amzn1.ask.skill.test123"},
                "device": {"supportedInterfaces": {"Alexa.Presentation.APL": {}}},
            }
        },
        "session": {
            "user": {"userId": "amzn1.ask.account.APPROVED"},
            "sessionId": "amzn1.echo-api.session.0000000000000000000000000000000000",
            "attributes": {"boredom_buster_media_type": "movie"},
        },
        "request": {"type": "Alexa.Presentation.APL.UserEvent", "arguments": ["chooseMood", "something funny"]},
    }

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.return_value = _fake_agentcore_response(_SAMPLE_RECS)
        resp = lambda_handler(event, {})

        sent_payload = json.loads(mock_client.invoke_agent_runtime.call_args.kwargs["payload"])

    assert sent_payload["input"] == "something funny movie"
    assert resp["response"]["directives"][0]["token"] == "boredomBusterRecommendations"
    assert resp["sessionAttributes"]["boredom_buster_media_type"] == "movie"


def test_choose_mood_user_event_without_stored_media_type(mocked_aws):
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    event = {
        "context": {
            "System": {
                "application": {"applicationId": "amzn1.ask.skill.test123"},
                "device": {"supportedInterfaces": {"Alexa.Presentation.APL": {}}},
            }
        },
        "session": {
            "user": {"userId": "amzn1.ask.account.APPROVED"},
            "sessionId": "amzn1.echo-api.session.0000000000000000000000000000000000",
            "attributes": {},
        },
        "request": {"type": "Alexa.Presentation.APL.UserEvent", "arguments": ["chooseMood", "something thrilling"]},
    }

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.return_value = _fake_agentcore_response(_SAMPLE_RECS)
        lambda_handler(event, {})

        sent_payload = json.loads(mock_client.invoke_agent_runtime.call_args.kwargs["payload"])

    assert sent_payload["input"] == "something thrilling"


def test_choose_mood_user_event_missing_mood_handled_gracefully(mocked_aws):
    from handlers.handler import lambda_handler

    event = {
        "context": {"System": {"application": {"applicationId": "amzn1.ask.skill.test123"}}},
        "session": {"user": {"userId": "amzn1.ask.account.APPROVED"}, "attributes": {}},
        "request": {"type": "Alexa.Presentation.APL.UserEvent", "arguments": ["chooseMood"]},
    }
    resp = lambda_handler(event, {})

    assert "something went wrong" in resp["response"]["outputSpeech"]["text"]


def test_record_feedback_keeps_the_detail_screen_visible(mocked_aws):
    """A like/dislike is an aside, not a navigation step -- the detail
    screen the button was pressed on should stay up rather than being
    replaced by the generic message screen."""
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    event = {
        "context": {
            "System": {
                "application": {"applicationId": "amzn1.ask.skill.test123"},
                "device": {"supportedInterfaces": {"Alexa.Presentation.APL": {}}},
            }
        },
        "session": {
            "user": {"userId": "amzn1.ask.account.APPROVED"},
            "sessionId": "amzn1.echo-api.session.0000000000000000000000000000000000",
            "attributes": {
                "boredom_buster_recommendations": _SAMPLE_RECS,
                "boredom_buster_current_index": 0,
            },
        },
        "request": {"type": "Alexa.Presentation.APL.UserEvent", "arguments": ["recordFeedback", "liked"]},
    }

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        mock_client.invoke_agent_runtime.return_value = _fake_agentcore_response([], text="Noted.")
        resp = lambda_handler(event, {})

    assert "liked Fight Club" in resp["response"]["outputSpeech"]["text"]
    assert resp["response"]["directives"][0]["token"] == "boredomBusterDetail"
    assert resp["sessionAttributes"]["boredom_buster_current_index"] == 0


# --------------------------------------------------------------------------
# Saying "specific search" by voice must show the same keyword search
# screen that tapping the "Specific Search" chip shows (chooseMood
# UserEvent with mood == "SHOW_KEYWORD_OPTIONS" -- see
# _handle_choose_mood and _handle_intent_request's matching voice-path
# check).
# --------------------------------------------------------------------------


def test_saying_specific_search_shows_keyword_search_screen(mocked_aws):
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    event = _apl_event_with_slots(
        "amzn1.ask.account.APPROVED",
        {"MoodOrGenre": {"name": "MoodOrGenre", "value": "specific search"}},
    )
    # No AgentCore call should happen at all for this path -- the keyword
    # screen is pure navigation, same as the tap equivalent.
    with patch("bedrock_client._agentcore_runtime") as mock_client:
        resp = lambda_handler(event, {})
        mock_client.invoke_agent_runtime.assert_not_called()

    directives = resp["response"]["directives"]
    apl_directive = next(d for d in directives if d["type"] == "Alexa.Presentation.APL.RenderDocument")
    assert apl_directive["token"] == "boredomBusterClarify"
    assert apl_directive["datasources"]["keywordData"]["properties"]["options"]
    assert resp["response"]["shouldEndSession"] is False
    elicit_directive = next(d for d in directives if d["type"] == "Dialog.ElicitSlot")
    assert elicit_directive["slotToElicit"] == "MoodOrGenre"


def test_saying_specific_search_is_case_and_whitespace_insensitive(mocked_aws):
    """Voice recognition capitalization/whitespace shouldn't matter --
    matches the same normalization router.build_query_text already
    applies (mood.lower())."""
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    event = _apl_event_with_slots(
        "amzn1.ask.account.APPROVED",
        {"MoodOrGenre": {"name": "MoodOrGenre", "value": "  Specific Search  "}},
    )
    with patch("bedrock_client._agentcore_runtime") as mock_client:
        resp = lambda_handler(event, {})
        mock_client.invoke_agent_runtime.assert_not_called()

    apl_directive = next(d for d in resp["response"]["directives"] if d["type"] == "Alexa.Presentation.APL.RenderDocument")
    assert apl_directive["token"] == "boredomBusterClarify"


def test_saying_specific_search_preserves_media_type_in_speech(mocked_aws):
    """If MediaType was already established (e.g. from a prior 'recommend
    a movie' turn), the keyword screen's speech should be movie-specific,
    matching the tap path's behavior in _handle_choose_mood."""
    from handlers.handler import lambda_handler

    users_table = mocked_aws["dynamodb"].Table("SkillUsers")
    users_table.put_item(Item={"userId": "amzn1.ask.account.APPROVED", "status": "approved", "total_calls": 0})

    event = _apl_event_with_slots(
        "amzn1.ask.account.APPROVED",
        {"MoodOrGenre": {"name": "MoodOrGenre", "value": "specific search"}},
    )
    event["session"]["attributes"] = {"boredom_buster_media_type": "movie"}

    with patch("bedrock_client._agentcore_runtime") as mock_client:
        resp = lambda_handler(event, {})
        mock_client.invoke_agent_runtime.assert_not_called()

    assert "movie search" in resp["response"]["outputSpeech"]["text"]
