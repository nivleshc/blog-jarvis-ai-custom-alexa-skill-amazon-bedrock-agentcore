"""
Intent routing to Bedrock (foundation model) or Bedrock AgentCore Runtime
(named tasks). Implements solution-design.md Section 4, step 5 and
Section 11 (task registry / hot-reload).
"""

from dataclasses import dataclass

from bedrock_client import (
    BedrockInvocationError,
    invoke_agentcore_task,
    invoke_foundation_model,
    invoke_lambda_task,
)
from cost import estimate_cost_usd
from task_registry import get_task


@dataclass
class RouteResult:
    response_text: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    # Structured movie/TV recommendation data, populated only by Boredom
    # Buster (solution-design.md Section 13) for driving the Echo Show's
    # APL recommendations grid alongside the spoken response. Empty for
    # every other intent -- see bedrock_client.py's invoke_* functions.
    recommendations: list = None
    # True when the task couldn't resolve a required slot value (e.g.
    # GetWeather's location wasn't recognized) and the user should be
    # asked to re-supply it rather than the conversation just ending --
    # see bedrock_client.invoke_lambda_task's docstring and
    # handlers/handler.py's use of this to return a real
    # Dialog.ElicitSlot re-prompt instead of a terminal response.
    needs_clarification: bool = False

    def __post_init__(self):
        if self.recommendations is None:
            self.recommendations = []


# Maps Alexa intent names (from skill_package/interactionModels/custom/
# en-US.json) to task registry keys (from task_registry/task_registry.json).
# These differ slightly by convention -- Alexa intents are suffixed with
# "Intent", task registry keys are not -- so this mapping bridges them
# rather than forcing the two files to share an identical naming scheme.
_INTENT_TO_TASK_KEY = {
    "SmartAssistantIntent": "AskBedrock",
    "GetWeatherIntent": "GetWeather",
    "BoredomBusterIntent": "BoredomBuster",
    "ViewWatchlistIntent": "ViewWatchlist",
    # TakeNoteIntent/SearchKnowledgeBaseIntent are intentionally absent --
    # both had placeholder task registry entries with no real agent
    # behind either, and advertising them in the welcome message would
    # mislead users into intents that can't work. Add both back here, in
    # the interaction model, and in the registry together, only once real
    # agents exist for each.
}


def route_request(intent_name: str, slots: dict, user_id: str, session_id: str = "", region: str = "AU", alexa_context: dict = None) -> RouteResult:
    """
    `user_id` is Alexa's session.user.userId -- stable across every
    conversation a given user has with this skill. `session_id` is
    Alexa's session.sessionId -- a new value each time the user opens a
    fresh conversation. Only the `agentcore_task` branch uses
    `session_id` (as AgentCore Runtime's `runtimeSessionId`, for session
    affinity and AgentCore Memory's short-term/session-scoped
    namespaces); `foundation_model` and `lambda` tasks are stateless
    per-call and don't need it. Defaults to "" for callers/tests that
    don't have a real Alexa session (falls back to `user_id`).
    `region` is the user's ISO-3166-1 country code for TMDB watch/providers
    lookups (streaming availability). Defaults to "AU".
    `alexa_context` is the Alexa context dict containing API endpoint,
    access token, and device ID for Device Address API access (weather location).
    """
    task_key = _INTENT_TO_TASK_KEY.get(intent_name)
    if task_key is None:
        return RouteResult(
            response_text="I don't know how to do that yet.",
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
        )

    try:
        task_config = get_task(task_key)
    except KeyError:
        # The intent maps to a task key that isn't in the registry (e.g.
        # the registry was hot-reloaded and this task was removed). Fail
        # gracefully rather than raising -- this is a data-consistency
        # issue, not a code bug.
        return RouteResult(
            response_text="That task isn't available right now.",
            input_tokens=0,
            output_tokens=0,
            estimated_cost_usd=0.0,
        )

    query_text = build_query_text(intent_name, slots)
    media_type = None
    if intent_name == "BoredomBusterIntent":
        media_type = resolve_media_type(slots.get("MediaType") or {})

    try:
        if task_config["type"] == "foundation_model":
            result = invoke_foundation_model(query_text)
        elif task_config["type"] == "agentcore_task":
            # runtimeSessionId ties this call to a specific AgentCore
            # Runtime microVM (session affinity) and, for agents using
            # AgentCore Memory, scopes short-term/session-summary memory
            # -- see solution-design.md Section 12.3. AWS requires this
            # to be >= 33 characters; Alexa's own session.sessionId
            # (e.g. "amzn1.echo-api.session.<uuid>") comfortably clears
            # that, so it's used directly when present. user_id is
            # passed separately as actor_id in the payload -- it's the
            # STABLE per-user identifier AgentCore Memory's long-term
            # strategies key on, deliberately distinct from the
            # per-conversation session_id (see router.route_request's
            # docstring parameters and handlers/handler.py's comment on
            # why these two IDs are kept separate).
            runtime_session_id = session_id or user_id
            result = invoke_agentcore_task(
                agent_runtime_arn=task_config["agent_runtime_arn"],
                payload={"input": query_text, "actor_id": user_id, "session_id": runtime_session_id, "media_type": media_type, "region": region},
                session_id=runtime_session_id,
            )
        elif task_config["type"] == "lambda":
            # The "lambda" backend type exists for named tasks that are
            # deterministic lookups with no LLM reasoning or
            # AgentCore-specific capability needed (e.g. GetWeather) -- a
            # plain Lambda function is simpler to build and deploy than
            # an AgentCore Runtime agent for this class of task.
            payload = {"input": query_text, "user_id": user_id}

            # For GetWeather, include Alexa Device Address API info for automatic location
            if intent_name == "GetWeatherIntent" and alexa_context:
                payload["api_endpoint"] = alexa_context.get("api_endpoint")
                payload["api_access_token"] = alexa_context.get("api_access_token")
                payload["device_id"] = alexa_context.get("device_id")

            result = invoke_lambda_task(
                function_arn=task_config["lambda_function_arn"],
                payload=payload,
            )
        else:
            raise BedrockInvocationError(f"Unknown task type: {task_config['type']}")
    except BedrockInvocationError:
        # Re-raise so handler.py's existing try/except around
        # route_request() logs the failure to SkillCallLog with
        # success=False -- see handlers/handler.py's step 5/6 error path.
        # Not caught here because this module doesn't have access to the
        # call-log writer, and handler.py already owns that
        # responsibility.
        raise

    cost = estimate_cost_usd(result["input_tokens"], result["output_tokens"])

    return RouteResult(
        response_text=result["response_text"],
        input_tokens=result["input_tokens"],
        output_tokens=result["output_tokens"],
        estimated_cost_usd=cost,
        recommendations=result.get("recommendations", []),
        needs_clarification=result.get("needs_clarification", False),
    )


# Words that already imply a media type when they appear inside a
# free-text MoodOrGenre value -- used to avoid producing a redundant
# phrase like "funny movie movie" when Alexa fills BOTH MoodOrGenre
# ("funny movie") and MediaType ("movie") from the same utterance. See
# _build_boredom_buster_query.
_MEDIA_TYPE_WORDS = ("movie", "film", "flick", "tv", "series", "show")

# Maps the MediaType custom slot type's entity-resolution IDs (see
# skill_package/interactionModels/custom/en-US.json's `types`) to the
# wording sent to the Boredom Buster agent. The agent's SYSTEM_PROMPT
# keys off these exact words ("movie" / "tv") to decide which
# search tool to use and, critically, to NOT ask "movie or TV show?"
# again -- see agents/boredom_buster_agent/agent.py.
_MEDIA_TYPE_ID_TO_PHRASE = {"MOVIE": "movie", "TV": "tv"}


def build_query_text(intent_name: str, slots: dict) -> str:
    """
    Builds the text sent to the backing task/agent from Alexa's filled
    slots.

    Every intent except BoredomBusterIntent has exactly one meaningful
    slot (SmartAssistant's Query, GetWeather's Location), so the default is
    simply the first non-empty slot value. BoredomBusterIntent is the
    exception -- it has two slots that BOTH carry meaning and need to
    reach the agent together, which the old first-non-empty behavior
    silently dropped half of.

    Exposed (not underscore-private) because handlers/handler.py's
    input-length check needs to measure the same text that will actually
    be sent, not a different subset of the slots.
    """
    if intent_name == "BoredomBusterIntent":
        return _build_boredom_buster_query(slots)
    return _first_slot_value(slots)


def _build_boredom_buster_query(slots: dict) -> str:
    """
    Combines BoredomBusterIntent's MoodOrGenre (free text, e.g. "funny")
    and MediaType ("movie" / "TV show") into one phrase for the agent.

    WHY BOTH: the utterance "recommend a movie" fills no AMAZON.SearchQuery
    slot at all -- there's no free text left over after the carrier phrase --
    so without MediaType the agent would receive an empty string and have
    no idea a movie had been asked for. The router's `_first_slot_value`
    returns only the first non-empty slot, so once both slots can be
    filled (e.g. a clarifying question adds a mood to an existing
    MediaType), combining them here is what stops one from being dropped.

    NOTE ON WHY THEY'RE SEPARATE SLOTS AND NOT ONE: Alexa rejects a
    sample utterance containing an AMAZON.SearchQuery slot alongside any
    other slot (build error InvalidIntentSamplePhraseSlot), so
    "recommend a {MoodOrGenre} {MediaType}" is not expressible. Each
    slot is therefore filled by its own samples, and recombined here.

    The `_MEDIA_TYPE_WORDS` check avoids a redundant phrase: an
    utterance like "recommend a funny movie" can fill MoodOrGenre with
    "funny movie" (SearchQuery greedily captures the trailing noun), and
    appending MediaType on top of that would send "funny movie movie".

    IMPORTANT: If MoodOrGenre is empty but MediaType is filled (e.g. user
    tapped "Movie Ideas" card), we return "" (empty) so the agent's
    empty-input fallback triggers and asks for mood. The media_type is
    passed separately in the payload (see route_request), so the agent
    knows the type without it being in the query text.

    SPECIAL CASE: If mood is "specific search" (from voice or tapping that chip),
    return "" so the handler can show the keyword search screen instead of sending
    it to the agent as a query.
    """
    mood = (slots.get("MoodOrGenre") or {}).get("value") or ""
    media_phrase = resolve_media_type(slots.get("MediaType") or {})

    # Special case: "specific search" triggers the keyword search screen, not a query
    if mood.lower() == "specific search":
        return ""

    # If no mood/genre specified, send empty query so agent asks for mood.
    # The media_type is passed separately in the AgentCore payload (for tool
    # filtering), but the query text also carries context visible to the model.
    if not mood:
        return ""

    # Avoid a redundant phrase when MoodOrGenre already includes the type
    # word (e.g. "funny movie" from an utterance like "recommend a funny
    # movie"). Otherwise append it so the model sees media context in the
    # query text as well as the payload's explicit media_type constraint.
    if any(word in mood.lower() for word in _MEDIA_TYPE_WORDS):
        return mood
    if media_phrase:
        return f"{mood} {media_phrase}"
    return mood


def resolve_media_type(slot: dict) -> str:
    """
    Resolves a MediaType slot to "movie", "tv", or "" (not filled /
    unrecognized).

    Prefers Alexa's entity resolution (`resolutions`) over the raw spoken
    `value`, since that's what collapses the slot type's synonyms --
    "film", "flick", "series", "box set" -- onto a single canonical ID
    (MOVIE / TV). Falls back to keyword-matching the raw value when
    resolution is absent or didn't match, which covers both a device
    response shape without resolutions and a synonym the slot type
    doesn't list.
    """
    for authority in (slot.get("resolutions") or {}).get("resolutionsPerAuthority") or []:
        if (authority.get("status") or {}).get("code") != "ER_SUCCESS_MATCH":
            continue
        for resolved_value in authority.get("values") or []:
            value_id = (resolved_value.get("value") or {}).get("id", "")
            if value_id in _MEDIA_TYPE_ID_TO_PHRASE:
                return _MEDIA_TYPE_ID_TO_PHRASE[value_id]

    raw_value = (slot.get("value") or "").lower()
    if not raw_value:
        return ""
    if any(word in raw_value for word in ("movie", "film", "flick")):
        return "movie"
    if any(word in raw_value for word in ("tv", "series", "show")):
        return "tv"
    return ""


def _first_slot_value(slots: dict) -> str:
    for slot in slots.values():
        value = slot.get("value")
        if value:
            return value
    return ""
