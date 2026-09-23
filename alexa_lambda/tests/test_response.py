"""Tests for utils/response.py -- Alexa response envelope shapes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.response import (
    build_error_response,
    build_input_too_long_response,
    build_launch_response,
    build_not_authorized_response,
    build_rate_limited_response,
    build_speech_response,
)


def test_build_speech_response_basic_shape():
    resp = build_speech_response("Hello")
    assert resp["version"] == "1.0"
    assert resp["response"]["outputSpeech"]["type"] == "PlainText"
    assert resp["response"]["outputSpeech"]["text"] == "Hello"
    assert resp["response"]["shouldEndSession"] is True
    assert "card" not in resp["response"]


def test_build_speech_response_with_card():
    resp = build_speech_response("Hello", card_title="Title", card_content="Content")
    assert resp["response"]["card"]["type"] == "Simple"
    assert resp["response"]["card"]["title"] == "Title"
    assert resp["response"]["card"]["content"] == "Content"


def test_build_speech_response_card_defaults_content_to_speech_text():
    resp = build_speech_response("Hello", card_title="Title")
    assert resp["response"]["card"]["content"] == "Hello"


def test_build_not_authorized_response():
    resp = build_not_authorized_response()
    assert "not authorized" in resp["response"]["outputSpeech"]["text"]


def test_build_rate_limited_response_daily_limit():
    resp = build_rate_limited_response("daily_limit")
    assert "daily usage limit" in resp["response"]["outputSpeech"]["text"]


def test_build_rate_limited_response_burst_limit():
    resp = build_rate_limited_response("burst_limit")
    assert "quickly" in resp["response"]["outputSpeech"]["text"]


def test_build_rate_limited_response_global_ceiling():
    resp = build_rate_limited_response("global_ceiling")
    assert "high demand" in resp["response"]["outputSpeech"]["text"]


def test_build_rate_limited_response_unknown_reason_has_fallback():
    resp = build_rate_limited_response("some_unexpected_reason")
    assert "usage limit" in resp["response"]["outputSpeech"]["text"]


def test_build_input_too_long_response():
    resp = build_input_too_long_response()
    assert "shorter" in resp["response"]["outputSpeech"]["text"]


def test_build_error_response():
    resp = build_error_response()
    assert "went wrong" in resp["response"]["outputSpeech"]["text"]


def test_build_launch_response_keeps_session_open():
    resp = build_launch_response()
    assert resp["response"]["shouldEndSession"] is False
