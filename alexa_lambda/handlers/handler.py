"""
Lambda entry point for the Echo Show Jarvis AI skill.

Implements the request processing pipeline described in solution-design.md
Section 4: skill-ID check, allowlist check, rate-limit check, and input
length check, plus the logging and response-building hooks that wrap
every request. Routing to Bedrock/AgentCore itself is delegated to
`router.route_request` -- this file owns the pipeline shell and the
checks that can reject a request cheaply, not the Bedrock integration.

This module is deliberately thin: the actual per-feature response
building lives in sibling modules under this package --
`launch_screen.py` (launch cards, launchAction), `boredom_buster.py`
(mood selection, keyword search, feedback), `watchlist.py` (watchlist
screens), `smart_assistant.py` (follow-up questions), and
`detail_actions.py` (detail-screen voice actions, cross-screen return
navigation). This file owns only: the top-level dispatch by request
type, the shared allowlist/rate-limit/input-length pipeline for
IntentRequest, and the Alexa.Presentation.APL.UserEvent dispatch table
that routes each touch event to its owning module.
"""

import logging
import os
import time

from allowlist import check_allowlist
from rate_limit import RateLimitExceeded, check_and_increment
from utils import apl
from utils.logging_config import emit_denial_metric, log_error, log_incoming_request, log_outgoing_response
from utils.response import (
    build_apl_directive_response,
    build_elicit_slot_response,
    build_error_response,
    build_input_too_long_response,
    build_launch_response,
    build_not_authorized_response,
    build_rate_limited_response,
    build_speech_response,
)

from .boredom_buster import (
    build_show_keyword_options_response,
    handle_back_to_mood_selection,
    handle_choose_mood,
    handle_record_feedback,
)
from .common import (
    INTENT_TO_CLARIFICATION_SLOT,
    MAX_INPUT_CHARS_DEFAULT,
    SESSION_ATTR_CURRENT_INDEX,
    SESSION_ATTR_MEDIA_TYPE,
    SESSION_ATTR_RECOMMENDATIONS,
    SESSION_ATTR_REGION,
    SESSION_ATTR_LAST_SCREEN,
    build_enabled_task_phrases,
    condense_ask_bedrock_response,
    extract_media_type,
    extract_query_text,
    log_failed_call,
    log_successful_call,
    resolve_region,
    strip_markdown,
)
from .detail_actions import (
    handle_detail_screen_action,
    handle_open_trailer,
    handle_return_to_previous_screen,
)
from .launch_screen import (
    add_apl_message_directive_if_supported,
    build_launch_response_with_cards,
    handle_launch_action,
)
from .smart_assistant import handle_ask_followup
from .watchlist import (
    handle_add_to_watchlist,
    handle_remove_from_watchlist,
    handle_remove_from_watchlist_item,
    handle_view_watchlist,
    handle_view_watchlist_item,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    """
    Thin wrapper around _dispatch() whose only job is guaranteeing every
    single invocation logs both its incoming request AND its outgoing
    response exactly once, regardless of which of _dispatch()'s many
    internal return points produced that response -- see
    log_incoming_request's and log_outgoing_response's docstrings. Doing
    this here, in one place, means no future new return statement deep
    inside _handle_intent_request/_handle_apl_user_event/etc. can
    accidentally skip logging the outgoing response the way scattering
    a log call before every individual `return` risked doing.
    """
    # Logged unconditionally, before ANY pipeline check (skill-ID,
    # allowlist, rate limit, routing) runs -- see log_incoming_request's
    # docstring for why this must run first: every invocation needs a
    # complete audit trail in CloudWatch Logs regardless of what happens
    # to it afterwards (denied, routed successfully, or crashed).
    log_incoming_request(event)
    response = _dispatch(event)
    response = add_apl_message_directive_if_supported(event, response)
    log_outgoing_response(event, response)
    return response


def _dispatch(event: dict) -> dict:
    try:
        request = event.get("request", {})
        request_type = request.get("type", "")

        if request_type == "LaunchRequest":
            # Check if device supports APL for the launch screen with cards
            if apl.supports_apl(event):
                return build_launch_response_with_cards()
            else:
                # Fall back to voice-only response for non-APL devices
                enabled_phrases = build_enabled_task_phrases()
                return build_launch_response(enabled_phrases)

        if request_type == "SessionEndedRequest":
            return build_speech_response(speech_text="", should_end_session=True)

        if request_type == "Alexa.Presentation.APL.UserEvent":
            # Touch events from Boredom Buster's APL screens (card tap,
            # back button, like/dislike) -- see solution-design.md
            # Section 13. No other intent in this project uses APL, so
            # this branch is Boredom-Buster-specific by construction,
            # not a generic APL event router.
            return _handle_apl_user_event(event)

        if request_type != "IntentRequest":
            logger.info("Unhandled request type: %s", request_type)
            return build_error_response()

        return _handle_intent_request(event)

    except Exception as exc:  # noqa: BLE001 -- last-resort catch-all so
        # the skill always returns a valid Alexa response instead of a
        # raw Lambda error, which Alexa would render as "there was a
        # problem with the requested skill's response."
        log_error("Unhandled exception in lambda_handler", exc)
        return build_error_response()


def _handle_intent_request(event: dict) -> dict:
    request = event["request"]
    intent = request.get("intent", {})
    intent_name = intent.get("name", "")

    # --- Pipeline step 1: skill authenticity check (defense in depth) ---
    # See solution-design.md Section 4, step 1. The primary enforcement is
    # the aws_lambda_permission's event_source_token (terraform/lambda.tf);
    # this is a second layer in case that mechanism is ever bypassed or
    # misconfigured.
    configured_skill_id = os.environ.get("ALEXA_SKILL_ID", "")
    received_app_id = (
        event.get("context", {}).get("System", {}).get("application", {}).get("applicationId", "")
    )
    if configured_skill_id and received_app_id and configured_skill_id != received_app_id:
        logger.warning(
            "Rejected request: applicationId mismatch (configured=%s, received=%s)",
            configured_skill_id,
            received_app_id,
        )
        return build_error_response()

    # Built-in intents that don't need the allowlist/rate-limit pipeline at
    # all -- there's nothing to gate, they don't touch Bedrock.
    if intent_name in ("AMAZON.CancelIntent", "AMAZON.StopIntent"):
        return build_speech_response(speech_text="Goodbye.", should_end_session=True)
    if intent_name == "AMAZON.HelpIntent":
        return build_launch_response_with_cards()
    if intent_name == "AMAZON.NavigateHomeIntent":
        return build_launch_response_with_cards()
    if intent_name == "ReturnToMainMenuIntent":
        # User wants to return to the main launch screen from anywhere
        return build_launch_response_with_cards()
    if intent_name == "ReturnToPreviousScreenIntent":
        # User wants to return to where they were before leaving via button press
        return handle_return_to_previous_screen(event)
    if intent_name == "AMAZON.FallbackIntent":
        # Alexa's NLU always force-matches an utterance to the closest
        # intent it has, even for genuinely out-of-domain requests --
        # confirmed against Amazon's own docs: "A user can input an
        # undefined nonsense utterance well outside the scope of the
        # provided samples and Alexa still attempts to choose an intent.
        # This behavior is by design." Without AMAZON.FallbackIntent, an
        # unmatched request like "tell me the latest news" would
        # silently land on an unrelated intent (e.g. GetWeatherIntent)
        # instead of a graceful "I don't understand" -- which is exactly
        # what caused a real AccessDeniedException in production against
        # a placeholder AgentCore ARN.
        #
        # IMPORTANT LIMITATION, not a bug: AMAZON.FallbackIntent carries
        # NO slot data and NOT the user's raw spoken text -- Alexa only
        # signals "nothing matched with confidence," never what was
        # actually said. So this cannot forward the real question to
        # Bedrock; it can only respond with a fixed, helpful message
        # pointing the user at what the skill can actually do. This is a
        # platform limitation of the classic Alexa Skills Kit NLU, not
        # something fixable from this Lambda's code.
        logger.info("Fallback intent triggered -- utterance did not match any known intent")
        enabled_phrases = build_enabled_task_phrases()
        if enabled_phrases:
            help_clause = " You can ask me a general question, or " + " or ".join(enabled_phrases) + "."
        else:
            help_clause = " You can ask me a general question."
        return build_speech_response(
            speech_text="I'm not sure how to help with that." + help_clause,
            card_title="Jarvis AI",
            card_content="I'm not sure how to help with that." + help_clause,
            should_end_session=False,
        )

    session = event.get("session", {})
    user_id = session.get("user", {}).get("userId", "")
    if not user_id:
        logger.error("Request missing session.user.userId -- cannot proceed")
        return build_error_response()

    # Alexa's own session.sessionId -- distinct from userId. userId is
    # permanent per user-per-skill; sessionId is a new value every time
    # the user opens a fresh conversation with the skill (it does not
    # survive "Alexa, stop" or ~8s of silence). Passed through to
    # AgentCore-backed tasks (router.py) as the AgentCore Runtime
    # session identifier, so multi-turn AgentCore conversations (e.g.
    # Boredom Buster's clarifying-question flow) align with Alexa's own
    # session boundaries, while userId separately serves as the stable
    # actor identifier for AgentCore Memory's per-user long-term
    # preferences -- see solution-design.md Section 12.3.
    alexa_session_id = session.get("sessionId", "")
    session_attributes = session.get("attributes", {}) or {}

    # For BoredomBusterIntent, if MediaType slot is not filled in the current
    # request, try to get it from session attributes to preserve context
    # across turns (e.g. when user responds to a clarification question).
    # This fixes an issue where after a clarifying question about mood,
    # follow-up utterances like "surprise me" would lose the media type
    # context established earlier in the conversation.
    if intent_name == "BoredomBusterIntent":
        media_type_slot = intent.get("slots", {}).get("MediaType")
        if not media_type_slot or not media_type_slot.get("value"):
            # Try to get media_type from session attributes
            media_type_from_session = session_attributes.get(SESSION_ATTR_MEDIA_TYPE, "")
            if media_type_from_session:
                # Add MediaType slot with the session value
                if "slots" not in intent:
                    intent["slots"] = {}
                intent["slots"]["MediaType"] = {
                    "name": "MediaType",
                    "value": media_type_from_session,
                    "resolutions": {
                        "resolutionsPerAuthority": [
                            {
                                "status": {"code": "ER_SUCCESS_MATCH"},
                                "values": [
                                    {"value": {"id": "MOVIE" if media_type_from_session == "movie" else "TV", "name": media_type_from_session}}
                                ],
                            }
                        ]
                    },
                }

    # --- Pipeline step 2: allowlist check ---
    # See solution-design.md Section 4, step 2 and allowlist.py.
    user = check_allowlist(user_id)
    if user is None:
        logger.info("Call denied: userId=%s intent=%s reason=not_approved", user_id, intent_name)
        emit_denial_metric(user_id, "not_approved")
        return build_not_authorized_response()

    # --- Pipeline step 3: rate-limit check ---
    # See solution-design.md Section 4, step 3 and rate_limit.py.
    try:
        check_and_increment(user)
    except RateLimitExceeded as e:
        logger.info("Call denied: userId=%s intent=%s reason=%s", user_id, intent_name, e.reason)
        emit_denial_metric(user_id, e.reason)
        return build_rate_limited_response(e.reason)

    # ViewWatchlistIntent is a local DB read - no Bedrock/AgentCore call needed
    # Handle it after allowlist/rate-limit but before router
    if intent_name == "ViewWatchlistIntent":
        return handle_view_watchlist(event, user_id)

    # --- Pipeline step 4: input length check ---
    # See solution-design.md Section 4, step 4.
    query_text = extract_query_text(intent)
    max_input_chars = int(os.environ.get("MAX_INPUT_CHARS", MAX_INPUT_CHARS_DEFAULT))
    if query_text is not None and len(query_text) > max_input_chars:
        logger.info(
            "Call denied: userId=%s intent=%s reason=input_too_long input_length=%d",
            user_id,
            intent_name,
            len(query_text),
        )
        emit_denial_metric(user_id, "input_too_long")
        return build_input_too_long_response()

    # For SmartAssistantIntent, check if Query slot is empty (user said just
    # "smart assistant" without a question). Use Dialog.ElicitSlot to prompt
    # them to ask their question, ensuring the follow-up utterance gets routed
    # back to SmartAssistantIntent instead of falling through to FallbackIntent.
    if intent_name == "SmartAssistantIntent":
        query_slot = intent.get("slots", {}).get("Query", {})
        if not query_slot.get("value"):
            return build_elicit_slot_response(
                speech_text="Sure, what would you like to ask me?",
                intent_name="SmartAssistantIntent",
                slot_to_elicit="Query",
                existing_slots=intent.get("slots", {}),
            )

    # For BoredomBusterIntent, saying "specific search" is the voice
    # equivalent of tapping the "Specific Search" chip (apl.py's
    # CLARIFY_MOOD_OPTIONS) and must show the same keyword search screen,
    # not be routed to the agent. This check runs before routing (pipeline
    # step 5) so it never reaches the agent, mirroring
    # handle_choose_mood's SHOW_KEYWORD_OPTIONS check for the tap path via
    # the same shared helper.
    if intent_name == "BoredomBusterIntent":
        mood_value = (intent.get("slots", {}).get("MoodOrGenre") or {}).get("value") or ""
        if mood_value.strip().lower() == "specific search":
            return build_show_keyword_options_response(event, user_id, session_attributes)

    # --- Pipeline step 5: route and invoke ---
    start_time = time.time()
    try:
        from router import route_request

        # Get user's region for streaming availability (TMDB watch/providers).
        # Auto-detected from Alexa's locale; stored value overrides.
        user_region = resolve_region(session_attributes, request.get("locale"))

        # Build Alexa context for Device Address API (weather location)
        alexa_context = {
            "api_endpoint": event.get("context", {}).get("System", {}).get("apiEndpoint"),
            "api_access_token": event.get("context", {}).get("System", {}).get("apiAccessToken"),
            "device_id": event.get("context", {}).get("System", {}).get("device", {}).get("deviceId"),
        }

        result = route_request(
            intent_name=intent_name,
            slots=intent.get("slots", {}),
            user_id=user_id,
            session_id=alexa_session_id,
            region=user_region,
            alexa_context=alexa_context,
        )
        latency_ms = (time.time() - start_time) * 1000
        log_successful_call(user_id, intent_name, result, latency_ms)

        # Handle DetailScreenActionIntent - voice commands on detail screen
        if intent_name == "DetailScreenActionIntent":
            return handle_detail_screen_action(event, intent, user_id, alexa_session_id, result, session_attributes)

        # Boredom Buster is the only intent that returns structured
        # recommendations -- see router.py's RouteResult docstring.
        # Every other intent's result.recommendations is always [], so
        # this branch never fires for AskBedrock/GetWeather/etc.
        #
        # CHECKED BEFORE needs_clarification, deliberately: if a turn
        # somehow produced both (the agent searched AND asked a
        # follow-up), showing what was found beats asking a question the
        # user has effectively already answered. This ordering also
        # guarantees the recommendations grid, not the generic message
        # screen with the agent's prose, is what renders whenever there
        # are titles to show.
        if result.recommendations and apl.supports_apl(event):
            media_type = extract_media_type(intent)
            session = event.get("session", {})
            user_id = session.get("user", {}).get("userId", "")

            # Extract the search query from MoodOrGenre slot to show in the header
            search_query = ""
            mood_slot = intent.get("slots", {}).get("MoodOrGenre", {})
            if mood_slot and mood_slot.get("value"):
                search_query = mood_slot.get("value", "")

            document, datasources = apl.build_recommendations_document(
                result.recommendations, media_type, user_id, search_query
            )
            return build_apl_directive_response(
                speech_text=result.response_text,
                document=document,
                datasources=datasources,
                token=apl.APL_TOKEN_RECOMMENDATIONS,
                session_attributes={
                    SESSION_ATTR_RECOMMENDATIONS: result.recommendations,
                    SESSION_ATTR_MEDIA_TYPE: media_type,
                    SESSION_ATTR_REGION: user_region,
                    # Store screen state for return functionality
                    SESSION_ATTR_LAST_SCREEN: {
                        "type": "recommendations",
                        "recommendations": result.recommendations,
                        "media_type": media_type,
                    },
                },
            )

        # A resolvable gap (GetWeatherIntent's location couldn't be
        # geocoded; Boredom Buster needs to know the user's mood) should
        # re-prompt for that slot and keep the conversation going, not
        # end the session with a terminal message -- see
        # lambda_tasks/get_weather/handler.py's and
        # agents/boredom_buster_agent/agent.py's docstrings. Only intents
        # in INTENT_TO_CLARIFICATION_SLOT support this; any other
        # intent's needs_clarification is always False
        # (bedrock_client.py's default), so this branch never fires for
        # them.
        clarification_slot = INTENT_TO_CLARIFICATION_SLOT.get(intent_name)
        if result.needs_clarification and clarification_slot:
            media_type = extract_media_type(intent)
            apl_document = None
            apl_datasources = None
            apl_token = None
            # Start with existing session attributes and update as needed
            updated_session_attributes = {
                SESSION_ATTR_MEDIA_TYPE: session_attributes.get(SESSION_ATTR_MEDIA_TYPE, ""),
                SESSION_ATTR_REGION: user_region,
            }
            # Boredom Buster's clarifying question is about mood, which
            # has a small, known set of good answers -- so offer them as
            # tappable chips as well as by voice. GetWeather's location
            # has no such shortlist, so it stays voice-only and falls
            # through to the generic message screen.
            if intent_name == "BoredomBusterIntent" and apl.supports_apl(event):
                apl_document, apl_datasources = apl.build_clarification_document(result.response_text, media_type=media_type)
                apl_token = apl.APL_TOKEN_CLARIFY
                # Preserve the media_type from the original launch action
                updated_session_attributes[SESSION_ATTR_MEDIA_TYPE] = media_type
            return build_elicit_slot_response(
                speech_text=result.response_text,
                intent_name=intent_name,
                slot_to_elicit=clarification_slot,
                existing_slots=intent.get("slots", {}),
                apl_document=apl_document,
                apl_datasources=apl_datasources,
                apl_token=apl_token,
                session_attributes=updated_session_attributes,
            )

        # Boredom Buster and Smart Assistant are multi-turn intents that support
        # conversational follow-ups. BoredomBuster's agent can ask clarifying
        # questions ("movie or tv?"), and SmartAssistant keeps the session alive
        # to allow users to ask follow-up questions naturally without restarting.
        # build_speech_response's should_end_session defaults to True, which would
        # close the Alexa session immediately, breaking the conversation. Keep the
        # session open for both intents so follow-up utterances are treated as
        # the next turn of the same conversation.
        keep_session_open = intent_name in ("BoredomBusterIntent", "SmartAssistantIntent", "GetWeatherIntent")

        # Default speech text for every intent that falls through to the
        # generic BoredomBuster/plain-speech branches below. SmartAssistant
        # overrides this with its own condensed text immediately after.
        speech_text = result.response_text

        # For Smart Assistant, build rich APL response with follow-up options
        if intent_name == "SmartAssistantIntent":
            speech_text = condense_ask_bedrock_response(result.response_text)

            # Use custom Smart Assistant APL template for Echo Show
            if apl.supports_apl(event):
                # Generate follow-up suggestions (simplified for now - can be enhanced later)
                follow_ups = []

                # Strip markdown from the response for Echo Show display and extract any links
                clean_answer_text, extracted_links = strip_markdown(result.response_text)

                document, datasources = apl.build_smart_assistant_document(
                    answer_text=clean_answer_text,
                    follow_ups=follow_ups,
                    links=extracted_links
                )
                return build_apl_directive_response(
                    speech_text=speech_text,
                    document=document,
                    datasources=datasources,
                    token=apl.APL_TOKEN_SMART_ASSISTANT,
                    should_end_session=False,
                )

            # Fallback for non-APL devices - use simple speech response
            return build_speech_response(
                speech_text=speech_text,
                card_title="Smart Assistant",
                card_content=speech_text,
                should_end_session=False,
            )

        # Boredom Buster needs context preserved across turns even when there
        # are no recommendations and the agent didn't flag clarification -- the
        # model's own text IS the response, but media_type must survive to the
        # next turn or else both search tools reactivate.  Alexa expects
        # sessionAttributes at the top level of the response dict (not inside
        # `response`), so we build it manually here.
        if intent_name == "BoredomBusterIntent":
            media_type = extract_media_type(intent)
            return {
                "version": "1.0",
                "sessionAttributes": {
                    SESSION_ATTR_MEDIA_TYPE: media_type,
                    SESSION_ATTR_REGION: user_region,
                },
                "response": {
                    "outputSpeech": {
                        "type": "PlainText",
                        "text": speech_text,
                    },
                    "card": {
                        "type": "Simple",
                        "title": "Jarvis AI",
                        "content": speech_text,
                    },
                    "shouldEndSession": not keep_session_open,
                },
            }

        return build_speech_response(
            speech_text=speech_text,
            card_title="Jarvis AI",
            card_content=speech_text,
            should_end_session=not keep_session_open,
        )

    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.time() - start_time) * 1000
        log_error(
            f"Error routing intent {intent_name} (userId={user_id}, "
            f"slots={intent.get('slots', {})})",
            exc,
        )
        log_failed_call(user_id, intent_name, latency_ms, exc)
        return build_error_response()


def _handle_apl_user_event(event: dict) -> dict:
    """
    Handles Alexa.Presentation.APL.UserEvent requests sent by Boredom
    Buster's APL screens (solution-design.md Section 13): tapping a
    recommendation card, the back button, or a like/dislike button. This
    is the only APL-using flow in the project, so this handler is
    deliberately specific to Boredom Buster's event types rather than a
    generic APL event dispatcher.

    Navigation events (viewRecommendation, backToRecommendations) are
    zero-cost -- they redisplay data already stored in session
    attributes rather than calling the agent again. Only a handful of
    event types (recordFeedback, chooseMood) make a real AgentCore
    call, so only those go through the allowlist/rate-limit pipeline --
    see each individual handler module for details.
    """
    request = event["request"]
    arguments = request.get("arguments", [])
    event_type = arguments[0] if arguments else None

    session = event.get("session", {})
    user_id = session.get("user", {}).get("userId", "")
    session_attributes = session.get("attributes", {}) or {}
    recommendations = session_attributes.get(SESSION_ATTR_RECOMMENDATIONS, [])

    if event_type == "viewRecommendation":
        return _handle_view_recommendation(event, arguments, recommendations, session_attributes)

    if event_type == "backToRecommendations":
        return _handle_back_to_recommendations(event, user_id, recommendations, session_attributes)

    if event_type == "recordFeedback":
        return handle_record_feedback(event, arguments, user_id, session_attributes, recommendations)

    if event_type == "addToWatchlist":
        return handle_add_to_watchlist(event, arguments, user_id, session_attributes, recommendations)

    if event_type == "removeFromWatchlist":
        return handle_remove_from_watchlist(event, arguments, user_id, session_attributes, recommendations)

    if event_type == "viewWatchlistItem":
        return handle_view_watchlist_item(event, arguments, user_id, session_attributes)

    if event_type == "removeFromWatchlistItem":
        return handle_remove_from_watchlist_item(event, arguments, user_id, session_attributes)

    if event_type == "openTrailer":
        return handle_open_trailer(event, arguments, user_id)

    if event_type == "launchAction":
        return handle_launch_action(event, arguments, user_id)

    if event_type == "chooseMood":
        return handle_choose_mood(event, arguments, user_id, session_attributes)

    if event_type == "askFollowup":
        return handle_ask_followup(event, arguments, user_id, session_attributes)

    if event_type == "backToMoodSelection":
        return handle_back_to_mood_selection(event, user_id, session_attributes)

    logger.warning("Unrecognized Alexa.Presentation.APL.UserEvent arguments: %s", arguments)
    return build_error_response()


def _handle_view_recommendation(event: dict, arguments: list, recommendations: list, session_attributes: dict) -> dict:
    """
    Handles the viewRecommendation UserEvent -- tapping a poster on the
    recommendations grid to see its detail screen. Zero-cost: looks up
    the tapped recommendation from the list already stored in session
    attributes rather than calling the agent again.
    """
    try:
        index = int(arguments[1])
    except (IndexError, ValueError, TypeError):
        logger.warning("viewRecommendation UserEvent missing/invalid index argument: %s", arguments)
        return build_error_response()

    if index < 0 or index >= len(recommendations):
        logger.warning("viewRecommendation index %d out of range (have %d recommendations)", index, len(recommendations))
        return build_error_response()

    recommendation = recommendations[index]

    # Check if this item is in the user's watchlist
    in_watchlist = False
    watchlist_date = ""
    session = event.get("session", {})
    user_id = session.get("user", {}).get("userId", "")
    if user_id and recommendation.get("id"):
        try:
            from watchlist import watchlist as watchlist_client
            watchlist_item = watchlist_client.get_watchlist_item(
                user_id=user_id,
                media_type=recommendation.get("media_type", "movie"),
                tmdb_id=recommendation.get("id"),
            )
            if watchlist_item:
                in_watchlist = True
                from datetime import datetime
                added_at = watchlist_item.get("addedAt", 0)
                if added_at:
                    # DynamoDB returns numbers as Decimal - convert to int
                    dt = datetime.fromtimestamp(int(added_at))
                    watchlist_date = dt.strftime("%b %d, %Y")
        except Exception:
            pass  # Silently ignore watchlist check failures

    document, datasources = apl.build_detail_document(recommendation, in_watchlist, watchlist_date)
    return build_apl_directive_response(
        speech_text=f"Here's more about {recommendation.get('title', 'this title')}.",
        document=document,
        datasources=datasources,
        token=apl.APL_TOKEN_DETAIL,
        session_attributes={
            SESSION_ATTR_RECOMMENDATIONS: recommendations,
            SESSION_ATTR_CURRENT_INDEX: index,
            # Echoed back so it survives this turn -- Alexa replaces
            # the whole session-attribute map with whatever the
            # response returns, so anything omitted here is lost for
            # the rest of the session.
            SESSION_ATTR_MEDIA_TYPE: session_attributes.get(SESSION_ATTR_MEDIA_TYPE, ""),
            # Store screen state for return functionality
            SESSION_ATTR_LAST_SCREEN: {
                "type": "detail",
                "recommendation": recommendation,
                "recommendations": recommendations,
                "current_index": index,
                "media_type": session_attributes.get(SESSION_ATTR_MEDIA_TYPE, ""),
                "in_watchlist": in_watchlist,
                "watchlist_date": watchlist_date,
            },
        },
    )


def _handle_back_to_recommendations(event: dict, user_id: str, recommendations: list, session_attributes: dict) -> dict:
    """
    Handles the backToRecommendations UserEvent -- user tapped "All
    picks" on a detail screen. If we have stored recommendations (came
    from a Boredom Buster recommendations grid), redisplay the grid.
    Otherwise fall back to the watchlist or main menu so the user isn't
    stuck on an error screen when they pressed "All picks" while
    browsing their watchlist.
    """
    if recommendations:
        media_type = session_attributes.get(SESSION_ATTR_MEDIA_TYPE, "")
        document, datasources = apl.build_recommendations_document(recommendations, media_type, user_id)
        return build_apl_directive_response(
            speech_text="Here are your recommendations again.",
            document=document,
            datasources=datasources,
            token=apl.APL_TOKEN_RECOMMENDATIONS,
            session_attributes={
                SESSION_ATTR_RECOMMENDATIONS: recommendations,
                SESSION_ATTR_MEDIA_TYPE: media_type,
                # Store screen state for return functionality
                SESSION_ATTR_LAST_SCREEN: {
                    "type": "recommendations",
                    "recommendations": recommendations,
                    "media_type": media_type,
                },
            },
        )

    # No recommendations — user may be on a watchlist detail screen.
    # Fall back to the watchlist grid (so "All picks" actually returns
    # to a list), or the main menu if the watchlist can't be loaded.
    try:
        from watchlist import watchlist as watchlist_client

        wl_items = watchlist_client.get_watchlist(user_id, limit=50) if user_id else []
        if apl.supports_apl(event):
            return handle_view_watchlist(event, user_id)
        speech_text = f"Here are your {len(wl_items)} watchlist items." if wl_items else "Your watchlist is empty."
        return build_speech_response(
            speech_text=speech_text,
            card_title="Jarvis AI",
            card_content=speech_text,
        )
    except Exception:  # noqa: BLE001
        return build_launch_response_with_cards()
