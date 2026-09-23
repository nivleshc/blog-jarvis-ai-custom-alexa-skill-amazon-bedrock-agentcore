"""
Smart Assistant's askFollowup UserEvent handler -- tapping a follow-up
suggestion chip on the Smart Assistant response screen.

The IntentRequest side of Smart Assistant (SmartAssistantIntent itself)
lives in handler.py's _handle_intent_request, since it shares the main
allowlist/rate-limit/routing pipeline with every other intent. This
module only covers the touch (APL UserEvent) follow-up path.
"""

import logging
import time

from allowlist import check_allowlist
from rate_limit import RateLimitExceeded, check_and_increment
from utils import apl
from utils.logging_config import emit_denial_metric, log_error
from utils.response import (
    build_apl_directive_response,
    build_error_response,
    build_not_authorized_response,
    build_rate_limited_response,
    build_speech_response,
)

from .common import condense_ask_bedrock_response, log_failed_call, log_successful_call, strip_markdown

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handle_ask_followup(event: dict, arguments: list, user_id: str, session_attributes: dict) -> dict:
    """
    Handles the askFollowup UserEvent -- user tapped a follow-up suggestion chip
    on the Smart Assistant response screen.

    Args:
        event: The full Alexa event
        arguments: UserEvent arguments [event_type, question_text]
        user_id: The user's ID
        session_attributes: Current session attributes

    Returns:
        Alexa response with the answer to the follow-up question
    """
    if len(arguments) < 2:
        logger.warning("askFollowup UserEvent missing question argument: %s", arguments)
        return build_error_response()

    question = arguments[1]
    if not question:
        logger.warning("askFollowup UserEvent with empty question")
        return build_error_response()

    if not user_id:
        logger.error("askFollowup UserEvent missing session.user.userId -- cannot proceed")
        return build_error_response()

    # Check allowlist and rate limit
    user = check_allowlist(user_id)
    if user is None:
        logger.info("Call denied: userId=%s intent=SmartAssistantIntent reason=not_approved", user_id)
        emit_denial_metric(user_id, "not_approved")
        return build_not_authorized_response()

    try:
        check_and_increment(user)
    except RateLimitExceeded as e:
        logger.info("Call denied: userId=%s intent=SmartAssistantIntent reason=%s", user_id, e.reason)
        emit_denial_metric(user_id, e.reason)
        return build_rate_limited_response(e.reason)

    # Route the follow-up question through SmartAssistantIntent
    session_id = event.get("session", {}).get("sessionId", "")
    start_time = time.time()

    try:
        from router import route_request

        result = route_request(
            intent_name="SmartAssistantIntent",
            slots={"Query": {"name": "Query", "value": question}},
            user_id=user_id,
            session_id=session_id,
        )
        latency_ms = (time.time() - start_time) * 1000
        log_successful_call(user_id, "SmartAssistantIntent", result, latency_ms)

        # Build Smart Assistant response with APL
        speech_text = condense_ask_bedrock_response(result.response_text)

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

        # Fallback for non-APL devices
        return build_speech_response(
            speech_text=speech_text,
            card_title="Smart Assistant",
            card_content=speech_text,
            should_end_session=False,
        )

    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.time() - start_time) * 1000
        log_error(f"Error handling askFollowup (userId={user_id}, question={question})", exc)
        log_failed_call(user_id, "SmartAssistantIntent", latency_ms, exc)
        return build_error_response()
