#!/usr/bin/env python3
"""
agent.py -- the Boredom Buster AgentCore Runtime agent, this project's
Bedrock AgentCore showcase: a task with enough genuine multi-step
reasoning (tool selection, clarifying questions, persistent memory) to
demonstrate what AgentCore offers beyond a single deterministic lookup.

WHAT THIS AGENT DOES: recommends movies and TV shows (deliberately no
books, per an explicit scope decision) based on a spoken mood/genre
request, using genuine multi-step reasoning -- it can ask a clarifying
question when the request is vague, searches TMDB for candidates, checks
whether the user has already rated a title, and records feedback
(like/dislike) that AgentCore Memory's long-term SEMANTIC strategy turns
into improving recommendations over future sessions (solution-design.md
Section 12).

FRAMEWORK: built with the Strands Agents SDK (strands-agents). Strands'
@tool decorator (from `strands`) is what turns the plain Python
functions below into tools the underlying model can choose to call --
this is the actual decision-making AgentCore is used for here.

MODEL: Amazon Nova Lite, via Strands' BedrockModel provider -- the same
model this project already uses for AskBedrock (solution-design.md
Section 7.0.1), kept consistent rather than introducing a second model
choice without a specific reason to.

MEMORY: AgentCore Memory, via the bedrock_agentcore SDK's MemoryClient
(not the Strands session-manager integration) -- the client is used
directly inside search/feedback tools so memory reads/writes happen at
the exact points they're semantically meaningful (checking history
before recommending, recording feedback after a like/dislike), rather
than as an opaque per-turn side effect of the agent loop. See
memory_client.py for the actual read/write helpers, and
terraform/agentcore_memory.tf for the Memory resource + its two
strategies (SEMANTIC for per-user preferences, SUMMARIZATION for
session continuity).

RESPONSE CONTRACT (required, matching every other AgentCore agent used
by this project, per bedrock_client.py's invoke_agentcore_task()):
    {"response_text": "...", "input_tokens": N, "output_tokens": N,
     "recommendations": [...], "needs_clarification": bool}
`recommendations` is an addition beyond the base contract other agents
use -- GetWeather never needs structured data beyond a spoken sentence,
but Boredom Buster's whole point is showing recommendations with cover
art on the Echo Show screen (solution-design.md Section 13), so the
response needs to carry that structured list alongside the spoken
response_text. `handlers/handler.py`'s Alexa response builder reads this
field to build the APL recommendations-grid directive; it's simply
ignored by callers that only care about response_text.

`needs_clarification` is True whenever this turn asked a clarifying
question instead of presenting recommendations (via
ask_clarifying_question_tool, or the empty-input fallback below).
handlers/handler.py reads this field to return a real Dialog.ElicitSlot
directive, biasing Alexa's NLU to capture the next short reply into
MoodOrGenre, instead of a plain speech response that a bare one-word
reply like "movie" would never land in a slot at all.

REQUEST CONTRACT: alexa_lambda/router.py calls invoke_agentcore_task
with payload={"input": query_text, "actor_id": user_id, "session_id":
session_id} -- see router.py's route_request() for why actor_id
(Alexa's stable per-user userId) and session_id (Alexa's per-
conversation sessionId) are both passed and kept distinct.

DEPLOYMENT: this file is deployed to a Bedrock AgentCore Runtime via the
AgentCore CLI -- see this directory's README.md and DEPLOYMENT.md
Section 7a for the exact commands.
"""

import json
import logging
import os
import random
import re

from bedrock_agentcore import BedrockAgentCoreApp
from strands import Agent, tool
from strands.models import BedrockModel

from memory_client import BoredomBusterMemory
from tmdb_client import (
    MOOD_TO_GENRE_IDS,
    TmdbClientError,
    get_trailer_url,
    get_watch_providers,
    search_movies,
    search_tv_shows,
    discover_movies_by_mood,
    discover_tv_shows_by_mood,
)

logger = logging.getLogger("boredom_buster_agent")
logger.setLevel(logging.INFO)

app = BedrockAgentCoreApp()

BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
MAX_TOKENS = int(os.environ.get("BEDROCK_MAX_TOKENS", 512))

SYSTEM_PROMPT = """You are Boredom Buster, a friendly movie and TV show recommendation \
assistant for an Alexa skill with a screen (Echo Show).

Your job: help the user find something to watch. Movies and TV shows only -- \
never recommend books or anything else.

WHAT THE USER ALREADY TOLD YOU -- never ask for it again:
- If the request mentions a movie (or film), they want MOVIES. Search movies \
only. Never ask "movie or TV show?"
- If the request mentions a TV show (or series/show), they want TV SHOWS. \
Search TV shows only. Never ask "movie or TV show?"
- If the request already names a mood, genre, vibe, or era (funny, scary, \
feel-good, 80s action, etc.), that is enough to search on. Do not ask a \
clarifying question -- just search.

WHEN TO ASK A CLARIFYING QUESTION (at most ONE, ever, per request):
- Only when you have NO mood or genre to search on. Use the \
ask_clarifying_question tool and ask about MOOD or GENRE -- you MUST use \
this exact wording: "What are you in the mood for -- something funny, \
something thrilling, something feel-good, or surprise me?"
- If they told you the type (movie or TV show) but no mood, still ask about \
MOOD only. Do not repeat the type question back to them.
- If their answer is still vague after one question, pick a popular crowd- \
pleasing genre yourself and search. Never ask twice.

SEARCHING:
- Use search_movies and/or search_tv_shows to find real candidates. If the \
user did not specify a type, search both for a mix.
- Before presenting a candidate, use check_history to see if this user has \
already liked or disliked it. Skip anything they've already disliked.
- Aim for 3 to 5 recommendations.
- If the user says they like or dislike something you recommended, use the \
record_feedback tool to save that -- this is what makes future \
recommendations better.

YOUR SPOKEN REPLY (this is read aloud by Alexa, and the titles are shown on \
the screen at the same time):
- Keep it to ONE short sentence. Under 20 words.
- Never describe your own process. Do not say what you searched, which \
tools you used, how many results came back, that you checked history, or \
what you are "going to" do next. No lists of titles with descriptions -- \
the screen shows all of that.
- Good: "Here are a few funny picks for you." / "Try one of these."
- Bad: "I searched for comedies and found several options. Let me check \
your history first. Here are the results: 1. ..."
- Never wrap your reply in tags like <thinking>. Reply with the sentence \
only.

IF A SEARCH TOOL RETURNS AN ERROR:
- Do NOT retry it more than once. Instead, present whatever recommendations \
you already collected from the other search path (movies or TV shows) -- \
the user tapped a chip because they want suggestions, not an error message.
- If NO recommendations exist from any search, say only: "I'm having \
trouble reaching my recommendations right now. Please \
try again in a moment." Nothing about errors, tools, genre, or what went \
wrong. Do NOT name a specific media type (movie/TV show) since the user's \
request may have searched both libraries.
"""

# Spoken lines used when this turn produced recommendations, replacing the
# model's own final text entirely -- see _build_spoken_summary.
_MAX_TITLES_SPOKEN = 2

# Cap on the synopsis text carried per recommendation. The full list is
# echoed back to Alexa in sessionAttributes so the detail screen and
# back-navigation can reuse it without another agent call (see
# handlers/handler.py), and Alexa enforces a hard limit on total
# response size -- 5 untrimmed TMDB overviews plus poster/logo URLs can
# get close to it. 400 characters is comfortably more than the detail
# screen's 6-line synopsis area displays anyway.
_MAX_OVERVIEW_CHARS = 400


# Per-session cache of built Agents, so a multi-turn conversation (e.g.
# the agent asking "movie or TV show?" via ask_clarifying_question_tool,
# then the user answering "movie" on the next turn) actually has
# conversation history to work with. Strands' Agent keeps its own turn
# history in its `messages` attribute for as long as the same Agent
# instance keeps being used, so building a fresh Agent per invocation
# would lose that context between turns. AgentCore Runtime routes
# repeated calls sharing the same runtimeSessionId to the same warm
# execution environment (session affinity), so caching here at module
# level, keyed by (session_id, media_type), is what persists
# conversation state turn-to-turn within one Alexa session -- see
# handlers/handler.py for the corresponding logic that keeps the Alexa
# session itself open across a clarifying question.
#
# Bounded to a small size (not unbounded) since a single warm
# environment could in principle serve more than one session_id over
# its lifetime -- evicts the oldest entry once the cap is hit, rather
# than growing forever.
_MAX_CACHED_SESSIONS = 20
_session_agents: dict[tuple[str, str, str], tuple[Agent, "BoredomBusterMemory", list[dict], dict]] = {}


def _build_agent(actor_id: str, session_id: str, media_type: str = "", region: str = "AU") -> tuple[Agent, BoredomBusterMemory, list[dict], dict]:
    """
    Returns the cached Agent + memory helper for this session_id if one
    already exists (continuing that conversation's history), or builds
    a fresh one otherwise (first turn of a new session, or a cold
    execution environment with no cache yet).

    `collected_recommendations` and `clarification_state` are cleared/
    reset, not rebuilt, on a cache hit -- both track only THIS turn's
    outcome (see this function's docstring on the response contract
    and clarification_state's own comment above), while the Agent/
    memory objects persist across turns. Both objects are the SAME ones
    the cached tools' closures already reference, so mutating them in
    place (not reassigning) is what makes this turn start fresh while
    still being visible to those existing closures.
    """
    # Use (session_id, media_type, region) as the cache key
    cache_key = (session_id, media_type, region)
    cached = _session_agents.get(cache_key)
    if cached is not None:
        _agent, _memory, _collected_recommendations, _clarification_state = cached
        _collected_recommendations.clear()
        _clarification_state["asked"] = False
        _clarification_state["question"] = ""
        return _agent, _memory, _collected_recommendations, _clarification_state

    memory = BoredomBusterMemory(actor_id=actor_id, session_id=session_id)

    # Collected across the conversation so the entrypoint can return
    # structured recommendation data alongside the spoken response --
    # see this module's docstring on the response contract. Tools
    # append to this list as they find candidates; it is NOT returned
    # by the tools themselves (tool results are strings/dicts fed back
    # to the model, not necessarily what should reach the Alexa
    # response), it's tracked here as an explicit side channel.
    collected_recommendations: list[dict] = []

    # Tracks whether THIS turn ended by asking a clarifying question
    # (ask_clarifying_question_tool below sets "asked"=True), so
    # boredom_buster() can tell the Alexa Lambda side to bias its NLU
    # toward capturing the user's short reply into the MoodOrGenre slot
    # -- see this module's docstring and handlers/handler.py's use of
    # the "needs_clarification" response field, which is what lets a
    # bare one-word reply like "movie" actually land in MoodOrGenre
    # instead of arriving as an empty slot on the follow-up turn.
    #
    # "question" holds the exact text passed to
    # ask_clarifying_question_tool, and is what actually gets spoken and
    # rendered on the clarification screen -- not the model's own final
    # text, since the model may write closing prose after calling the
    # tool (e.g. "Here is a funny pick for you: The Hangover.") that
    # would otherwise end up captioning a screen of mood chips with an
    # answer instead of a question.
    clarification_state = {"asked": False, "question": ""}

    @tool
    def search_movies_tool(query: str) -> str:
        """
        Search for movies matching a mood, genre, or description using TMDB's
        discover endpoint for genre-based search (not text search).

        Args:
            query: A mood, genre, or description of what kind of movie to find,
                e.g. "funny", "scary horror", "80s action", "feel-good family".

        Returns:
            A JSON string list of matching movies with title, overview,
            release date, and rating.
        """
        # Only search for movies if media_type is movie or not specified
        if media_type and media_type != "movie":
            return json.dumps([])
        try:
            # Use discover by mood for better genre-based results, filtered
            # to content available in the user's region.
            results = discover_movies_by_mood(query, max_results=5, region=region)
        except TmdbClientError as exc:
            logger.warning("search_movies_tool failed: %s", exc)
            return json.dumps({"error": str(exc)})
        collected_recommendations.extend(results)
        return json.dumps(results)

    @tool
    def search_tv_shows_tool(query: str) -> str:
        """
        Search for TV shows matching a mood, genre, or description using TMDB's
        discover endpoint for genre-based search.

        Args:
            query: A mood, genre, or description of what kind of show to find,
                e.g. "gripping crime drama" or "lighthearted sitcom".

        Returns:
            A JSON string list of matching TV shows with title,
            overview, first air date, and rating.
        """
        # Only search for TV shows if media_type is tv or not specified
        if media_type and media_type != "tv":
            return json.dumps([])
        try:
            # Use discover by mood for better genre-based results, filtered
            # to content available in the user's region.
            results = discover_tv_shows_by_mood(query, max_results=5, region=region)
        except TmdbClientError as exc:
            logger.warning("search_tv_shows_tool failed: %s", exc)
            return json.dumps({"error": str(exc)})
        collected_recommendations.extend(results)
        return json.dumps(results)

    @tool
    def check_history_tool(title: str) -> str:
        """
        Check whether this user has already rated a title (liked or
        disliked it) in a previous session.

        Args:
            title: The title of the movie or TV show to check.

        Returns:
            A JSON string describing any prior rating found, or a note
            that there is no history for this title.
        """
        rating = memory.get_title_rating(title)
        if rating is None:
            return json.dumps({"title": title, "prior_rating": None})
        return json.dumps({"title": title, "prior_rating": rating})

    @tool
    def record_feedback_tool(title: str, liked: bool) -> str:
        """
        Record that the user liked or disliked a title. This is what
        makes future recommendations improve over time.

        Args:
            title: The title of the movie or TV show.
            liked: True if the user liked it, False if they disliked it.

        Returns:
            A short confirmation string.
        """
        memory.record_feedback(title=title, liked=liked)
        return f"Recorded that the user {'liked' if liked else 'disliked'} '{title}'."

    @tool
    def ask_clarifying_question_tool(question: str) -> str:
        """
        Signal that a clarifying question should be asked instead of
        searching yet. Use this when the request is too vague to search
        on (no mood, genre, or type given at all).

        Args:
            question: The short, natural clarifying question to ask the
                user, e.g. "Are you in the mood for something funny, or
                something more intense?"

        Returns:
            The question text, to be spoken as-is.
        """
        clarification_state["asked"] = True
        clarification_state["question"] = question
        return question

    model = BedrockModel(model_id=BEDROCK_MODEL_ID, max_tokens=MAX_TOKENS)
    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[
            search_movies_tool,
            search_tv_shows_tool,
            check_history_tool,
            record_feedback_tool,
            ask_clarifying_question_tool,
        ],
    )

    if len(_session_agents) >= _MAX_CACHED_SESSIONS:
        # Evict the oldest entry (insertion-ordered dict, standard since
        # Python 3.7) -- a simple bound against unbounded growth, not a
        # true LRU; fine for this project's scale (one warm environment
        # serving at most a handful of concurrent Alexa sessions).
        oldest_key = next(iter(_session_agents))
        del _session_agents[oldest_key]
    _session_agents[cache_key] = (agent, memory, collected_recommendations, clarification_state)

    return agent, memory, collected_recommendations, clarification_state


# Reasoning/scaffolding wrappers some models emit around their answer.
# Matched case-insensitively, across newlines, and tolerant of an
# UNCLOSED opening tag (a response truncated at max_tokens mid-thought
# would otherwise have its entire body kept).
_SCAFFOLDING_TAGS = ("thinking", "thought", "reasoning", "scratchpad", "answer")
_SCAFFOLDING_BLOCK_RE = re.compile(
    r"<\s*(" + "|".join(_SCAFFOLDING_TAGS) + r")\s*>(.*?)(?:<\s*/\s*\1\s*>|$)",
    re.IGNORECASE | re.DOTALL,
)
_STRAY_TAG_RE = re.compile(r"<\s*/?\s*(" + "|".join(_SCAFFOLDING_TAGS) + r")\s*>", re.IGNORECASE)


def _strip_model_scaffolding(text: str) -> str:
    """
    Removes model reasoning scaffolding from text that is about to be
    spoken aloud and rendered on screen.

    WHY: a real deployed log showed Alexa saying, verbatim, "<thinking>
    There was another error with the search for movies. Since I cannot
    search for movies, I will suggest a popular comedy movie that is
    generally well-received.</thinking> Here is a funny pick for you:
    The Hangover." Nova Lite wraps its deliberation in <thinking> tags
    when it hits an unexpected situation, and that whole block was being
    passed straight through to the user -- exposing internal tool
    failures and reasoning in the one code path that still forwards the
    model's own words (a turn with no recommendations; turns WITH
    recommendations already discard the model text entirely, see
    _build_spoken_summary).

    Belt and braces on top of SYSTEM_PROMPT's instruction not to narrate
    reasoning: prompt instructions are a request, this is a guarantee.
    If stripping leaves nothing at all (a response that was ONLY a
    thinking block), the caller gets "" and substitutes its own text --
    see boredom_buster().
    """
    if not text:
        return ""

    # Keep the tail after a reasoning block (the actual answer), drop the
    # block's contents.
    cleaned = _SCAFFOLDING_BLOCK_RE.sub(" ", text)
    # Any unpaired tag left over (e.g. a stray closing tag) is noise.
    cleaned = _STRAY_TAG_RE.sub(" ", cleaned)
    # Collapse the whitespace the substitutions leave behind so the
    # spoken text doesn't contain long pauses.
    return re.sub(r"\s+", " ", cleaned).strip()


def _build_spoken_summary(recommendations: list[dict]) -> str:
    """
    Builds the spoken reply for a turn that produced recommendations,
    deterministically, from the recommendation list itself -- replacing
    the model's own final text rather than passing it through.

    Nova Lite's free text routinely narrates its own tool use ("I
    searched for comedies and found several options, let me check your
    history...") and then reads every title and synopsis aloud, which is
    slow to listen to and redundant with the grid already on screen.
    Tightening SYSTEM_PROMPT reduces this but can't guarantee it, since
    an LLM's free text is never fully constrained. Generating the spoken
    line here keeps reasoning and tool narration out of what the user
    hears, at the cost of a little personality in the wording.

    The screen (alexa_lambda/utils/apl.py's recommendations grid) is
    what actually presents the titles, ratings, and poster art, so this
    line's only job is to hand the user off to it.
    """
    count = len(recommendations)
    if count == 0:
        return ""

    titles = [rec.get("title", "") for rec in recommendations if rec.get("title")]
    if count == 1:
        return f"Here's my pick: {titles[0]}. Tap it for details."

    named = titles[:_MAX_TITLES_SPOKEN]
    if count <= _MAX_TITLES_SPOKEN:
        title_clause = " and ".join(named)
        return f"Here you go: {title_clause}. Tap either one for details."

    title_clause = ", ".join(named)
    return f"I found {count} for you, including {title_clause}. Tap any one to see more."


def _enrich_recommendations(recommendations: list[dict]) -> None:
    """
    Adds a trailer URL and "where to watch" providers to each
    recommendation, in place.

    Both lookups happen here, inside the agent, rather than in the Alexa
    Lambda's APL layer -- keeps every TMDB API call in one module
    (tmdb_client.py) with one place to handle its failures, and means
    the Lambda never needs a TMDB API key of its own. Each title costs
    two extra TMDB calls (videos + watch/providers); TMDB's free tier
    has no per-call charge, and the calls only happen on turns that
    actually produced recommendations (3-5 titles), not on clarifying
    turns.

    A failure on either lookup degrades that field to empty/absent for
    that one title rather than failing the whole turn -- a missing
    trailer or unknown availability is a normal outcome (see
    get_watch_providers' docstring), and the detail screen handles both.
    """
    for rec in recommendations:
        if rec.get("overview") and len(rec["overview"]) > _MAX_OVERVIEW_CHARS:
            rec["overview"] = rec["overview"][:_MAX_OVERVIEW_CHARS].rstrip() + "..."

        try:
            rec["trailer_url"] = get_trailer_url(rec["media_type"], rec["id"])
        except TmdbClientError as exc:
            logger.warning("get_trailer_url failed for %s: %s", rec.get("title"), exc)
            rec["trailer_url"] = ""

        try:
            rec["watch_providers"] = get_watch_providers(rec["media_type"], rec["id"])
        except TmdbClientError as exc:
            logger.warning("get_watch_providers failed for %s: %s", rec.get("title"), exc)
            rec["watch_providers"] = {"region": "", "link": "", "stream": [], "rent": [], "buy": []}


@app.entrypoint
def boredom_buster(payload: dict) -> dict:
    """
    AgentCore Runtime entrypoint. `payload` is whatever router.py sends
    -- {"input": "<spoken request>", "actor_id": "<Alexa userId>",
    "session_id": "<Alexa sessionId>", "media_type": "<movie|TV show>"}
    Always returns the {"response_text", "input_tokens", "output_tokens",
    "recommendations"} shape described in this file's module docstring,
    even on failure.
    """
    user_input = (payload.get("input") or "").strip()
    actor_id = payload.get("actor_id") or "unknown-user"
    session_id = payload.get("session_id") or actor_id
    media_type = payload.get("media_type") or ""
    region = payload.get("region") or "AU"  # ISO-3166-1 country code for TMDB watch/providers

    # Handle "surprise me" -- pick a random genre from the mood-to-genre
    # mapping so we actually send a valid genre filter to TMDB rather than
    # querying with no genre at all (which produces an unfiltered popular
    # list that may fail for the user's region, or return content they've
    # already seen).  Uses the same pool as the CLARIFY_MOOD_OPTIONS chips.
    SURPRISE_PHRASES = ("surprise me", "a popular crowd pleaser", "random", "anything", "pick one")
    if user_input.lower() in SURPRISE_PHRASES:
        mood_key = random.choice(list(MOOD_TO_GENRE_IDS.keys()))
        user_input = mood_key
        logger.info("Surprise me picked genre: %s", mood_key)

    if not user_input:
        return {
            # Asks about MOOD, not "movie or TV show?" -- mood/genre is
            # the one thing an empty request genuinely never carries,
            # and it's what actually improves the search. If the user
            # already specified a media type, MediaType is passed
            # separately in the payload rather than re-asked here.
            # MUST match the exact wording in SYSTEM_PROMPT and CLARIFY_MOOD_OPTIONS
            "response_text": "Happy to help you find something. What are you in the mood for -- something funny, something thrilling, something feel-good, surprise me, or search for something specific?",
            "input_tokens": 0,
            "output_tokens": 0,
            "recommendations": [],
            # Signals the Lambda side to elicit MoodOrGenre with NLU
            # biasing turned on for a short reply -- see this response
            # contract's docstring below and handlers/handler.py's use
            # of this field, which is what lets Alexa specifically listen
            # for a short slot-filling reply on the next turn instead of
            # a follow-up like "movie" arriving with an empty slot again.
            "needs_clarification": True,
        }

    agent, memory, collected_recommendations, clarification_state = _build_agent(actor_id=actor_id, session_id=session_id, media_type=media_type, region=region)

    try:
        result = agent(user_input)
    except Exception as exc:  # noqa: BLE001 -- this agent must always return the
        # required response contract, even on an unexpected model/tool
        # failure, so the Lambda side never has to guess at a missing key.
        # Logged with the full traceback (logger.exception) so the real
        # cause is in the Runtime's CloudWatch Logs -- but deliberately
        # not included in response_text, which Alexa reads aloud and
        # renders on screen. An internal failure (e.g. a TMDB HTTP error)
        # must never be read out to the user verbatim.
        logger.exception("Boredom Buster agent invocation failed: %s", exc)
        return {
            "response_text": "Sorry, I couldn't put together a recommendation just now. Please try again in a moment.",
            "input_tokens": 0,
            "output_tokens": 0,
            "recommendations": [],
            "needs_clarification": False,
        }

    usage = result.metrics.accumulated_usage if result.metrics else {}
    input_tokens = usage.get("inputTokens", 0)
    output_tokens = usage.get("outputTokens", 0)

    # De-duplicate by title in case both search tools returned overlapping
    # results, and cap the count to what solution-design.md Section 13.1's
    # recommendations grid is designed to show (3-5 per the system prompt).
    seen_titles = set()
    deduped_recommendations = []
    for rec in collected_recommendations:
        if rec["title"] in seen_titles:
            continue
        seen_titles.add(rec["title"])
        deduped_recommendations.append(rec)

    # Trailer + "where to watch" per title, so the detail screen has
    # everything it needs without the Lambda ever calling TMDB itself --
    # see _enrich_recommendations.
    _enrich_recommendations(deduped_recommendations)

    # Recommendations win over a clarifying question if the model somehow
    # did both in one turn (searched AND called
    # ask_clarifying_question_tool): showing what was found beats asking
    # another question the user has effectively already answered.
    if deduped_recommendations:
        response_text = _build_spoken_summary(deduped_recommendations)
        needs_clarification = False
    elif clarification_state["asked"]:
        # The recorded question is used verbatim, in preference to the
        # model's own closing prose -- see clarification_state's comment
        # in _build_agent for the deployed bug where those two diverged
        # and the mood-chip screen ended up captioned with an answer
        # instead of a question.
        needs_clarification = True
        response_text = clarification_state["question"] or _strip_model_scaffolding(str(result))
    else:
        # No recommendations and no clarifying question -- the model's own
        # text IS the response here (e.g. "nothing matched"), passed
        # through with any reasoning scaffolding removed.
        needs_clarification = False
        response_text = _strip_model_scaffolding(str(result))

    # If both movie and TV searches returned zero results, or if one of them
    # hit a TMDB error (caught by the search tool), ensure we have a valid
    # fallback. When neither search produced data, the model's own text might
    # be an incomplete error message -- substitute a clean generic fallback.
    if not response_text:
        # Stripping can legitimately empty the string (a response that was
        # nothing but a reasoning block). Never hand Alexa an empty
        # outputSpeech -- the device would sit silent with no indication
        # anything happened.
        response_text = "I couldn't find anything to suggest just now. Want to try a different mood?"
        needs_clarification = True

    return {
        "response_text": response_text,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "recommendations": deduped_recommendations,
        # True when this turn ended by asking a clarifying question
        # (clarification_state, set by ask_clarifying_question_tool)
        # rather than presenting recommendations -- see this response
        # contract's docstring and handlers/handler.py's use of this
        # field to bias Alexa's NLU toward capturing a short reply (e.g.
        # "something funny") into the MoodOrGenre slot on the next turn,
        # instead of that reply landing as an empty slot again.
        "needs_clarification": needs_clarification,
    }


if __name__ == "__main__":
    app.run()
