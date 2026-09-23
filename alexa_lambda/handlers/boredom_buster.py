"""
Boredom Buster's mood-selection / keyword-search UserEvent handlers --
tapping a mood chip, tapping "Specific Search", going back to mood
selection, and recording like/dislike feedback from the detail screen.

The IntentRequest side of Boredom Buster (BoredomBusterIntent itself,
including the voice equivalent of the "specific search" chip) lives in
handler.py's _handle_intent_request, since it shares the main
allowlist/rate-limit/routing pipeline with every other intent. This
module only covers the touch (APL UserEvent) paths that don't go
through that shared pipeline function, plus the handlers these two
paths have in common.
"""

import logging
import time

from allowlist import check_allowlist
from rate_limit import RateLimitExceeded, check_and_increment
from utils import apl
from utils.logging_config import emit_denial_metric, log_error
from utils.response import (
    build_apl_directive_response,
    build_elicit_slot_response,
    build_error_response,
    build_not_authorized_response,
    build_rate_limited_response,
    build_speech_response,
)

from .common import (
    SESSION_ATTR_CURRENT_INDEX,
    SESSION_ATTR_MEDIA_TYPE,
    SESSION_ATTR_RECOMMENDATIONS,
    SESSION_ATTR_REGION,
    SESSION_ATTR_LAST_SCREEN,
    log_failed_call,
    log_successful_call,
    resolve_region,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def build_show_keyword_options_response(event: dict, user_id: str, session_attributes: dict) -> dict:
    """
    Builds the keyword search screen response -- shared by both ways a
    user can ask for it:
    1. Tapping the "Specific Search" chip on the mood selection screen
       (chooseMood UserEvent with mood == "SHOW_KEYWORD_OPTIONS" --
       apl.py's CLARIFY_MOOD_OPTIONS).
    2. Saying "specific search" from that same screen (BoredomBusterIntent
       IntentRequest with MoodOrGenre == "specific search"), handled in
       handler.py's _handle_intent_request's BoredomBusterIntent branch.

    Sharing this helper between the tap and voice paths keeps their
    speech, screen, and session-attribute behavior identical rather than
    maintaining two separate implementations of the same screen.
    """
    if not apl.supports_apl(event):
        # Device doesn't support APL, fall back to voice prompt.
        return build_elicit_slot_response(
            speech_text="What specific movie or TV show would you like to search for? For example, say 'spiderman movies' or 'marvel tv shows'.",
            intent_name="BoredomBusterIntent",
            slot_to_elicit="MoodOrGenre",
            existing_slots={},
            session_attributes=session_attributes,
        )

    # Show the keyword search screen with personalized options
    media_type = session_attributes.get(SESSION_ATTR_MEDIA_TYPE) or ""
    document, datasources = apl.build_keyword_search_document(media_type, user_id)

    speech_prompt = "Tap one of the suggestions or say your specific search."
    if media_type == "movie":
        speech_prompt = "Tap one of the suggestions or say your specific movie search, like 'spiderman movies' or 'marvel movies'."
    elif media_type == "tv":
        speech_prompt = "Tap one of the suggestions or say your specific TV show search, like 'marvel tv shows' or 'crime dramas'."

    # Use elicit slot response to keep the session open and actively listen for voice input
    response = build_apl_directive_response(
        speech_text=speech_prompt,
        document=document,
        datasources=datasources,
        token=apl.APL_TOKEN_CLARIFY,
        session_attributes={
            SESSION_ATTR_MEDIA_TYPE: media_type,
            SESSION_ATTR_REGION: session_attributes.get(SESSION_ATTR_REGION) or "AU",
        },
    )

    # Manually set shouldEndSession to False and add elicitSlot directive to actively listen for voice input
    response["response"]["shouldEndSession"] = False
    if "directives" not in response["response"]:
        response["response"]["directives"] = []

    # Add Dialog.ElicitSlot directive to actively listen for MoodOrGenre slot
    response["response"]["directives"].append({
        "type": "Dialog.ElicitSlot",
        "slotToElicit": "MoodOrGenre",
        "updatedIntent": {
            "name": "BoredomBusterIntent",
            "confirmationStatus": "NONE",
            "slots": {
                "MoodOrGenre": {
                    "name": "MoodOrGenre",
                    "confirmationStatus": "NONE",
                    "value": None,
                },
                "MediaType": {
                    "name": "MediaType",
                    "confirmationStatus": "NONE",
                    "value": media_type,
                    "resolutions": {
                        "resolutionsPerAuthority": [
                            {
                                "status": {"code": "ER_SUCCESS_MATCH"},
                                "values": [
                                    {"value": {"id": "MOVIE" if media_type == "movie" else "TV", "name": media_type}}
                                ],
                            }
                        ]
                    } if media_type else None,
                }
            }
        }
    })

    return response


def handle_choose_mood(event: dict, arguments: list, user_id: str, session_attributes: dict) -> dict:
    """
    Handles the chooseMood UserEvent -- the user tapped one of the mood
    chips on Boredom Buster's clarification screen (utils/apl.py's
    build_clarification_document) instead of answering by voice.

    Treated exactly as if they'd spoken that answer: the chip's value
    ("something funny") is sent to the agent as the MoodOrGenre slot,
    combined with whatever MediaType the earlier turn already
    established (carried in SESSION_ATTR_MEDIA_TYPE, since a UserEvent
    carries no Alexa slots of its own). So a user who said "recommend a
    movie" and then tapped "Funny" gets funny MOVIES rather than being
    asked movie-or-TV again.

    Special case: if the mood value is "SHOW_KEYWORD_OPTIONS", this
    shows the keyword search screen instead of making an agent call --
    see build_show_keyword_options_response, shared with the voice
    equivalent of this same action in handler.py's _handle_intent_request.

    Goes through the allowlist/rate-limit pipeline because this makes a
    real AgentCore call, unlike the purely navigational
    viewRecommendation/backToRecommendations events.
    """
    mood = arguments[1] if len(arguments) > 1 else ""
    if not mood:
        logger.warning("chooseMood UserEvent missing mood argument: %s", arguments)
        return build_error_response()

    # Special case: "Specific Search" option shows the keyword search screen
    if mood == "SHOW_KEYWORD_OPTIONS":
        return build_show_keyword_options_response(event, user_id, session_attributes)

    if not user_id:
        logger.error("chooseMood UserEvent missing session.user.userId -- cannot proceed")
        return build_error_response()

    user = check_allowlist(user_id)
    if user is None:
        logger.info("Call denied: userId=%s intent=BoredomBusterIntent reason=not_approved", user_id)
        emit_denial_metric(user_id, "not_approved")
        return build_not_authorized_response()

    try:
        check_and_increment(user)
    except RateLimitExceeded as e:
        logger.info("Call denied: userId=%s intent=BoredomBusterIntent reason=%s", user_id, e.reason)
        emit_denial_metric(user_id, e.reason)
        return build_rate_limited_response(e.reason)

    media_type = session_attributes.get(SESSION_ATTR_MEDIA_TYPE) or ""
    slots = {"MoodOrGenre": {"name": "MoodOrGenre", "value": mood}}
    if media_type:
        # Rebuilt as a resolved slot so router.build_query_text's normal
        # MoodOrGenre + MediaType combination applies unchanged, rather
        # than this path assembling the agent's input string itself.
        slots["MediaType"] = {
            "name": "MediaType",
            "value": media_type,
            "resolutions": {
                "resolutionsPerAuthority": [
                    {
                        "status": {"code": "ER_SUCCESS_MATCH"},
                        "values": [
                            {"value": {"id": "MOVIE" if media_type == "movie" else "TV", "name": media_type}}
                        ],
                    }
                ]
            },
        }

    session_id = event.get("session", {}).get("sessionId", "")
    start_time = time.time()
    try:
        from router import route_request

        # Get user's region for streaming availability. Auto-detected from Alexa's locale; stored value overrides.
        user_region = resolve_region(session_attributes, event.get("request", {}).get("locale"))

        result = route_request(
            intent_name="BoredomBusterIntent",
            slots=slots,
            user_id=user_id,
            session_id=session_id,
            region=user_region,
        )
        latency_ms = (time.time() - start_time) * 1000
        log_successful_call(user_id, "BoredomBusterIntent", result, latency_ms)

        if result.recommendations and apl.supports_apl(event):
            document, datasources = apl.build_recommendations_document(result.recommendations, media_type, user_id, mood)
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

        # No recommendations and no APL support -- preserve media_type so
        # the next turn still knows which library to use.  build_speech_response
        # doesn't accept session_attributes, so build the envelope manually.
        return {
            "version": "1.0",
            "sessionAttributes": {
                SESSION_ATTR_MEDIA_TYPE: media_type,
                SESSION_ATTR_REGION: user_region,
            },
            "response": {
                "outputSpeech": {
                    "type": "PlainText",
                    "text": result.response_text,
                },
                "card": {
                    "type": "Simple",
                    "title": "Jarvis AI",
                    "content": result.response_text,
                },
                "shouldEndSession": False,
            },
        }

    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.time() - start_time) * 1000
        log_error(f"Error handling chooseMood (userId={user_id}, mood={mood})", exc)
        log_failed_call(user_id, "BoredomBusterIntent", latency_ms, exc)
        return build_error_response()


def handle_back_to_mood_selection(event: dict, user_id: str, session_attributes: dict) -> dict:
    """
    Handles the backToMoodSelection UserEvent -- the user tapped the "Back"
    button on the keyword search screen to return to the mood selection screen.

    This is a pure navigation action that doesn't make an agent call, so it
    doesn't go through the allowlist/rate-limit pipeline.
    """
    if not apl.supports_apl(event):
        # Device doesn't support APL, fall back to voice prompt.
        return build_elicit_slot_response(
            speech_text="What are you in the mood for -- something funny, something thrilling, something feel-good, surprise me, or search for something specific?",
            intent_name="BoredomBusterIntent",
            slot_to_elicit="MoodOrGenre",
            existing_slots={},
            session_attributes=session_attributes,
        )

    # Show the mood selection screen
    media_type = session_attributes.get(SESSION_ATTR_MEDIA_TYPE) or ""
    question = "What are you in the mood for -- something funny, something thrilling, something feel-good, surprise me, or search for something specific?"
    document, datasources = apl.build_clarification_document(question, media_type=media_type)

    return build_apl_directive_response(
        speech_text=question,
        document=document,
        datasources=datasources,
        token=apl.APL_TOKEN_CLARIFY,
        session_attributes={
            SESSION_ATTR_MEDIA_TYPE: media_type,
            SESSION_ATTR_REGION: session_attributes.get(SESSION_ATTR_REGION) or "AU",
        },
    )


def handle_record_feedback(
    event: dict, arguments: list, user_id: str, session_attributes: dict, recommendations: list
) -> dict:
    """
    Handles the recordFeedback UserEvent (like/dislike button press on
    Boredom Buster's detail screen). Unlike pure navigation, this makes
    a real AgentCore call -- the feedback is recorded via the agent's
    own record_feedback tool (solution-design.md Section 12.2), reusing
    the exact same natural-language-driven flow a spoken "I liked it"
    would trigger, rather than adding a second, agent-bypassing
    feedback-recording code path. So this goes through the same
    allowlist/rate-limit pipeline as any other Bedrock/AgentCore call.
    """
    sentiment = arguments[1] if len(arguments) > 1 else None
    if sentiment not in ("liked", "disliked"):
        logger.warning("recordFeedback UserEvent missing/invalid sentiment argument: %s", arguments)
        return build_error_response()

    current_index = session_attributes.get(SESSION_ATTR_CURRENT_INDEX)
    if current_index is None or not (0 <= current_index < len(recommendations)):
        logger.warning("recordFeedback UserEvent with no current recommendation in session")
        return build_error_response()

    title = recommendations[current_index].get("title", "that title")

    if not user_id:
        logger.error("recordFeedback UserEvent missing session.user.userId -- cannot proceed")
        return build_error_response()

    user = check_allowlist(user_id)
    if user is None:
        logger.info("Call denied: userId=%s intent=BoredomBusterFeedback reason=not_approved", user_id)
        emit_denial_metric(user_id, "not_approved")
        return build_not_authorized_response()

    try:
        check_and_increment(user)
    except RateLimitExceeded as e:
        logger.info("Call denied: userId=%s intent=BoredomBusterFeedback reason=%s", user_id, e.reason)
        emit_denial_metric(user_id, e.reason)
        return build_rate_limited_response(e.reason)

    session_id = event.get("session", {}).get("sessionId", "")
    synthetic_message = f"I {sentiment} {title}."

    start_time = time.time()
    try:
        from router import route_request

        result = route_request(
            intent_name="BoredomBusterIntent",
            slots={"MoodOrGenre": {"name": "MoodOrGenre", "value": synthetic_message}},
            user_id=user_id,
            session_id=session_id,
        )
        latency_ms = (time.time() - start_time) * 1000
        log_successful_call(user_id, "BoredomBusterFeedback", result, latency_ms)

        verb = "Got it, I've noted that you liked" if sentiment == "liked" else "Got it, noted that you're not interested in"
        speech_text = f"{verb} {title}."

        # Re-render the SAME detail screen the button was pressed on,
        # rather than replacing it with the generic message screen. A
        # like/dislike is an aside, not a navigation step -- yanking the
        # poster and synopsis away because the user tapped a thumbs-up
        # would lose their place for no reason.
        if apl.supports_apl(event):
            document, datasources = apl.build_detail_document(recommendations[current_index])
            return build_apl_directive_response(
                speech_text=speech_text,
                document=document,
                datasources=datasources,
                token=apl.APL_TOKEN_DETAIL,
                session_attributes={
                    SESSION_ATTR_RECOMMENDATIONS: recommendations,
                    SESSION_ATTR_CURRENT_INDEX: current_index,
                    SESSION_ATTR_MEDIA_TYPE: session_attributes.get(SESSION_ATTR_MEDIA_TYPE, ""),
                    SESSION_ATTR_REGION: session_attributes.get(SESSION_ATTR_REGION, "AU"),
                },
            )

        return build_speech_response(speech_text=speech_text, should_end_session=False)

    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.time() - start_time) * 1000
        log_error(f"Error recording feedback (userId={user_id}, title={title})", exc)
        log_failed_call(user_id, "BoredomBusterFeedback", latency_ms, exc)
        return build_error_response()
