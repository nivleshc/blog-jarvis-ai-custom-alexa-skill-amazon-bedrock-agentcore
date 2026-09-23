#!/usr/bin/env python3
"""
handler.py -- the GetWeather task, running as a plain AWS Lambda function
(the task registry's "lambda" backend type).

GetWeather is a deterministic API lookup (geocode a location, fetch a
forecast, map a weather code to a description) with no decision-making,
tool selection, or memory involved, so it runs as a plain Lambda function
rather than a Bedrock AgentCore Runtime agent -- AgentCore's containerized
deployment model is unnecessary complexity for a task this simple.
`BoredomBuster` (agents/boredom_buster_agent/) is this project's actual
AgentCore showcase, since it genuinely needs AgentCore's multi-step
reasoning and persistent memory.

RESPONSE CONTRACT (required by alexa_lambda/bedrock_client.py's
invoke_lambda_task()):
    {"response_text": "...", "input_tokens": N, "output_tokens": N}
input_tokens/output_tokens are correctly 0 here since no Bedrock model
call is made -- this is a deterministic API lookup, not an LLM task.

REQUEST CONTRACT: alexa_lambda/router.py invokes this function with
payload={"input": query_text, "user_id": user_id}, where query_text is
the raw value of GetWeatherIntent's Location slot (e.g. "Seattle").
`user_id` is included for consistency with the other backend types'
payload shape and potential future use (e.g. per-user unit preferences)
but is not currently used by this handler.
"""

import json
import urllib.parse
import urllib.request

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HTTP_TIMEOUT_SECONDS = 8

# Open-Meteo's numeric weather codes map to these WMO-standard
# descriptions -- verified against Open-Meteo's own documented code
# table (open-meteo.com/en/docs), not guessed. Only a practical subset
# is included; anything not listed falls back to a generic description
# rather than raising, since a spoken weather report should always
# produce *some* sentence.
_WEATHER_CODE_DESCRIPTIONS = {
    0: "clear sky",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "foggy",
    48: "foggy with frost",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    61: "light rain",
    63: "moderate rain",
    65: "heavy rain",
    71: "light snow",
    73: "moderate snow",
    75: "heavy snow",
    80: "light rain showers",
    81: "moderate rain showers",
    82: "violent rain showers",
    95: "a thunderstorm",
    96: "a thunderstorm with light hail",
    99: "a thunderstorm with heavy hail",
}


class WeatherLookupError(Exception):
    """Raised when the location can't be geocoded or weather can't be
    fetched -- caught in the handler so it always returns a spoken-
    friendly response_text rather than propagating an exception, which
    would surface as a Lambda FunctionError to bedrock_client.py's
    invoke_lambda_task() and be treated as a hard failure of the whole
    call rather than a graceful "couldn't find that place" response."""


def _http_get_json(url: str, params: dict) -> dict:
    query_string = urllib.parse.urlencode(params)
    full_url = f"{url}?{query_string}"
    with urllib.request.urlopen(full_url, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))


def _geocode_location(location: str) -> tuple:
    """Resolve a place name to (latitude, longitude, resolved_name) via
    Open-Meteo's free geocoding endpoint. Raises WeatherLookupError if
    the location can't be resolved to any result."""
    try:
        data = _http_get_json(GEOCODING_URL, {"name": location, "count": 1})
    except Exception as exc:  # noqa: BLE001
        raise WeatherLookupError(f"Geocoding request failed: {exc}") from exc

    results = data.get("results") or []
    if not results:
        raise WeatherLookupError(f"Could not find a location matching '{location}'")

    top = results[0]
    resolved_name = top.get("name", location)
    country = top.get("country")
    display_name = f"{resolved_name}, {country}" if country else resolved_name
    return top["latitude"], top["longitude"], display_name


def _fetch_current_weather(latitude: float, longitude: float) -> dict:
    try:
        data = _http_get_json(
            FORECAST_URL,
            {
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,weather_code",
                # Celsius is Open-Meteo's own default unit for
                # temperature_2m -- no temperature_unit param needed at
                # all to get Celsius output.
            },
        )
    except Exception as exc:  # noqa: BLE001
        raise WeatherLookupError(f"Weather forecast request failed: {exc}") from exc

    current = data.get("current")
    if not current:
        raise WeatherLookupError("Weather service returned no current conditions")
    return current


def get_weather_summary(location: str) -> str:
    """
    Core logic, factored out from the handler so it's directly unit
    testable without going through the Lambda event/context wrapper.
    Returns a complete, spoken-friendly sentence.
    """
    latitude, longitude, display_name = _geocode_location(location)
    current = _fetch_current_weather(latitude, longitude)

    temperature_c = current.get("temperature_2m")
    weather_code = current.get("weather_code")
    description = _WEATHER_CODE_DESCRIPTIONS.get(weather_code, "conditions I can't fully describe")

    if temperature_c is None:
        return f"I found {display_name}, but couldn't get the current temperature."

    return f"Right now in {display_name}, it's {round(temperature_c)} degrees Celsius with {description}."


def _get_device_location(api_endpoint: str, api_access_token: str, device_id: str) -> str | None:
    """
    Attempt to get the user's configured location from Alexa Device Address API.
    Returns a location string (city name or postal code) if available, None otherwise.

    Uses the Device Address API to get the user's full address, then extracts
    the city for weather lookup. Handles permission errors gracefully.
    """
    if not all([api_endpoint, api_access_token, device_id]):
        return None

    try:
        # Request full address from Alexa Device Address API
        url = f"{api_endpoint}/v1/devices/{device_id}/settings/address"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {api_access_token}")

        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
            address_data = json.loads(resp.read().decode("utf-8"))

        # Extract city from address
        city = address_data.get("city")
        if city:
            return city

        # Fallback to postal code if city not available
        postal_code = address_data.get("postalCode")
        if postal_code:
            return postal_code

    except urllib.error.HTTPError as exc:
        # 403 Forbidden means user hasn't granted device address permission
        if exc.code == 403:
            return None
        # Other HTTP errors also return None (graceful degradation)
        return None
    except Exception:  # noqa: BLE001
        # Any other error (network, timeout, etc.) - gracefully degrade
        return None

    return None


def lambda_handler(event: dict, context) -> dict:  # noqa: ARG001 -- context required by the Lambda runtime contract
    """
    Plain AWS Lambda entrypoint. `event` is whatever
    bedrock_client.invoke_lambda_task() sends -- {"input": "<Location
    slot value>", "user_id": "<Alexa userId>", "api_endpoint": "<Alexa API endpoint>",
    "api_access_token": "<API access token>", "device_id": "<device ID>"}.

    Always returns the {"response_text", "input_tokens", "output_tokens", "needs_clarification"}
    shape described in this file's module docstring, even on failure, so
    the calling Lambda never has to guess at a missing key.

    `needs_clarification` is True when the location genuinely couldn't be
    resolved -- e.g. it was misheard, too vague, or doesn't exist.
    handlers/handler.py reads this flag to re-elicit the Location slot
    via a real Dialog.ElicitSlot directive, keeping the session open for
    another attempt rather than ending the conversation on a terminal
    response (build_speech_response's should_end_session defaults to
    True) -- see that module's _handle_intent_request.

    DEVICE LOCATION SUPPORT: If no location is provided in the input,
    attempts to use the Alexa Device Address API to get the user's
    configured location. Only asks for location if device location is
    unavailable or permission is denied.
    """
    location = (event.get("input") or "").strip()

    # If no location provided by user, try to get device location
    if not location:
        api_endpoint = event.get("api_endpoint")
        api_access_token = event.get("api_access_token")
        device_id = event.get("device_id")

        location = _get_device_location(api_endpoint, api_access_token, device_id)

        if not location:
            # Device location unavailable - ask user for location
            return {
                "response_text": "I didn't catch which location you wanted the weather for. What city would you like?",
                "input_tokens": 0,
                "output_tokens": 0,
                "needs_clarification": True,
            }

    try:
        response_text = get_weather_summary(location)
        needs_clarification = False
    except WeatherLookupError:
        response_text = f"Sorry, I couldn't find {location}. What city would you like the weather for?"
        needs_clarification = True

    return {
        "response_text": response_text,
        "input_tokens": 0,
        "output_tokens": 0,
        "needs_clarification": needs_clarification,
    }
