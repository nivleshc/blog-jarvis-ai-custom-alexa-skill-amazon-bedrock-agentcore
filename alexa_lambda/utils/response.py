"""
Alexa response envelope builders.

Kept deliberately dumb -- these functions only shape a dict into the JSON
structure Alexa expects. No business logic (allowlist checks, rate
limiting, Bedrock calls) lives here. See solution-design.md Section 4 for
where each response type is used in the request pipeline.
"""

from typing import Optional


def build_speech_response(
    speech_text: str,
    card_title: Optional[str] = None,
    card_content: Optional[str] = None,
    should_end_session: bool = True,
) -> dict:
    """
    Build a standard Alexa response: spoken text, plus an optional simple
    card for Echo Show's screen.
    """
    response: dict = {
        "version": "1.0",
        "response": {
            "outputSpeech": {
                "type": "PlainText",
                "text": speech_text,
            },
            "shouldEndSession": should_end_session,
        },
    }

    if card_title is not None:
        response["response"]["card"] = {
            "type": "Simple",
            "title": card_title,
            "content": card_content if card_content is not None else speech_text,
        }

    return response


def build_not_authorized_response() -> dict:
    """
    Returned by the allowlist check (pipeline step 2) when a user is not
    found or not yet approved. See solution-design.md Section 4, step 2.
    """
    return build_speech_response(
        speech_text=(
            "You're not authorized to use this skill yet. "
            "The developer has been notified and will review your request."
        ),
        card_title="Access Pending",
        card_content="Your request to use this skill is pending developer approval.",
    )


def build_rate_limited_response(reason: str) -> dict:
    """
    Returned by the rate-limit check (pipeline step 3). `reason` is one of
    the DenialReason values from solution-design.md Section 8.2:
    daily_limit, burst_limit, global_ceiling.
    """
    messages = {
        "daily_limit": "You've reached your daily usage limit for this skill. Please try again tomorrow.",
        "burst_limit": "You're asking questions a bit too quickly. Please wait a few minutes and try again.",
        "global_ceiling": "This skill is experiencing high demand right now. Please try again later.",
    }
    speech_text = messages.get(
        reason, "You've reached a usage limit for this skill. Please try again later."
    )
    return build_speech_response(
        speech_text=speech_text,
        card_title="Usage Limit Reached",
        card_content=speech_text,
    )


def build_input_too_long_response() -> dict:
    """Returned by the input length check (pipeline step 4)."""
    return build_speech_response(
        speech_text="That question is a bit long. Could you ask something shorter?",
        card_title="Question Too Long",
        card_content="Please ask a shorter question.",
    )


def build_error_response() -> dict:
    """Generic fallback for unexpected errors, so the skill never leaves
    the user with no response at all.

    Sets should_end_session=False so the session stays open, allowing
    the user to tap "Main Menu" on the error screen to navigate back.
    """
    return build_speech_response(
        speech_text="Sorry, something went wrong. Please try again in a moment.",
        card_title="Error",
        card_content="An unexpected error occurred.",
        should_end_session=False,
    )


def build_launch_response(enabled_task_phrases: Optional[list] = None) -> dict:
    """
    Returned for a bare LaunchRequest (user said 'open bedrock
    assistant') and for AMAZON.HelpIntent/AMAZON.NavigateHomeIntent.

    `enabled_task_phrases`, when provided, is used to build the welcome
    message dynamically from whatever tasks are actually enabled in the
    task registry (see handlers/handler.py's _build_welcome_speech_text
    and task_registry.is_task_enabled) -- e.g. ["ask me to check the
    weather"]. Falls back to a generic message with no task examples if
    the list is empty/None, rather than a hardcoded set of task names
    that could drift out of sync with the registry (the real bug this
    fixes: the welcome message used to advertise "take a note" and
    "search for information," neither of which had a real agent behind
    it, so both failed immediately if a user tried them).
    """
    if enabled_task_phrases:
        tasks_clause = " or ".join(enabled_task_phrases)
        speech_text = f"Welcome to Jarvis AI. You can ask me a general question, or {tasks_clause}."
    else:
        speech_text = "Welcome to Jarvis AI. You can ask me a general question."

    return build_speech_response(
        speech_text=speech_text,
        card_title="Jarvis AI",
        card_content=speech_text,
        should_end_session=False,
    )


def build_elicit_slot_response(
    speech_text: str,
    intent_name: str,
    slot_to_elicit: str,
    existing_slots: dict,
    apl_document: Optional[dict] = None,
    apl_datasources: Optional[dict] = None,
    apl_token: Optional[str] = None,
    session_attributes: Optional[dict] = None,
) -> dict:
    """
    Builds a Dialog.ElicitSlot directive response -- asks the user for
    a specific slot value again, keeping the session open and the
    conversation continuing on the SAME intent, rather than ending the
    session with a terminal error message. See Amazon's own Dialog
    Interface Reference: "You must include the prompt to ask the user
    for the slot value in the OutputSpeech object. The directive does
    not use the prompts defined in the dialog model" for this manual-
    control path (distinct from GetWeatherIntent's existing dialog
    model, which handles the FIRST elicitation automatically via
    `delegationStrategy: ALWAYS` -- this directive is for the SECOND
    kind of "missing slot," where a value WAS given but turned out to
    be unresolvable, e.g. a location that doesn't exist. Real bug this
    fixes: a location Open-Meteo couldn't geocode used to end the
    conversation outright instead of giving the user a chance to
    correct it).

    `existing_slots` must include every slot the intent defines (per
    Amazon's own docs: "when you do set slot values on the new intent,
    be sure to include all slots defined in the interaction model for
    the intent, even those you want to leave empty"), with the
    slot-to-elicit's own value cleared to None so Alexa doesn't just
    re-use the same unresolvable value it already rejected. Note that
    every OTHER slot's value IS preserved -- that's what stops
    BoredomBusterIntent from forgetting an already-supplied MediaType
    ("recommend a movie") while it elicits MoodOrGenre, which would
    otherwise put the user right back to being asked movie-or-TV again.

    `apl_document`/`apl_datasources`/`apl_token`, when all provided, add
    an Alexa.Presentation.APL.RenderDocument directive ALONGSIDE the
    Dialog.ElicitSlot directive (a response's `directives` array can
    carry both). Boredom Buster uses this so a clarifying question shows
    tappable answer chips on screen while still eliciting the slot by
    voice -- see utils/apl.py's build_clarification_document.
    """
    updated_slots = {}
    for slot_name, slot_data in existing_slots.items():
        updated_slots[slot_name] = {
            "name": slot_name,
            "confirmationStatus": "NONE",
            **({"value": slot_data.get("value")} if slot_name != slot_to_elicit else {}),
        }
    if slot_to_elicit not in updated_slots:
        updated_slots[slot_to_elicit] = {"name": slot_to_elicit, "confirmationStatus": "NONE"}

    directives: list = [
        {
            "type": "Dialog.ElicitSlot",
            "slotToElicit": slot_to_elicit,
            "updatedIntent": {
                "name": intent_name,
                "confirmationStatus": "NONE",
                "slots": updated_slots,
            },
        }
    ]

    if apl_document is not None and apl_token is not None:
        directives.append(
            {
                "type": "Alexa.Presentation.APL.RenderDocument",
                "token": apl_token,
                "document": apl_document,
                "datasources": apl_datasources or {},
            }
        )

    response: dict = {
        "version": "1.0",
        "response": {
            "outputSpeech": {
                "type": "PlainText",
                "text": speech_text,
            },
            "shouldEndSession": False,
            "directives": directives,
        },
    }

    if session_attributes is not None:
        response["sessionAttributes"] = session_attributes

    return response


def build_apl_directive_response(
    speech_text: str,
    document: dict,
    datasources: dict,
    token: str,
    session_attributes: Optional[dict] = None,
    should_end_session: bool = False,
) -> dict:
    """
    Builds a response combining spoken output with an
    Alexa.Presentation.APL.RenderDocument directive -- used only by
    Boredom Buster's recommendations grid and detail screens (solution-
    design.md Section 13). No Simple Card is added here (unlike
    build_speech_response) since the APL document itself is the visual
    content; a Simple Card would be redundant/conflicting on a device
    that's already rendering the APL document.

    `session_attributes`, when provided, are echoed back in the response
    so Alexa persists them for the next request in this session --
    Boredom Buster uses this to store the current recommendation list
    across the grid/detail screen navigation (see handlers/handler.py's
    UserEvent handling), avoiding a second agent call just to redisplay
    data the user already saw.
    """
    response: dict = {
        "version": "1.0",
        "response": {
            "outputSpeech": {
                "type": "PlainText",
                "text": speech_text,
            },
            "directives": [
                {
                    "type": "Alexa.Presentation.APL.RenderDocument",
                    "token": token,
                    "document": document,
                    "datasources": datasources,
                }
            ],
            "shouldEndSession": should_end_session,
        },
    }

    if session_attributes is not None:
        response["sessionAttributes"] = session_attributes

    return response
