"""
Tests for lambda_tasks/get_weather/handler.py -- the classic-Lambda
backend for the GetWeather task.

HTTP calls to Open-Meteo are mocked -- no real network access during the
test suite, matching the same "no live external calls in CI" principle
used everywhere else in this project.
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import handler  # noqa: E402


def _geocode_result(name="Seattle", country="United States", lat=47.6, lon=-122.3):
    return {"results": [{"name": name, "country": country, "latitude": lat, "longitude": lon}]}


def _forecast_result(temp_c=17.0, weather_code=1):
    return {"current": {"temperature_2m": temp_c, "weather_code": weather_code}}


def test_get_weather_summary_returns_expected_sentence():
    with patch.object(handler, "_http_get_json") as mock_get:
        mock_get.side_effect = [_geocode_result(), _forecast_result()]
        result = handler.get_weather_summary("Seattle")

    assert result == "Right now in Seattle, United States, it's 17 degrees Celsius with mostly clear."


def test_get_weather_summary_unknown_weather_code_has_generic_fallback():
    with patch.object(handler, "_http_get_json") as mock_get:
        mock_get.side_effect = [_geocode_result(), _forecast_result(weather_code=999)]
        result = handler.get_weather_summary("Seattle")

    assert "conditions I can't fully describe" in result


def test_geocode_location_raises_on_no_results():
    with patch.object(handler, "_http_get_json") as mock_get:
        mock_get.return_value = {"results": []}
        try:
            handler._geocode_location("Nonexistentplacexyz")
            raise AssertionError("expected WeatherLookupError")
        except handler.WeatherLookupError as exc:
            assert "Could not find" in str(exc)


def test_geocode_location_raises_on_http_failure():
    with patch.object(handler, "_http_get_json", side_effect=Exception("network error")):
        try:
            handler._geocode_location("Seattle")
            raise AssertionError("expected WeatherLookupError")
        except handler.WeatherLookupError as exc:
            assert "Geocoding request failed" in str(exc)


def test_fetch_current_weather_raises_when_no_current_key():
    with patch.object(handler, "_http_get_json", return_value={}):
        try:
            handler._fetch_current_weather(47.6, -122.3)
            raise AssertionError("expected WeatherLookupError")
        except handler.WeatherLookupError as exc:
            assert "no current conditions" in str(exc)


def test_lambda_handler_returns_response_contract_on_success():
    """bedrock_client.py's invoke_lambda_task() requires exactly this
    shape back: response_text, input_tokens, output_tokens -- see
    handler.py's module docstring."""
    with patch.object(handler, "_http_get_json") as mock_get:
        mock_get.side_effect = [_geocode_result(), _forecast_result()]
        result = handler.lambda_handler({"input": "Seattle", "user_id": "amzn1.ask.account.TESTUSER"}, None)

    assert set(result.keys()) == {"response_text", "input_tokens", "output_tokens", "needs_clarification"}
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0
    assert "Seattle" in result["response_text"]
    assert result["needs_clarification"] is False


def test_lambda_handler_handles_missing_input_gracefully():
    result = handler.lambda_handler({"input": "", "user_id": "amzn1.ask.account.TESTUSER"}, None)
    assert "didn't catch which location" in result["response_text"]
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0
    assert result["needs_clarification"] is True


def test_lambda_handler_handles_missing_input_key_entirely():
    """event might not even have an "input" key -- must not raise
    KeyError."""
    result = handler.lambda_handler({}, None)
    assert "didn't catch which location" in result["response_text"]


def test_lambda_handler_handles_unresolvable_location_gracefully():
    """
    Regression test: a location that can't be geocoded must ask for
    clarification (needs_clarification=True) rather than returning a
    terminal failure message -- previously this ended the Alexa
    session outright the moment a location was misheard or didn't
    exist, instead of giving the user a chance to try a different city.
    """
    with patch.object(handler, "_http_get_json", return_value={"results": []}):
        result = handler.lambda_handler({"input": "Zzzznonexistentplace"}, None)

    assert "couldn't find" in result["response_text"]
    assert result["input_tokens"] == 0
    assert result["output_tokens"] == 0
    assert result["needs_clarification"] is True
