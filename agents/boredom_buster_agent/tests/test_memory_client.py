"""
Tests for memory_client.py. bedrock_agentcore.memory.MemoryClient is
mocked -- no real AWS calls during pytest. Method names/signatures used
in the mocks (create_event, retrieve_memories) match the real SDK's
MemoryClient class, verified directly against its source
(github.com/aws/bedrock-agentcore-sdk-python's memory/client.py) before
writing this module, not assumed.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory_client import BoredomBusterMemory  # noqa: E402


def _make_memory(monkeypatch, memory_id="mem-123"):
    monkeypatch.setenv("BEDROCK_AGENTCORE_MEMORY_ID", memory_id)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with patch("memory_client.MemoryClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        memory = BoredomBusterMemory(actor_id="amzn1.ask.account.TESTUSER", session_id="session-abc")
        return memory, mock_client


def test_record_feedback_calls_create_event_with_expected_shape(monkeypatch):
    memory, mock_client = _make_memory(monkeypatch)

    memory.record_feedback(title="Fight Club", liked=True)

    mock_client.create_event.assert_called_once()
    call_kwargs = mock_client.create_event.call_args.kwargs
    assert call_kwargs["memory_id"] == "mem-123"
    assert call_kwargs["actor_id"] == "amzn1-ask-account-TESTUSER"
    assert call_kwargs["session_id"] == "session-abc"
    messages = call_kwargs["messages"]
    assert any("liked" in text and "Fight Club" in text for text, role in messages if role == "USER")


def test_record_feedback_disliked(monkeypatch):
    memory, mock_client = _make_memory(monkeypatch)

    memory.record_feedback(title="Some Bad Movie", liked=False)

    call_kwargs = mock_client.create_event.call_args.kwargs
    messages = call_kwargs["messages"]
    assert any("disliked" in text for text, role in messages if role == "USER")


def test_record_feedback_swallows_exceptions(monkeypatch):
    """A memory write failure should never propagate and break the
    recommendation flow -- see memory_client.py's module docstring on
    graceful degradation."""
    memory, mock_client = _make_memory(monkeypatch)
    mock_client.create_event.side_effect = Exception("network error")

    memory.record_feedback(title="Fight Club", liked=True)  # should not raise


def test_get_title_rating_returns_liked_when_found(monkeypatch):
    memory, mock_client = _make_memory(monkeypatch)
    mock_client.retrieve_memories.return_value = [
        {"content": {"text": "The user liked 'Fight Club'."}},
    ]

    result = memory.get_title_rating("Fight Club")

    assert result == "liked"
    call_kwargs = mock_client.retrieve_memories.call_args.kwargs
    assert call_kwargs["memory_id"] == "mem-123"
    assert call_kwargs["namespace"] == "/preferences/amzn1-ask-account-TESTUSER/"


def test_get_title_rating_returns_disliked_when_found(monkeypatch):
    memory, mock_client = _make_memory(monkeypatch)
    mock_client.retrieve_memories.return_value = [
        {"content": {"text": "The user disliked 'Some Bad Movie'."}},
    ]

    result = memory.get_title_rating("Some Bad Movie")

    assert result == "disliked"


def test_get_title_rating_returns_none_when_no_match(monkeypatch):
    memory, mock_client = _make_memory(monkeypatch)
    mock_client.retrieve_memories.return_value = [
        {"content": {"text": "The user liked 'A Completely Different Movie'."}},
    ]

    result = memory.get_title_rating("Fight Club")

    assert result is None


def test_get_title_rating_returns_none_on_empty_results(monkeypatch):
    memory, mock_client = _make_memory(monkeypatch)
    mock_client.retrieve_memories.return_value = []

    result = memory.get_title_rating("Fight Club")

    assert result is None


def test_get_title_rating_swallows_exceptions(monkeypatch):
    memory, mock_client = _make_memory(monkeypatch)
    mock_client.retrieve_memories.side_effect = Exception("service error")

    result = memory.get_title_rating("Fight Club")

    assert result is None


def test_missing_memory_id_skips_operations_gracefully(monkeypatch):
    monkeypatch.delenv("BEDROCK_AGENTCORE_MEMORY_ID", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    with patch("memory_client.MemoryClient") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client
        memory = BoredomBusterMemory(actor_id="user1", session_id="session1")

        memory.record_feedback(title="Fight Club", liked=True)
        result = memory.get_title_rating("Fight Club")

    mock_client.create_event.assert_not_called()
    mock_client.retrieve_memories.assert_not_called()
    assert result is None
