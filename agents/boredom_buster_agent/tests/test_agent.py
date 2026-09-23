"""
Tests for agent.py -- the Boredom Buster AgentCore Runtime entrypoint.

The real strands.Agent (which would call Bedrock) and MemoryClient
(which would call AgentCore Memory) are both mocked -- no real network/
AWS access during pytest, matching the same "no live external calls in
CI" principle used everywhere else in this project. TMDB calls are
mocked at the tmdb_client module level (patched into agent's imported
names) rather than going through real HTTP.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_session_agent_cache():
    """
    _build_agent now caches one Agent per session_id (see agent.py's
    _session_agents docstring, added so a multi-turn conversation like
    a clarifying question actually has history to work with). Several
    tests below reuse session_id="session1" with DIFFERENT mocked
    Agent/tool setups -- without clearing the cache between tests, a
    later test would hit an earlier test's cached (differently mocked)
    Agent instance instead of building its own, silently breaking test
    isolation rather than raising a clear failure.
    """
    agent._session_agents.clear()
    yield
    agent._session_agents.clear()


def _fake_agent_result(text: str, input_tokens: int = 10, output_tokens: int = 5):
    """Builds a fake object mimicking strands' AgentResult enough for
    agent.py's boredom_buster() to extract a response string and token
    usage from it -- see agent.py's use of str(result) and
    result.metrics.accumulated_usage."""
    result = MagicMock()
    result.__str__.return_value = text
    result.metrics.accumulated_usage = {"inputTokens": input_tokens, "outputTokens": output_tokens}
    return result


def test_entrypoint_returns_response_contract_on_success(monkeypatch):
    monkeypatch.setenv("BEDROCK_AGENTCORE_MEMORY_ID", "")  # memory disabled for this test

    fake_movie = {
        "id": 550,
        "media_type": "movie",
        "title": "Fight Club",
        "overview": "...",
        "release_date": "1999-10-15",
        "rating": 8.4,
        "poster_url": "https://image.tmdb.org/t/p/w500/abc.jpg",
    }

    with (
        patch.object(agent, "search_movies", return_value=[fake_movie]),
        patch.object(agent, "get_trailer_url", return_value="https://www.youtube.com/watch?v=abc123"),
        patch("agent.Agent") as mock_agent_cls,
        patch("agent.BedrockModel"),
    ):
        # The mocked Agent's call never actually invokes tools (there's
        # no real model/event-loop here to decide to call them), so
        # this test's `recommendations` list will legitimately be
        # empty -- it exercises the entrypoint's overall control flow
        # and response contract, not tool invocation. Tool behavior
        # (populating collected_recommendations, memory reads/writes)
        # is covered by the dedicated tool-level tests below, which
        # call the tool functions directly.
        mock_agent_instance = MagicMock()
        mock_agent_instance.return_value = _fake_agent_result("Fight Club, a great pick.")
        mock_agent_cls.return_value = mock_agent_instance

        result = agent.boredom_buster({"input": "something funny", "actor_id": "user1", "session_id": "session1"})

    assert set(result.keys()) == {"response_text", "input_tokens", "output_tokens", "recommendations", "needs_clarification"}
    assert result["response_text"] == "Fight Club, a great pick."
    assert result["input_tokens"] == 10
    assert result["output_tokens"] == 5
    assert result["recommendations"] == []
    assert result["needs_clarification"] is False


def test_entrypoint_handles_empty_input_gracefully():
    result = agent.boredom_buster({"input": "", "actor_id": "user1", "session_id": "session1"})

    assert "mood" in result["response_text"].lower()
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0
    assert result["recommendations"] == []
    assert result["needs_clarification"] is True


def test_empty_input_asks_about_mood_not_movie_or_tv():
    """
    The empty-input fallback must ask about mood, not "movie or TV
    show?" -- media type is passed separately in the payload once the
    user has already specified it, so it should never be re-asked here.
    """
    response_text = agent.boredom_buster({"input": ""})["response_text"].lower()

    assert "mood" in response_text
    assert "movie or a tv" not in response_text
    assert "movie or tv" not in response_text


def test_entrypoint_handles_missing_input_key_entirely():
    result = agent.boredom_buster({})

    assert "mood" in result["response_text"].lower()
    assert result["needs_clarification"] is True


def test_entrypoint_handles_agent_exception_gracefully(monkeypatch):
    monkeypatch.setenv("BEDROCK_AGENTCORE_MEMORY_ID", "")

    with patch("agent.Agent") as mock_agent_cls, patch("agent.BedrockModel"):
        mock_agent_instance = MagicMock()
        mock_agent_instance.side_effect = Exception("model unavailable")
        mock_agent_cls.return_value = mock_agent_instance

        result = agent.boredom_buster({"input": "something scary", "actor_id": "user1", "session_id": "session1"})

    assert "couldn't put together a recommendation" in result["response_text"]
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0
    assert result["recommendations"] == []
    assert result["needs_clarification"] is False


def test_search_movies_tool_populates_collected_recommendations(monkeypatch):
    """Directly exercises the search_movies_tool closure built by
    _build_agent, confirming it both returns JSON for the model AND
    appends to collected_recommendations -- the side-channel agent.py's
    entrypoint reads to build the final 'recommendations' field."""
    monkeypatch.setenv("BEDROCK_AGENTCORE_MEMORY_ID", "")
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")

    fake_results = [
        {
            "id": 1,
            "media_type": "movie",
            "title": "Test Movie",
            "overview": "...",
            "release_date": "2020-01-01",
            "rating": 7.0,
            "poster_url": "",
        }
    ]

    with patch.object(agent, "discover_movies_by_mood", return_value=fake_results):
        built_agent, memory, collected, clarification_state = agent._build_agent(actor_id="user1", session_id="session1")
        # DecoratedFunctionTool.__call__ invokes the ORIGINAL function
        # directly (per strands.tools.decorator's own docs: "Call the
        # original function with the provided arguments... preserving
        # the normal function call behavior") -- distinct from calling
        # through agent.tool.<name>(...), which wraps the result in a
        # tool-use envelope ({"toolUseId", "status", "content"}) meant
        # for the model, not for a test asserting on the raw return
        # value. Tests here deliberately use direct calls to check the
        # tools' actual Python-level behavior.
        search_movies_tool = built_agent.tool_registry.registry["search_movies_tool"]
        tool_result = search_movies_tool(query="test")

    assert "Test Movie" in tool_result
    assert len(collected) == 1
    assert collected[0]["title"] == "Test Movie"


def test_check_history_and_record_feedback_tools_use_memory(monkeypatch):
    monkeypatch.setenv("BEDROCK_AGENTCORE_MEMORY_ID", "mem-123")
    monkeypatch.setenv("AWS_REGION", "us-east-1")

    with patch("memory_client.MemoryClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.retrieve_memories.return_value = [{"content": {"text": "liked 'Test Movie'"}}]
        mock_client_cls.return_value = mock_client

        built_agent, memory, collected, clarification_state = agent._build_agent(actor_id="user1", session_id="session1")
        registry = built_agent.tool_registry.registry

        history_result = registry["check_history_tool"](title="Test Movie")
        feedback_result = registry["record_feedback_tool"](title="Test Movie", liked=True)

    assert "liked" in history_result
    assert "Recorded" in feedback_result
    mock_client.create_event.assert_called_once()


def test_ask_clarifying_question_tool_returns_question_as_is():
    built_agent, memory, collected, clarification_state = agent._build_agent(actor_id="user1", session_id="session1")
    ask_clarifying_question_tool = built_agent.tool_registry.registry["ask_clarifying_question_tool"]
    result = ask_clarifying_question_tool(question="Are you in the mood for something funny or something intense?")
    assert result == "Are you in the mood for something funny or something intense?"


def test_ask_clarifying_question_tool_sets_clarification_state():
    """
    Regression test for the real BoredomBuster infinite-loop bug: the
    clarification_state dict is what tells boredom_buster() to set
    needs_clarification=True in its response, which handlers/handler.py
    then uses to return a real Dialog.ElicitSlot directive (biasing
    Alexa's NLU to capture the next short reply into MoodOrGenre)
    instead of a plain speech response the platform has no reason to
    treat specially.
    """
    built_agent, memory, collected, clarification_state = agent._build_agent(actor_id="user1", session_id="session_clarify")
    assert clarification_state["asked"] is False

    ask_clarifying_question_tool = built_agent.tool_registry.registry["ask_clarifying_question_tool"]
    ask_clarifying_question_tool(question="Movie or TV show?")

    assert clarification_state["asked"] is True


def test_second_call_with_same_session_id_reuses_the_same_agent_instance():
    """
    Regression test for the real bug this caching was added to fix: a
    fresh, memoryless Agent was built on EVERY invocation, so when the
    agent asked a clarifying question ("movie or TV show?") on turn 1,
    turn 2 had no idea that question had already been asked -- it just
    guessed a mood itself per the system prompt's "don't ask twice"
    rule, ignoring the user's actual mood entirely. Asserts that a
    second _build_agent call with the SAME session_id returns the exact
    same Agent object (so Strands' own conversation history in
    agent.messages carries over), while a DIFFERENT session_id gets a
    genuinely separate Agent.
    """
    built_agent_1, memory_1, collected_1, clarification_1 = agent._build_agent(actor_id="user1", session_id="session1")
    built_agent_2, memory_2, collected_2, clarification_2 = agent._build_agent(actor_id="user1", session_id="session1")
    built_agent_other, memory_other, collected_other, clarification_other = agent._build_agent(actor_id="user1", session_id="session2")

    assert built_agent_1 is built_agent_2
    assert memory_1 is memory_2
    assert built_agent_1 is not built_agent_other


def test_collected_recommendations_reset_between_turns_of_same_session():
    """The cached Agent/memory persist across turns, but
    collected_recommendations must NOT carry stale results from a
    previous turn into the next one's response -- confirmed by
    populating it on "turn 1" and checking it's empty again when
    _build_agent is called again for "turn 2" of the same session."""
    built_agent_1, memory_1, collected_1, clarification_1 = agent._build_agent(actor_id="user1", session_id="session3")
    collected_1.append({"title": "Leftover from turn 1"})
    assert len(collected_1) == 1

    built_agent_2, memory_2, collected_2, clarification_2 = agent._build_agent(actor_id="user1", session_id="session3")
    assert collected_2 == []
    # Same underlying list object as turn 1's tool closures reference --
    # cleared in place, not replaced with a new list -- so a tool
    # closure captured on turn 1 still appends to the list turn 2 reads.
    assert collected_2 is collected_1


def test_clarification_state_reset_between_turns_of_same_session():
    """Same reset-in-place guarantee as collected_recommendations,
    applied to clarification_state -- a clarifying question asked on
    turn 1 must not leak into turn 2's needs_clarification, or every
    subsequent turn would incorrectly keep re-eliciting MoodOrGenre
    even after the user already answered."""
    built_agent_1, memory_1, collected_1, clarification_1 = agent._build_agent(actor_id="user1", session_id="session4")
    clarification_1["asked"] = True

    built_agent_2, memory_2, collected_2, clarification_2 = agent._build_agent(actor_id="user1", session_id="session4")
    assert clarification_2["asked"] is False
    assert clarification_2 is clarification_1


# --------------------------------------------------------------------------
# Spoken-summary tests -- see agent._build_spoken_summary, which
# deterministically builds the spoken line so the model's own reasoning
# or tool narration never reaches the user.
# --------------------------------------------------------------------------


def test_build_spoken_summary_is_short_and_names_at_most_two_titles():
    recommendations = [
        {"title": "Fight Club"},
        {"title": "Se7en"},
        {"title": "Zodiac"},
        {"title": "Gone Girl"},
        {"title": "The Social Network"},
    ]

    summary = agent._build_spoken_summary(recommendations)

    assert "Fight Club" in summary
    assert "Se7en" in summary
    # Only the first two are named -- reading five titles aloud is
    # exactly the behavior being fixed.
    assert "Zodiac" not in summary
    assert "5" in summary
    assert len(summary.split()) < 20


def test_build_spoken_summary_single_recommendation():
    summary = agent._build_spoken_summary([{"title": "Fight Club"}])
    assert "Fight Club" in summary


def test_build_spoken_summary_two_recommendations():
    summary = agent._build_spoken_summary([{"title": "Alien"}, {"title": "Aliens"}])
    assert "Alien" in summary and "Aliens" in summary


def test_build_spoken_summary_empty_list():
    assert agent._build_spoken_summary([]) == ""


def test_entrypoint_replaces_model_prose_with_terse_summary_when_recommendations_exist(monkeypatch):
    """
    The model's own final text is DISCARDED whenever a turn produced
    recommendations -- Nova Lite routinely narrated its tool use ("I
    searched for comedies and found several options...") and read every
    synopsis aloud, which is what the user complained about. The screen
    shows the titles; the spoken line just hands off to it.
    """
    monkeypatch.setenv("BEDROCK_AGENTCORE_MEMORY_ID", "")

    fake_movie = {
        "id": 550,
        "media_type": "movie",
        "title": "Fight Club",
        "overview": "A ticking-time-bomb insomniac.",
        "release_date": "1999-10-15",
        "rating": 8.4,
        "poster_url": "https://image.tmdb.org/t/p/w500/abc.jpg",
    }
    reasoning_prose = (
        "Let me search for movies matching your mood. I found several options and "
        "checked your history. Here are the results: 1. Fight Club -- a ticking-time-bomb insomniac..."
    )

    with (
        patch.object(agent, "search_movies", return_value=[dict(fake_movie)]),
        patch.object(agent, "get_trailer_url", return_value="https://www.youtube.com/watch?v=abc"),
        patch.object(agent, "get_watch_providers", return_value={"region": "AU", "link": "", "stream": [], "rent": [], "buy": []}),
        patch("agent.Agent") as mock_agent_cls,
        patch("agent.BedrockModel"),
    ):
        mock_agent_instance = MagicMock()
        mock_agent_instance.return_value = _fake_agent_result(reasoning_prose)
        mock_agent_cls.return_value = mock_agent_instance

        built_agent, _memory, collected, _clarification = agent._build_agent("user1", "session_prose")
        mock_agent_cls.return_value = mock_agent_instance

        # Simulate the model having called the search tool, which is what
        # populates collected_recommendations.
        def _invoke(_user_input):
            collected.append(dict(fake_movie))
            return _fake_agent_result(reasoning_prose)

        mock_agent_instance.side_effect = _invoke

        result = agent.boredom_buster({"input": "something funny", "actor_id": "user1", "session_id": "session_prose"})

    assert result["recommendations"][0]["title"] == "Fight Club"
    assert "I found several options" not in result["response_text"]
    assert "checked your history" not in result["response_text"]
    assert "Fight Club" in result["response_text"]
    # Recommendations always win over a clarifying question.
    assert result["needs_clarification"] is False


def test_enrich_recommendations_attaches_trailer_and_watch_providers():
    recommendations = [{"id": 550, "media_type": "movie", "title": "Fight Club", "overview": "short"}]
    providers = {
        "region": "AU",
        "link": "https://www.themoviedb.org/movie/550/watch?locale=AU",
        "stream": [{"name": "Binge", "logo_url": "https://image.tmdb.org/t/p/w92/binge.jpg"}],
        "rent": [],
        "buy": [],
    }

    with (
        patch.object(agent, "get_trailer_url", return_value="https://www.youtube.com/watch?v=abc"),
        patch.object(agent, "get_watch_providers", return_value=providers),
    ):
        agent._enrich_recommendations(recommendations)

    assert recommendations[0]["trailer_url"] == "https://www.youtube.com/watch?v=abc"
    assert recommendations[0]["watch_providers"]["stream"][0]["name"] == "Binge"


def test_enrich_recommendations_degrades_gracefully_when_tmdb_lookups_fail():
    from tmdb_client import TmdbClientError

    recommendations = [{"id": 550, "media_type": "movie", "title": "Fight Club", "overview": "short"}]

    with (
        patch.object(agent, "get_trailer_url", side_effect=TmdbClientError("boom")),
        patch.object(agent, "get_watch_providers", side_effect=TmdbClientError("boom")),
    ):
        agent._enrich_recommendations(recommendations)

    assert recommendations[0]["trailer_url"] == ""
    assert recommendations[0]["watch_providers"]["stream"] == []


def test_enrich_recommendations_truncates_long_overviews():
    """The recommendation list is echoed back in Alexa's
    sessionAttributes, which counts against a hard response-size limit --
    see agent._MAX_OVERVIEW_CHARS."""
    long_overview = "x" * 900
    recommendations = [{"id": 1, "media_type": "movie", "title": "T", "overview": long_overview}]

    with (
        patch.object(agent, "get_trailer_url", return_value=""),
        patch.object(agent, "get_watch_providers", return_value={"region": "", "link": "", "stream": [], "rent": [], "buy": []}),
    ):
        agent._enrich_recommendations(recommendations)

    assert len(recommendations[0]["overview"]) <= agent._MAX_OVERVIEW_CHARS + 3


def test_system_prompt_forbids_re_asking_movie_or_tv():
    """
    If the user already said "movie" or "TV show", the agent must never
    ask again. The interaction model's MediaType slot gets that word to
    the agent (see alexa_lambda/router.py); this assertion guards the
    other half -- the prompt instruction telling the model not to ask
    anyway.
    """
    prompt = agent.SYSTEM_PROMPT.lower()
    assert "never ask" in prompt
    assert "mood" in prompt
