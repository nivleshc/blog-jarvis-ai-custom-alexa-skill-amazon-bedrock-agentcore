"""
Launch screen -- the capability-card grid shown on LaunchRequest,
AMAZON.HelpIntent, AMAZON.NavigateHomeIntent, and ReturnToMainMenuIntent,
plus the launchAction UserEvent handler for tapping one of those cards
and the generic APL "message screen" post-processing step that gives
every other intent's response a real on-screen presence.
"""

import logging
import time

from allowlist import check_allowlist
from rate_limit import RateLimitExceeded, check_and_increment
from task_registry import get_task_registry, is_task_enabled
from utils import apl
from utils.logging_config import emit_denial_metric, log_error
from utils.response import (
    build_apl_directive_response,
    build_elicit_slot_response,
    build_error_response,
    build_launch_response,
    build_not_authorized_response,
    build_rate_limited_response,
    build_speech_response,
)

from .common import (
    INTENT_TO_CLARIFICATION_SLOT,
    SESSION_ATTR_MEDIA_TYPE,
    SESSION_ATTR_RECOMMENDATIONS,
    SESSION_ATTR_REGION,
    SESSION_ATTR_LAST_SCREEN,
    build_enabled_task_phrases,
    log_failed_call,
    log_successful_call,
    resolve_region,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def build_launch_response_with_cards() -> dict:
    """
    Builds the launch response with tappable capability cards for APL devices,
    falling back to the standard speech response for non-APL devices.

    Returns an APL RenderDocument directive with capability cards on Echo Show,
    or a simple speech response with card on other devices.
    """
    # Build capability cards for enabled tasks
    capabilities = []

    # Ask Bedrock (always available) - uses SmartAssistantIntent
    capabilities.append({
        "icon": "💬",
        "label": "Smart Assistant",
        "value": "smart_assistant",
        "accentColor": "#1C8C7C",  # Teal - chat
    })

    # Try to get registry, but don't fail if it doesn't work
    registry = None
    try:
        registry = get_task_registry()
    except Exception:  # noqa: BLE001
        logger.warning("Failed to fetch task registry for launch screen - using defaults")

    # Check if GetWeather is enabled
    if registry and registry.get("GetWeather") and is_task_enabled(registry["GetWeather"]):
        capabilities.append({
            "icon": "⛅",
            "label": "Check Weather",
            "value": "check_weather",
            "accentColor": "#2C6E9E",  # Blue - weather
        })

    # Check if BoredomBuster is enabled - split into Movie and TV cards
    if registry and registry.get("BoredomBuster") and is_task_enabled(registry["BoredomBuster"]):
        capabilities.append({
            "icon": "🎬",
            "label": "Movie Ideas",
            "value": "recommend_movie",
            "accentColor": "#7B3FA0",  # Purple - movies
        })
        capabilities.append({
            "icon": "📺",
            "label": "TV Show Ideas",
            "value": "recommend_tv",
            "accentColor": "#E86C00",  # Orange - TV
        })

    # Add Watchlist card (always available for approved users)
    capabilities.append({
        "icon": "📋",
        "label": "My Watchlist",
        "value": "view_watchlist",
        "accentColor": "#2C5F8A",  # Blue - watchlist
    })

    # If device supports APL, show the card grid
    # Note: we check APL support in add_apl_message_directive_if_supported
    # which is called after _dispatch returns. So we always build the APL
    # document here and let the post-processor handle device capability.
    if capabilities:
        document, datasources = apl.build_launch_document(capabilities)
        return build_apl_directive_response(
            speech_text="Welcome to Jarvis AI. Tap a card to get started, or just ask me anything.",
            document=document,
            datasources=datasources,
            token=apl.APL_TOKEN_MESSAGE,
            should_end_session=False,
        )
    else:
        # This should never happen - we always add at least Ask Bedrock + Watchlist
        # But just in case, fallback to voice-only welcome
        enabled_phrases = build_enabled_task_phrases()
        return build_launch_response(enabled_phrases)


def pick_icon_and_accent(event: dict, resp_body: dict) -> tuple:
    """
    Chooses an emoji icon + accent color for the generic message
    screen (apl.build_message_document), based on the request's intent
    name and the response's own shape -- gives the screen visual
    distinction between "weather," "an error/denial," and "a general
    answer" instead of every response looking identical apart from the
    words.

    Deliberately simple, intent-name-based selection rather than
    inspecting response_text for keywords -- robust regardless of
    exactly what was said, and trivial to extend (see apl.py's
    ICON_* constants) if a future task wants its own icon.
    """
    # A re-prompt for a missing/invalid slot (Dialog.ElicitSlot) is a
    # normal, recoverable step of the conversation, not a failure --
    # falls through to the owning intent's normal icon below, same as
    # any other response from that intent.
    intent_name = event.get("request", {}).get("intent", {}).get("name", "")
    if intent_name == "GetWeatherIntent":
        return apl.ICON_WEATHER
    if intent_name == "BoredomBusterIntent":
        return apl.ICON_MOVIE
    if intent_name == "SmartAssistantIntent":
        return apl.ICON_CHAT
    if intent_name in ("DetailScreenActionIntent", "ReturnToMainMenuIntent"):
        return apl.ICON_DEFAULT

    # Denial/error responses (not authorized, rate-limited, input too
    # long, unhandled exception, fallback-intent "not sure how to help")
    # have no real intent name of their own to key off -- these are
    # exactly the built-in/pipeline-rejection paths in
    # handler.py's _handle_intent_request that never reach
    # router.route_request(), so a shared "alert" icon distinguishes
    # them from a normal answer.
    speech_text = (resp_body.get("outputSpeech", {}) or {}).get("text", "").lower()
    denial_phrases = ("not authorized", "usage limit", "asking questions a bit too quickly", "question is a bit long", "went wrong", "not sure how to help")
    if any(phrase in speech_text for phrase in denial_phrases):
        return apl.ICON_ALERT

    return apl.ICON_DEFAULT


def add_apl_message_directive_if_supported(event: dict, response: dict) -> dict:
    """
    Adds a generic APL "message screen" directive to `response` when the
    requesting device supports APL and the response doesn't already
    have a directive of its own (Boredom Buster's recommendations/
    detail screens, built separately, are left untouched).

    A Simple Card (Alexa's other card option) only renders in the Alexa
    app, never on the Echo Show's physical screen -- an APL
    RenderDocument directive is the only mechanism that updates the
    device display. This generic screen ensures every intent's response
    actually shows on screen, without every existing response-building
    branch needing to be individually rewritten to build its own APL
    document.

    Skipped entirely for a response with no spoken text at all (e.g.
    SessionEndedRequest's empty-string speech) -- there's nothing
    meaningful to show on screen for that case.
    """
    if not apl.supports_apl(event):
        return response

    resp_body = response.get("response", {})
    existing_directives = resp_body.get("directives", []) or []
    if any(d.get("type") == "Alexa.Presentation.APL.RenderDocument" for d in existing_directives):
        # Already has its own APL RenderDocument directive (Boredom
        # Buster's grid/detail screens) -- never override it with the
        # generic message screen.
        return response

    speech_text = resp_body.get("outputSpeech", {}).get("text", "")
    if not speech_text:
        return response

    card = resp_body.get("card", {}) or {}
    title = card.get("title") or "Jarvis AI"
    body = card.get("content") or speech_text

    icon, accent_color = pick_icon_and_accent(event, resp_body)
    document, datasources = apl.build_message_document(title, body, icon=icon, accent_color=accent_color)
    resp_body["directives"] = [
        *existing_directives,
        {
            "type": "Alexa.Presentation.APL.RenderDocument",
            "token": apl.APL_TOKEN_MESSAGE,
            "document": document,
            "datasources": datasources,
        },
    ]
    return response


def handle_launch_action(event: dict, arguments: list, user_id: str) -> dict:
    """
    Handles the launchAction UserEvent -- user tapped a capability card
    on the launch screen.

    `arguments[1]` is the action value: "smart_assistant", "check_weather", "recommend_movie", "recommend_tv", "go_home"
    """
    action = arguments[1] if len(arguments) > 1 else ""
    if not action:
        logger.warning("launchAction UserEvent missing action argument: %s", arguments)
        return build_error_response()

    if not user_id:
        logger.error("launchAction UserEvent missing session.user.userId -- cannot proceed")
        return build_error_response()

    # Initialize session attributes to ensure it's always defined
    session_attributes = {}
    # Extract session attributes for maintaining context across turns if available
    session = event.get("session", {})
    if session:
        session_attributes = session.get("attributes", {}) or {}

    # Handle "go_home" action - return to main launch screen
    if action == "go_home":
        return build_launch_response_with_cards()

    # Map action to intent and pre-filled slots
    intent_map = {
        "smart_assistant": ("SmartAssistantIntent", {}),
        "check_weather": ("GetWeatherIntent", {}),
        "recommend_movie": ("BoredomBusterIntent", {"MediaType": {"name": "MediaType", "value": "movie", "resolutions": {"resolutionsPerAuthority": [{"status": {"code": "ER_SUCCESS_MATCH"}, "values": [{"value": {"id": "MOVIE", "name": "movie"}}]}]}}}),
        "recommend_tv": ("BoredomBusterIntent", {"MediaType": {"name": "MediaType", "value": "tv", "resolutions": {"resolutionsPerAuthority": [{"status": {"code": "ER_SUCCESS_MATCH"}, "values": [{"value": {"id": "TV", "name": "tv"}}]}]}}}),
        "view_watchlist": ("ViewWatchlistIntent", {}),
    }

    intent_name, prefilled_slots = intent_map.get(action, (None, None))
    if not intent_name:
        logger.warning("Unknown launchAction: %s", action)
        return build_error_response()

    # For SmartAssistantIntent, we need a question. Use Dialog.ElicitSlot to
    # ensure the follow-up utterance gets routed back to SmartAssistantIntent
    # instead of falling through to FallbackIntent.
    if intent_name == "SmartAssistantIntent":
        return build_elicit_slot_response(
            speech_text="Sure, what would you like to ask me?",
            intent_name="SmartAssistantIntent",
            slot_to_elicit="Query",
            existing_slots={},
        )

    # For ViewWatchlistIntent, show the watchlist screen
    if intent_name == "ViewWatchlistIntent":
        from .watchlist import handle_view_watchlist
        return handle_view_watchlist(event, user_id)

    # For GetWeatherIntent and BoredomBusterIntent, we go through the
    # normal pipeline (allowlist/rate-limit/routing). They will ask for
    # clarification if needed (location for weather, mood for movies).
    session_id = event.get("session", {}).get("sessionId", "")
    start_time = time.time()

    user = check_allowlist(user_id)
    if user is None:
        logger.info("Call denied: userId=%s intent=%s reason=not_approved", user_id, intent_name)
        emit_denial_metric(user_id, "not_approved")
        return build_not_authorized_response()

    try:
        check_and_increment(user)
    except RateLimitExceeded as e:
        logger.info("Call denied: userId=%s intent=%s reason=%s", user_id, intent_name, e.reason)
        emit_denial_metric(user_id, e.reason)
        return build_rate_limited_response(e.reason)

    # OPTIMIZATION: For BoredomBusterIntent launched from "Movie Ideas" or "TV Show Ideas"
    # cards, the media_type is pre-filled but NO mood is provided yet. Instead of making
    # a full AgentCore call just to have the agent ask "what mood?", we can show the
    # clarification screen immediately. This makes the transition from launch screen
    # to mood selection instant.
    if intent_name == "BoredomBusterIntent" and prefilled_slots and "MediaType" in prefilled_slots:
        mood_slot = prefilled_slots.get("MoodOrGenre")
        if not mood_slot or not mood_slot.get("value"):
            # No mood provided yet - show clarification screen instantly
            media_type_from_slots = prefilled_slots["MediaType"].get("value", "")
            # Get user's region for streaming availability (auto-detected from Alexa locale).
            user_region = resolve_region(session_attributes, event.get("request", {}).get("locale"))
            if apl.supports_apl(event):
                # Wording must stay in sync with agent.py's SYSTEM_PROMPT/empty-input
                # response and _handle_back_to_mood_selection's question -- all three
                # speak the CLARIFY_MOOD_OPTIONS "Specific Search" chip rendered on
                # this same screen, so the spoken prompt must mention it too.
                if media_type_from_slots == "movie":
                    clarifying_question = "What kind of movie are you in the mood for? Something funny, something thrilling, something feel-good, surprise me, or search for something specific?"
                elif media_type_from_slots == "tv":
                    clarifying_question = "What kind of TV show are you in the mood for? Something funny, something thrilling, something feel-good, surprise me, or search for something specific?"
                else:
                    clarifying_question = "What are you in the mood for -- something funny, something thrilling, something feel-good, surprise me, or search for something specific?"

                apl_document, apl_datasources = apl.build_clarification_document(clarifying_question, media_type=media_type_from_slots)
                apl_token = apl.APL_TOKEN_CLARIFY
                # Region must be included in session_attributes so it persists once the
                # user taps a mood chip. actual_media_type preserves the resolved media
                # type ("movie"/"tv") rather than falling back to an empty string when
                # media_type_from_slots is unset but the MediaType slot itself is present.
                actual_media_type = media_type_from_slots if media_type_from_slots else prefilled_slots["MediaType"].get("value", "")

                updated_session_attributes = {
                    SESSION_ATTR_MEDIA_TYPE: actual_media_type,
                    SESSION_ATTR_REGION: user_region,
                }
                # Preserve any existing session attributes
                for key, value in session_attributes.items():
                    if key not in updated_session_attributes:
                        updated_session_attributes[key] = value
                session_attributes = updated_session_attributes
                existing_slots_for_elicitation = {"MediaType": prefilled_slots["MediaType"]}

                return build_elicit_slot_response(
                    speech_text=clarifying_question,
                    intent_name=intent_name,
                    slot_to_elicit="MoodOrGenre",
                    existing_slots=existing_slots_for_elicitation,
                    apl_document=apl_document,
                    apl_datasources=apl_datasources,
                    apl_token=apl_token,
                    session_attributes=session_attributes,
                )
            # Non-APL device - just elicit slot by voice. Same wording fix as the
            # APL branch above -- keep in sync with agent.py's SYSTEM_PROMPT wording.
            if media_type_from_slots == "movie":
                speech_prompt = "What kind of movie are you in the mood for? Something funny, something thrilling, something feel-good, surprise me, or search for something specific?"
            elif media_type_from_slots == "tv":
                speech_prompt = "What kind of TV show are you in the mood for? Something funny, something thrilling, something feel-good, surprise me, or search for something specific?"
            else:
                speech_prompt = "What are you in the mood for -- something funny, something thrilling, something feel-good, surprise me, or search for something specific?"
            existing_slots_for_elicitation = {"MediaType": prefilled_slots["MediaType"]}
            return build_elicit_slot_response(
                speech_text=speech_prompt,
                intent_name=intent_name,
                slot_to_elicit="MoodOrGenre",
                existing_slots=existing_slots_for_elicitation,
            )

    try:
        from router import route_request

        # Get user's region for streaming availability (auto-detected from Alexa locale).
        user_region = resolve_region(session_attributes, event.get("request", {}).get("locale"))

        # Send with pre-filled MediaType slot for movie/TV - the agent
        # will only ask for mood since media type is already known
        result = route_request(
            intent_name=intent_name,
            slots=prefilled_slots,
            user_id=user_id,
            session_id=session_id,
            region=user_region,
        )
        latency_ms = (time.time() - start_time) * 1000
        log_successful_call(user_id, intent_name, result, latency_ms)

        # Extract media_type from prefilled slots if available
        media_type_from_slots = ""
        if prefilled_slots and "MediaType" in prefilled_slots:
            media_type_from_slots = prefilled_slots["MediaType"].get("value", "")

        if result.recommendations and apl.supports_apl(event):
            document, datasources = apl.build_recommendations_document(result.recommendations, media_type_from_slots, user_id)
            return build_apl_directive_response(
                speech_text=result.response_text,
                document=document,
                datasources=datasources,
                token=apl.APL_TOKEN_RECOMMENDATIONS,
                session_attributes={
                    SESSION_ATTR_RECOMMENDATIONS: result.recommendations,
                    SESSION_ATTR_MEDIA_TYPE: media_type_from_slots,
                    SESSION_ATTR_REGION: user_region,
                    # Store screen state for return functionality
                    SESSION_ATTR_LAST_SCREEN: {
                        "type": "recommendations",
                        "recommendations": result.recommendations,
                        "media_type": media_type_from_slots,
                    },
                },
            )

        clarification_slot = INTENT_TO_CLARIFICATION_SLOT.get(intent_name)
        if result.needs_clarification and clarification_slot:
            apl_document = None
            apl_datasources = None
            apl_token = None
            # Start with existing session attributes and update as needed
            updated_session_attributes = {
                SESSION_ATTR_MEDIA_TYPE: session_attributes.get(SESSION_ATTR_MEDIA_TYPE, ""),
                SESSION_ATTR_REGION: session_attributes.get(SESSION_ATTR_REGION, "AU"),
            }
            existing_slots_for_elicitation = {}
            if prefilled_slots and "MediaType" in prefilled_slots:
                existing_slots_for_elicitation["MediaType"] = prefilled_slots["MediaType"]
            if intent_name == "BoredomBusterIntent" and apl.supports_apl(event):
                apl_document, apl_datasources = apl.build_clarification_document(result.response_text, media_type=media_type_from_slots)
                apl_token = apl.APL_TOKEN_CLARIFY
                # Preserve the media_type from the original launch action
                session_attributes[SESSION_ATTR_MEDIA_TYPE] = media_type_from_slots
            return build_elicit_slot_response(
                speech_text=result.response_text,
                intent_name=intent_name,
                slot_to_elicit=clarification_slot,
                existing_slots=existing_slots_for_elicitation,
                apl_document=apl_document,
                apl_datasources=apl_datasources,
                apl_token=apl_token,
                session_attributes=session_attributes,
            )

        keep_session_open = intent_name == "BoredomBusterIntent"
        return build_speech_response(
            speech_text=result.response_text,
            card_title="Jarvis AI",
            card_content=result.response_text,
            should_end_session=not keep_session_open,
        )

    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.time() - start_time) * 1000
        log_error(f"Error handling launchAction {action} (userId={user_id})", exc)
        log_failed_call(user_id, intent_name, latency_ms, exc)
        return build_error_response()
