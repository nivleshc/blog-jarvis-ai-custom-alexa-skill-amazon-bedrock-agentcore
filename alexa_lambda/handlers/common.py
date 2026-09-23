"""
Shared constants and helpers used across every handler module in this
package -- session attribute keys, region/locale resolution, markdown
stripping for on-screen display, response condensing for Smart
Assistant, and the shared call-logging/query-extraction helpers used by
both the main intent pipeline (handler.py) and the individual APL
UserEvent handlers (boredom_buster.py, watchlist.py, detail_actions.py,
smart_assistant.py).

Kept separate from handler.py so every other handler module can import
these without creating a circular import back into the dispatch shell.
"""

import logging
import re

from allowlist import record_successful_call
from call_log import write_call_log
from task_registry import get_task_registry, is_task_enabled
from utils.logging_config import emit_call_metric

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Session attribute key storing Boredom Buster's most recently shown
# recommendation list, so tapping a card (viewRecommendation) or the
# back button (backToRecommendations) can redisplay data the user
# already saw without another AgentCore call -- see solution-design.md
# Section 13.3.
SESSION_ATTR_RECOMMENDATIONS = "boredom_buster_recommendations"
# Session attribute key storing which recommendation (by index into the
# list above) is currently shown on the detail screen, so a like/dislike
# button press (which carries no title of its own) knows which title the
# feedback applies to.
SESSION_ATTR_CURRENT_INDEX = "boredom_buster_current_index"
# Session attribute key storing the media type ("movie" / "tv") the
# user already asked for, so tapping a mood chip on the clarification
# screen (a UserEvent, which carries no Alexa slots at all) can still
# tell the agent which of the two they wanted -- without this, tapping
# "Funny" after saying "recommend a movie" would lose the "movie" part
# and the agent would be back to guessing.
SESSION_ATTR_MEDIA_TYPE = "boredom_buster_media_type"
# Session attribute key storing the user's region for streaming availability.
# TMDB's watch/providers endpoint requires a country code -- see
# tmdb_client.py's DEFAULT_WATCH_REGION. Falls back to "AU" if not set.
SESSION_ATTR_REGION = "boredom_buster_region"
# Session attribute key storing the last screen state before user left via
# button press (OpenURL for trailer/streaming). Used to restore screen when
# user says "return" or "back to jarvis ai".
SESSION_ATTR_LAST_SCREEN = "last_screen_state"

MAX_INPUT_CHARS_DEFAULT = 800

# Maps intent names to the slot that should be re-elicited when
# RouteResult.needs_clarification is True -- GetWeatherIntent's task sets
# this for an unrecognized location (lambda_tasks/get_weather/handler.py),
# and BoredomBusterIntent's agent sets it whenever it asks a clarifying
# question instead of presenting recommendations
# (agents/boredom_buster_agent/agent.py). See handler.py's
# _handle_intent_request success path, which uses this to return a
# Dialog.ElicitSlot directive that biases Alexa's NLU to capture the
# user's next short reply into the named slot, keeping the conversation
# going rather than ending the session or re-asking the same fixed
# question with no way to recover.
INTENT_TO_CLARIFICATION_SLOT = {
    "GetWeatherIntent": "Location",
    "BoredomBusterIntent": "MoodOrGenre",
}

# Maps task registry keys to a short, spoken phrase describing that
# capability, used to build the welcome/fallback messages dynamically
# from whichever tasks are actually enabled -- see
# build_enabled_task_phrases's docstring for why this exists instead
# of a hardcoded sentence. Only covers tasks with a genuinely spoken
# invocation phrase; AskBedrock/BoredomBuster have their own dedicated
# phrasing below rather than appearing in this map (AskBedrock is the
# implicit "ask me a general question" already baked into
# build_launch_response's default text; BoredomBuster's phrase is
# deliberately more inviting than a dry task name).
_TASK_KEY_TO_SPOKEN_PHRASE = {
    "GetWeather": "ask me to check the weather",
    "BoredomBuster": "ask me to recommend a movie or TV show if you're bored",
    "ViewWatchlist": "ask me to show your watchlist",
}

# Regex patterns to strip model reasoning scaffolding from AskBedrock responses
_SCAFFOLDING_BLOCK_RE = re.compile(
    r"<\s*(thinking|thought|reasoning|scratchpad|answer)\s*>(.*?)(?:<\s*/\s*\1\s*>|$)",
    re.IGNORECASE | re.DOTALL,
)
_STRAY_TAG_RE = re.compile(r"<\s*/?\s*(thinking|thought|reasoning|scratchpad|answer)\s*>", re.IGNORECASE)


def build_enabled_task_phrases() -> list:
    """
    Returns the spoken phrases for every task in the registry that is
    both mapped to a known phrase (see _TASK_KEY_TO_SPOKEN_PHRASE) AND
    actually enabled (task_registry.is_task_enabled -- i.e. not still
    pointing at a literal placeholder ARN). Used by the welcome message
    (LaunchRequest/AMAZON.HelpIntent) and the AMAZON.FallbackIntent
    message, so neither one ever advertises a capability that isn't
    actually wired up to a real agent or function yet.

    A failure to reach the task registry (e.g. a transient S3 issue)
    degrades to an empty list -- build_launch_response's fallback text
    ("ask me a general question") still gives the user something
    useful to do, rather than this helper's failure taking down the
    whole welcome response.
    """
    try:
        registry = get_task_registry()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to load task registry for welcome message -- falling back to generic text")
        return []

    phrases = []
    for task_key, phrase in _TASK_KEY_TO_SPOKEN_PHRASE.items():
        task_config = registry.get(task_key)
        if task_config is not None and is_task_enabled(task_config):
            phrases.append(phrase)
    return phrases


def locale_to_country(locale_str: str) -> str:
    """Extract ISO-3166-1 country code from an Alexa locale string.

    Examples: 'en-AU' -> 'AU', 'en-US' -> 'US', 'en-GB' -> 'GB'.
    Returns empty string if the locale has no country suffix.
    """
    if not locale_str:
        return ""
    parts = locale_str.split("-")
    return parts[1].upper() if len(parts) > 1 else ""


def resolve_region(session_attributes: dict, alexa_locale: str | None) -> str:
    """Resolve the user's streaming region for TMDB watch/providers.

    Priority: stored session attribute (user already has one saved) > detected
    from Alexa's request locale > 'AU' default.  Returns the resolved code.
    """
    stored = session_attributes.get(SESSION_ATTR_REGION, "")
    if stored:
        return stored
    detected = locale_to_country(alexa_locale) if alexa_locale else ""
    return detected if detected else "AU"


def extract_query_text(intent: dict) -> str:
    """
    Returns the text that will actually be sent to the backing task for
    this intent, for the pipeline's step-4 length check.

    Delegates to router.build_query_text so the length check measures the
    exact same string the router will send, rather than its own
    approximation of it -- BoredomBusterIntent combines two slots
    (MoodOrGenre + MediaType), so "the first non-empty slot value" is no
    longer the same thing as "what gets sent."

    Imported inside the function, matching the deferred `from router
    import route_request` used elsewhere. Falls back to first-non-empty
    if the import fails, so a length check never takes down a request.
    """
    slots = intent.get("slots", {}) or {}
    try:
        from router import build_query_text

        return build_query_text(intent.get("name", ""), slots)
    except Exception:  # noqa: BLE001
        logger.exception("build_query_text unavailable -- falling back to first slot value for length check")
        for slot in slots.values():
            value = slot.get("value")
            if value:
                return value
        return ""


def extract_media_type(intent: dict) -> str:
    """
    Returns the media type ("movie" / "TV show" / "") the user asked for
    on this turn, resolved through the MediaType slot's entity
    resolution. Stored in session attributes so a subsequent mood-chip
    tap (an APL UserEvent, which carries no Alexa slots) can still pass
    it to the agent -- see SESSION_ATTR_MEDIA_TYPE.
    """
    slots = intent.get("slots", {}) or {}
    try:
        from router import resolve_media_type

        return resolve_media_type(slots.get("MediaType") or {})
    except Exception:  # noqa: BLE001
        logger.exception("resolve_media_type unavailable -- media type will not be carried in session")
        return ""


def strip_markdown(text: str) -> tuple[str, list[dict]]:
    """
    Strips markdown formatting from text to make it suitable for display
    on Echo Show where markdown rendering is not supported. Converts
    formatted text to plain text while preserving readability.

    Returns:
        tuple: (cleaned_text, extracted_links)
            - cleaned_text: Plain text with markdown removed
            - extracted_links: List of dicts with {"text": "...", "url": "..."}
              for any links found in the markdown

    Handles:
    - Headers (# ## ###) -> plain text with spacing
    - Bold (**text** or __text__) -> plain text
    - Italic (*text* or _text_) -> plain text
    - Code blocks (```...```) -> plain text
    - Inline code (`text`) -> plain text
    - Links ([text](url)) -> extracted as clickable cards
    - Lists (- item, * item, 1. item) -> plain text with bullets
    """
    if not text:
        return text, []

    # Extract links before stripping them
    extracted_links = []
    for match in re.finditer(r"\[([^\]]+)\]\(([^)]+)\)", text):
        link_text = match.group(1)
        url = match.group(2)
        # Only include http/https URLs (skip mailto:, javascript:, etc)
        if url.startswith(("http://", "https://")):
            extracted_links.append({"text": link_text, "url": url})

    # Remove code blocks first (```...```)
    text = re.sub(r"```[\s\S]*?```", "", text)

    # Remove inline code (`text`)
    text = re.sub(r"`([^`]+)`", r"\1", text)

    # Convert headers to plain text with line breaks
    text = re.sub(r"^#{1,6}\s+(.+)$", r"\1\n", text, flags=re.MULTILINE)

    # Remove bold formatting (**text** or __text__)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)

    # Remove italic formatting (*text* or _text_)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)

    # Convert links [text](url) to just the text (URLs are extracted above)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # Convert list markers to simple bullets
    text = re.sub(r"^[\s]*[-*+]\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*\d+\.\s+", "• ", text, flags=re.MULTILINE)

    return text, extracted_links


def condense_ask_bedrock_response(text: str) -> str:
    """
    Condenses the Nova model's response for Smart Assistant into a clean,
    conversational summary by stripping reasoning scaffolding while
    preserving structure and formatting for on-screen display.

    Nova Lite wraps its deliberation in <thinking> tags when it hits an
    unexpected situation; those tags and their contents must never reach
    the user as spoken or displayed text, only the model's actual answer.
    """
    if not text:
        return "I'm not sure how to answer that."

    # Keep the tail after a reasoning block (the actual answer), drop the
    # block's contents.
    cleaned = _SCAFFOLDING_BLOCK_RE.sub(" ", text)
    # Any unpaired tag left over (e.g. a stray closing tag) is noise.
    cleaned = _STRAY_TAG_RE.sub(" ", cleaned)
    # Collapse excessive whitespace but preserve single line breaks for readability
    cleaned = re.sub(r"[ \t]+", " ", cleaned)  # Collapse spaces/tabs
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)  # Max 2 consecutive newlines
    cleaned = cleaned.strip()

    # If stripping leaves nothing (a response that was ONLY a thinking
    # block), substitute a generic fallback.
    if not cleaned:
        return "I don't have a clear answer for that right now."

    # For Smart Assistant conversational experience, preserve more content
    # for the visual screen while condensing for speech. The APL template
    # displays the full answer, so we only condense for the spoken output.
    # Cap at a reasonable spoken length (~400 chars ~ 20-25 seconds of
    # speech). Truncate at sentence boundary if possible.
    MAX_SPEECH_CHARS = 400
    if len(cleaned) > MAX_SPEECH_CHARS:
        # Try to cut at a sentence end
        truncated = cleaned[:MAX_SPEECH_CHARS]
        last_period = truncated.rfind(".")
        last_question = truncated.rfind("?")
        last_exclamation = truncated.rfind("!")
        cut_at = max(last_period, last_question, last_exclamation)
        if cut_at > MAX_SPEECH_CHARS * 0.7:  # Only use if we keep most of it
            cleaned = truncated[:cut_at + 1]
        else:
            cleaned = truncated.rstrip() + "..."

    return cleaned


def log_successful_call(user_id: str, intent_or_task: str, result, latency_ms: float) -> None:
    """
    Pipeline step 6 (success path), factored out so both the main intent
    pipeline (handler.py's _handle_intent_request) and every APL
    UserEvent handler that makes a real AgentCore call
    (boredom_buster.py, watchlist.py, smart_assistant.py) share
    identical logging/call-log/metric behavior rather than duplicating
    this block. See solution-design.md Section 4, step 6.
    """
    logger.info(
        "Call succeeded: userId=%s intent=%s input_tokens=%d output_tokens=%d "
        "estimated_cost_usd=%.6f latency_ms=%.1f",
        user_id,
        intent_or_task,
        result.input_tokens,
        result.output_tokens,
        result.estimated_cost_usd,
        latency_ms,
    )
    write_call_log(
        user_id=user_id,
        intent_or_task=intent_or_task,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        estimated_cost_usd=result.estimated_cost_usd,
        latency_ms=latency_ms,
        success=True,
    )
    record_successful_call(
        user_id=user_id,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        estimated_cost_usd=result.estimated_cost_usd,
    )
    emit_call_metric(
        user_id=user_id,
        intent_or_task=intent_or_task,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        estimated_cost_usd=result.estimated_cost_usd,
        latency_ms=latency_ms,
    )


def log_failed_call(user_id: str, intent_or_task: str, latency_ms: float, exc: Exception) -> None:
    """Pipeline step 6 (failure path) -- see log_successful_call's
    docstring for why this is shared across every handler module that
    makes a real AgentCore/Bedrock call."""
    write_call_log(
        user_id=user_id,
        intent_or_task=intent_or_task,
        input_tokens=0,
        output_tokens=0,
        estimated_cost_usd=0.0,
        latency_ms=latency_ms,
        success=False,
        error_reason=str(exc),
    )
