"""
tmdb_client.py -- thin wrapper around TMDB's (themoviedb.org) v3 REST API,
used by boredom_buster_agent.py's search_movies/search_tv_shows/
get_trailer_url tools.

WHY TMDB: a free, well-documented catalog of movies and TV shows with
poster art, synopses, ratings, and trailer references -- exactly what
Boredom Buster's recommendation grid and detail screen need (solution-
design.md Section 13). Requires a free API key (themoviedb.org/settings/
api) -- see the repo root README.md's TMDB API key step.

Authentication: TMDB supports both a v3 `api_key` query parameter and a
v4 Bearer token; both grant the same read access for these endpoints
(confirmed against TMDB's own "Application" authentication docs -- "Both
authentication methods provide the same level of access"). This module
supports BOTH, auto-detected from the credential's own shape -- see
_is_v4_token() and _http_get_json().

WHY BOTH, RATHER THAN PICKING ONE: TMDB's API settings page issues two
different credentials, and which one you get depends on where you click.
The v3 "API Key" is a 32-character hex string; the v4 "API Read Access
Token" is a much longer JWT (three dot-separated base64 segments,
starting "eyJ"). They are NOT interchangeable across transports -- a v4
token sent as `?api_key=` is rejected with HTTP 401 Unauthorized, which
is exactly the bug this auto-detection fixes: every single TMDB search
failed with 401 on the deployed agent (verified in the Runtime's own
CloudWatch Logs, then reproduced directly against the live API with the
real stored credential: as `?api_key=` -> 401, as `Authorization:
Bearer` -> 200 with results). Detecting the shape means whichever of the
two you paste into TF_VAR_tmdb_api_key just works, instead of failing at
runtime with an error that says nothing about which credential was
expected.

Response field names below (poster_path, overview, vote_average,
first_air_date, etc.) were verified directly against TMDB's own API
reference pages (developer.themoviedb.org/reference/search-movie,
.../search-tv, .../movie-videos, .../tv-series-videos), not assumed.
"""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

TMDB_BASE_URL = "https://api.themoviedb.org/3"
# Confirmed against TMDB's own image-basics documentation
# (developer.themoviedb.org/docs/image-basics) -- w500 is a real,
# commonly-used poster size, a reasonable balance between an Echo
# Show's screen resolution and payload size for the recommendations grid.
TMDB_IMAGE_BASE_URL = "https://image.tmdb.org/t/p/w500"
# Provider logos are rendered small (a row of ~48dp icons on the detail
# screen), so a smaller size than the posters' w500 keeps the response
# payload and the device's image fetches light. w92 is a real TMDB logo
# size per developer.themoviedb.org/docs/image-basics.
TMDB_LOGO_BASE_URL = "https://image.tmdb.org/t/p/w92"
HTTP_TIMEOUT_SECONDS = 8

# Default country for "where to watch" lookups. TMDB's watch-providers
# endpoint returns availability keyed by ISO-3166-1 country code, and
# streaming rights differ per country, so there is no sensible global
# answer -- an explicit region is required. Overridable via the
# TMDB_WATCH_REGION environment variable (set in
# terraform/agentcore_runtime.tf) rather than hardcoded, since the right
# value depends on where the device owner actually lives.
DEFAULT_WATCH_REGION = "AU"

# How many providers to keep per category (stream/rent/buy). TMDB can
# return a dozen or more for a popular title; the detail screen only has
# room for a handful, and every extra entry inflates the Alexa response
# payload (the recommendation list is echoed back in sessionAttributes,
# which counts against Alexa's response size limit) for no visible gain.
MAX_PROVIDERS_PER_CATEGORY = 4

# TMDB Genre IDs (from TMDB's genre/movie/list and genre/tv/list endpoints)
# Used with the /discover endpoint's with_genres parameter for genre-based
# search instead of text-based search. Verified against TMDB's API reference.
MOVIE_GENRE_IDS = {
    "action": 28,
    "adventure": 12,
    "animation": 16,
    "comedy": 35,
    "crime": 80,
    "documentary": 99,
    "drama": 18,
    "family": 10751,
    "fantasy": 14,
    "history": 36,
    "horror": 27,
    "music": 10402,
    "mystery": 9648,
    "romance": 10749,
    "science fiction": 878,
    "sci-fi": 878,
    "tv movie": 10770,
    "thriller": 53,
    "war": 10752,
    "western": 37,
}

TV_GENRE_IDS = {
    "action & adventure": 10759,
    "animation": 16,
    "comedy": 35,
    "crime": 80,
    "documentary": 99,
    "drama": 18,
    "family": 10751,
    "kids": 10762,
    "mystery": 9648,
    "news": 10763,
    "reality": 10764,
    "sci-fi & fantasy": 10765,
    "soap": 10766,
    "talk": 10767,
    "war & politics": 10768,
    "western": 37,
}

# Maps user mood/genre phrases to TMDB genre IDs.
# Keys are lowercase phrases that might appear in user requests.
# Values are (movie_genre_id, tv_genre_id) tuples.
# If a genre doesn't exist for one media type, use None.
MOOD_TO_GENRE_IDS = {
    # Comedy / Funny
    "funny": (35, 35),
    "comedy": (35, 35),
    "hilarious": (35, 35),
    "laugh": (35, 35),
    "humor": (35, 35),
    # Horror / Scary
    "scary": (27, None),
    "horror": (27, None),
    "frightening": (27, None),
    "creepy": (27, None),
    "spooky": (27, None),
    # Thriller / Gripping
    "gripping": (53, 53),
    "thriller": (53, 9648),  # TV uses Mystery for thriller-like content
    "suspense": (53, 9648),
    "intense": (53, 53),
    "edge of your seat": (53, 53),
    # Feel-good / Uplifting
    "feel-good": (35, 35),
    "feel good": (35, 35),
    "uplifting": (35, 35),
    "heartwarming": (10751, 10751),
    # Family / Kids
    "family": (10751, 10751),
    "kids": (10751, 10762),
    "children": (10751, 10762),
    "childrens": (10751, 10762),
    # Action
    "action": (28, 10759),
    "adventure": (12, 10759),
    # Sci-Fi / Fantasy
    "sci-fi": (878, 10765),
    "science fiction": (878, 10765),
    "fantasy": (14, 10765),
    "magical": (14, 10765),
    "magic": (14, 10765),
    # Romance
    "romantic": (10749, 10749),
    "romance": (10749, 10749),
    "love": (10749, 10749),
    # Drama
    "drama": (18, 18),
    "emotional": (18, 18),
    "tearjerker": (18, 18),
    # Crime / Mystery
    "crime": (80, 80),
    "detective": (9648, 9648),
    "mystery": (9648, 9648),
    "whodunit": (9648, 9648),
    # Documentary
    "documentary": (99, 99),
    "docu": (99, 99),
    # Animation
    "animated": (16, 16),
    "animation": (16, 16),
    "cartoon": (16, 16),
    # Anime (special case - TMDB doesn't have a specific anime genre ID)
    # We'll use animation + search query for this
    "anime": (16, 16),
}

# Decade mappings for era-based requests
DECADE_TO_YEAR_RANGE = {
    "80s": ("1980-01-01", "1989-12-31"),
    "1980s": ("1980-01-01", "1989-12-31"),
    "90s": ("1990-01-01", "1999-12-31"),
    "1990s": ("1990-01-01", "1999-12-31"),
    "2000s": ("2000-01-01", "2009-12-31"),
    "2010s": ("2010-01-01", "2019-12-31"),
    "2020s": ("2020-01-01", "2029-12-31"),
    "classic": (None, "1989-12-31"),  # Before 1990
    "old": (None, "1989-12-31"),
    "retro": (None, "1999-12-31"),  # Before 2000
}


class TmdbClientError(Exception):
    """Raised when a TMDB request fails or the API key is missing."""


# Cached across invocations within the same warm AgentCore Runtime
# session, so a deployed agent doesn't call SSM's GetParameter on
# every single request -- same "cache the secret in memory for the
# life of the process" pattern used throughout this project (e.g.
# alexa_lambda's warm-Lambda module-level caching).
_cached_api_key: str | None = None


def _get_api_key() -> str:
    """
    Resolves the TMDB API key. Two paths, checked in order:

    1. `TMDB_API_KEY` set directly -- the raw key value. Used for local
       development/testing (this is what every test in
       tests/test_tmdb_client.py sets via monkeypatch) -- convenient for
       a quick local run, but this project's Terraform never sets this
       directly on the deployed agent, since that would put the secret
       in plaintext in the aws_bedrockagentcore_agent_runtime resource's
       environment_variables (visible in Terraform state and the
       console) -- exactly what storing it in SSM Parameter Store as a
       SecureString was meant to avoid.
    2. `TMDB_API_KEY_PARAMETER_NAME` set instead -- the SSM Parameter
       Store parameter *name* (not a secret itself), fetched via
       ssm:GetParameter with WithDecryption=True at call time. This is
       the path the deployed agent actually uses -- see
       terraform/agentcore_runtime.tf's environment_variables and the
       execution role's ssm:GetParameter grant scoped to this one
       parameter's ARN.
    """
    global _cached_api_key
    if _cached_api_key:
        return _cached_api_key

    api_key = os.environ.get("TMDB_API_KEY", "")
    if api_key:
        _cached_api_key = api_key
        return api_key

    parameter_name = os.environ.get("TMDB_API_KEY_PARAMETER_NAME", "")
    if parameter_name:
        import boto3  # local import -- only needed on this path, not local dev

        region = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
        try:
            ssm = boto3.client("ssm", region_name=region)
            response = ssm.get_parameter(Name=parameter_name, WithDecryption=True)
        except Exception as exc:  # noqa: BLE001
            raise TmdbClientError(f"Failed to read TMDB API key from SSM parameter '{parameter_name}': {exc}") from exc
        api_key = response["Parameter"]["Value"]
        _cached_api_key = api_key
        return api_key

    raise TmdbClientError(
        "Neither TMDB_API_KEY nor TMDB_API_KEY_PARAMETER_NAME is set -- see "
        "the repo root README.md for how to get a free TMDB API key and "
        "wire it into this agent's environment."
    )


def _is_v4_token(credential: str) -> bool:
    """
    True if `credential` looks like TMDB's v4 "API Read Access Token"
    rather than its v3 "API Key".

    The two are trivially distinguishable and the check is deliberately
    structural, not a length threshold: a v4 token is a JWT -- three
    dot-separated base64url segments whose header segment begins "eyJ"
    (the base64 encoding of `{"`). A v3 key is a 32-character hex string
    with no dots at all. Verified against the real credential issued by
    TMDB for this project.
    """
    return credential.count(".") == 2 and credential.startswith("eyJ")


def _http_get_json(path: str, params: dict) -> dict:
    """
    Performs an authenticated GET against TMDB and returns the parsed
    JSON body.

    The credential is sent as an `Authorization: Bearer` header for a v4
    read access token, or as the `api_key` query parameter for a v3 API
    key -- see _is_v4_token and this module's docstring for why sending
    the wrong one produces an unhelpful HTTP 401.
    """
    credential = _get_api_key()
    query_params = dict(params)
    headers = {"accept": "application/json"}

    if _is_v4_token(credential):
        headers["Authorization"] = f"Bearer {credential}"
    else:
        query_params["api_key"] = credential

    query_string = urllib.parse.urlencode(query_params)
    full_url = f"{TMDB_BASE_URL}{path}?{query_string}"
    request = urllib.request.Request(full_url, headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            # Called out specifically because a bare "HTTP Error 401" gave
            # no clue which of TMDB's two credentials was in play -- the
            # exact situation that made this bug hard to diagnose from the
            # agent's logs alone. The credential VALUE is never logged.
            credential_kind = "v4 read access token" if _is_v4_token(credential) else "v3 API key"
            raise TmdbClientError(
                f"TMDB rejected the request to {path} with HTTP 401 Unauthorized. The stored "
                f"credential was detected as a {credential_kind} and sent accordingly, so the "
                f"credential itself is most likely invalid, revoked, or truncated. Check the value "
                f"in SSM Parameter Store against themoviedb.org/settings/api."
            ) from exc
        raise TmdbClientError(f"TMDB request to {path} failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise TmdbClientError(f"TMDB request to {path} failed: {exc}") from exc


def logo_url(logo_path: str | None) -> str:
    """Builds a full provider-logo image URL from TMDB's relative
    logo_path, or "" if the provider has no logo."""
    if not logo_path:
        return ""
    return f"{TMDB_LOGO_BASE_URL}{logo_path}"


def poster_url(poster_path: str | None) -> str:
    """Builds a full poster image URL from TMDB's relative poster_path,
    or "" if no poster is available for a title -- APL response builders
    should handle the empty case with a placeholder image rather than a
    broken URL."""
    if not poster_path:
        return ""
    return f"{TMDB_IMAGE_BASE_URL}{poster_path}"


def search_movies(query: str, max_results: int = 5) -> list[dict]:
    """
    Searches TMDB for movies matching `query`. Returns a normalized list
    of dicts (not TMDB's raw response shape) so boredom_buster_agent.py's
    tool functions and the APL response builders don't need to know
    TMDB's exact field names -- see solution-design.md Section 12.2's
    search_movies tool.
    """
    data = _http_get_json("/search/movie", {"query": query, "include_adult": "false"})
    results = data.get("results", [])[:max_results]
    return [
        {
            "id": item["id"],
            "media_type": "movie",
            "title": item.get("title", ""),
            "overview": item.get("overview", ""),
            "release_date": item.get("release_date", ""),
            "rating": item.get("vote_average", 0),
            "poster_url": poster_url(item.get("poster_path")),
        }
        for item in results
    ]


def _search_keyword_ids(query: str, max_keywords: int = 3) -> list[int]:
    """
    Looks up TMDB keyword IDs matching `query` via /search/keyword, for
    use with /discover's with_keywords filter.

    WHY THIS EXISTS: discover_movies_by_mood/discover_tv_shows_by_mood's
    MOOD_TO_GENRE_IDS mapping only covers generic moods/genres ("funny",
    "scary", "action"). A franchise or proper-noun request -- "marvel",
    "star wars", "harry potter" -- matches none of those keys, so
    genre_ids stays empty. Without a keyword fallback, the /discover call
    would fall through to an unfiltered popularity-sorted list with no
    real connection to the franchise that was asked for. TMDB's keyword
    database is how franchises are actually tagged on individual titles
    (e.g. the keyword "marvel cinematic universe" or "superhero"), so
    searching keywords first and feeding matched IDs into with_keywords
    is what correctly constrains the discover call to the franchise
    itself, instead of an unfiltered popularity list.

    Returns up to `max_keywords` keyword IDs, or [] if TMDB has no
    keyword matching `query` at all (e.g. a genuinely generic phrase
    that isn't tagged as a keyword) or the request fails -- callers
    treat an empty list as "no keyword match" and fall back further
    (see discover_movies_by_mood/discover_tv_shows_by_mood's keyword ->
    text-search fallback chain), not as an error.
    """
    try:
        data = _http_get_json("/search/keyword", {"query": query})
    except TmdbClientError:
        return []
    return [item["id"] for item in (data.get("results") or [])[:max_keywords] if "id" in item]


def _text_search_by_mood(mood: str, media_type: str, max_results: int) -> list[dict]:
    """
    Falls back to TMDB's plain title/overview text search
    (/search/movie or /search/tv) when neither a genre NOR a keyword
    matched `mood` -- see discover_movies_by_mood/discover_tv_shows_by_mood's
    docstrings for the fallback chain this is the last link of.

    This deliberately does NOT apply the discover functions' quality
    filters (vote_count.gte/vote_average.gte) -- those exist to keep an
    UNFILTERED popularity list from returning obscure/low-quality
    titles, but here the user named something specific (a franchise,
    a title fragment), so the right results are whatever actually
    matches that name, not a quality-filtered subset of it.
    """
    search_fn = search_movies if media_type == "movie" else search_tv_shows
    return search_fn(mood, max_results=max_results)


def discover_movies_by_mood(
    mood: str,
    media_type: str = "movie",
    max_results: int = 5,
    region: str | None = None,
    min_rating: float = 6.0,
) -> list[dict]:
    """
    Uses TMDB's /discover/movie endpoint to find movies by genre/mood
    rather than text search. This is the CORRECT way to find "funny movies"
    or "scary movies" -- the /search endpoint only matches text in
    titles/overviews, which is why "funny" returned movies with "funny" in
    the title.

    Parameters:
    - mood: User's mood/genre request (e.g., "funny", "scary", "action")
    - media_type: "movie" or "tv" (currently only movie is implemented)
    - max_results: Maximum results to return
    - region: ISO-3166-1 country code for watch provider filtering (optional)
    - min_rating: Minimum TMDB vote average (default 6.0 for quality)

    Returns normalized list of movie dicts with watch provider info.

    FALLBACK CHAIN for franchise/proper-noun requests (e.g. "marvel",
    "star wars", "spiderman") that match no entry in MOOD_TO_GENRE_IDS
    at all:
    1. Genre match (existing behavior, unchanged for real moods/genres).
    2. TMDB keyword match (_search_keyword_ids + with_keywords) -- this
       is what actually constrains a franchise search; see
       _search_keyword_ids's docstring.
    3. Plain text search (_text_search_by_mood) as a last resort, if
       TMDB has no keyword for the phrase either -- still better than
       an unrelated popularity list for a request this specific.
    """
    # Parse mood to extract genre, decade, and other qualifiers
    mood_lower = mood.lower().strip()

    # Build discover parameters
    discover_params = {
        "sort_by": "popularity.desc",
        "include_adult": "false",
        "include_video": "false",
        "vote_count.gte": "100",  # At least 100 votes for reliability
        "vote_average.gte": str(min_rating),
        "page": 1,
    }

    # Check for genre mapping
    genre_ids = []
    matched_genre = None
    for mood_key, (movie_genre_id, tv_genre_id) in MOOD_TO_GENRE_IDS.items():
        if mood_key in mood_lower:
            genre_id = movie_genre_id if media_type == "movie" else tv_genre_id
            if genre_id is not None and genre_id not in genre_ids:
                genre_ids.append(genre_id)
                matched_genre = mood_key

    if genre_ids:
        # Use the first matched genre (could combine multiple with comma)
        discover_params["with_genres"] = ",".join(str(gid) for gid in genre_ids)

    # Check for decade/era
    for decade_key, (start_date, end_date) in DECADE_TO_YEAR_RANGE.items():
        if decade_key in mood_lower:
            if start_date:
                discover_params["primary_release_date.gte"] = start_date
            if end_date:
                discover_params["primary_release_date.lte"] = end_date
            break

    # If region specified, add watch region parameter for provider availability lookups.
    # Note: We only set watch_region (not with_watch_providers) because we want to
    # show all content with ratings/popularity data for that region, not filter to
    # only content currently streaming. The actual streaming availability is shown
    # per-title on the detail screen via get_watch_providers().
    if region:
        discover_params["watch_region"] = region.upper()

    # No genre matched at all -- this is likely a franchise/proper-noun
    # request ("marvel superhero", "star wars"), not a mood. Try a TMDB
    # keyword match before falling all the way back to an unfiltered
    # discover call -- see this function's docstring's fallback chain.
    if not genre_ids:
        keyword_ids = _search_keyword_ids(mood_lower)
        if keyword_ids:
            discover_params["with_keywords"] = "|".join(str(kid) for kid in keyword_ids)
        else:
            # Neither a genre nor a keyword matched -- an unfiltered
            # discover call would return a generic popularity list
            # unrelated to what was asked for. Fall back to plain text
            # search instead (which at least matches the phrase itself)
            # WITHOUT ever making the unfiltered /discover/movie call.
            return _text_search_by_mood(mood, media_type="movie", max_results=max_results)

    # Use discover endpoint
    data = _http_get_json("/discover/movie", discover_params)
    results = data.get("results", [])[:max_results]

    # Normalize results
    normalized = [
        {
            "id": item["id"],
            "media_type": "movie",
            "title": item.get("title", ""),
            "overview": item.get("overview", ""),
            "release_date": item.get("release_date", ""),
            "rating": item.get("vote_average", 0),
            "poster_url": poster_url(item.get("poster_path")),
        }
        for item in results
    ]

    return normalized


def discover_tv_shows_by_mood(
    mood: str,
    max_results: int = 5,
    region: str | None = None,
    min_rating: float = 6.0,
) -> list[dict]:
    """
    Uses TMDB's /discover/tv endpoint to find TV shows by genre/mood.
    Similar to discover_movies_by_mood but for TV shows -- see that
    function's docstring for the genre -> keyword -> text-search
    fallback chain this also implements, for the same reason (a
    franchise/proper-noun request like "marvel tv shows" matches no
    MOOD_TO_GENRE_IDS entry, so it needs a TMDB keyword or plain text
    match instead of falling through to an unfiltered popularity list).
    """
    mood_lower = mood.lower().strip()

    discover_params = {
        "sort_by": "popularity.desc",
        "include_adult": "false",
        "include_null_first_air_dates": "false",
        "vote_count.gte": "50",
        "vote_average.gte": str(min_rating),
        "page": 1,
    }

    # Check for genre mapping
    genre_ids = []
    for mood_key, (movie_genre_id, tv_genre_id) in MOOD_TO_GENRE_IDS.items():
        if mood_key in mood_lower:
            if tv_genre_id is not None:
                genre_ids.append(tv_genre_id)

    # If no TV genre matched (e.g. "horror" has None for TV), fall back to
    # the movie genre so we still get a genre-constrained search rather than
    # an unconstrained one -- an empty with_genres filter, combined with
    # region/streaming filters and min_rating, returns zero results from TMDB.
    if not genre_ids:
        for mood_key, (movie_genre_id, tv_genre_id) in MOOD_TO_GENRE_IDS.items():
            if mood_key in mood_lower and movie_genre_id is not None:
                genre_ids.append(movie_genre_id)
                break

    if genre_ids:
        discover_params["with_genres"] = ",".join(str(gid) for gid in genre_ids)

    # Check for decade/era
    for decade_key, (start_date, end_date) in DECADE_TO_YEAR_RANGE.items():
        if decade_key in mood_lower:
            if start_date:
                discover_params["first_air_date.gte"] = start_date
            if end_date:
                discover_params["first_air_date.lte"] = end_date
            break

    # If region specified, add watch region parameter for provider availability lookups.
    # Note: We only set watch_region (not with_watch_providers) because we want to
    # show all content with ratings/popularity data for that region, not filter to
    # only content currently streaming. The actual streaming availability is shown
    # per-title on the detail screen via get_watch_providers().
    if region:
        discover_params["watch_region"] = region.upper()

    # No genre matched (not even the movie-genre fallback above) -- likely
    # a franchise/proper-noun request. Try a TMDB keyword match before an
    # unfiltered discover call -- see discover_movies_by_mood's docstring.
    if not genre_ids:
        keyword_ids = _search_keyword_ids(mood_lower)
        if keyword_ids:
            discover_params["with_keywords"] = "|".join(str(kid) for kid in keyword_ids)
        else:
            # Neither a genre nor a keyword matched -- fall back to plain
            # text search WITHOUT ever making the unfiltered /discover/tv
            # call, rather than returning an unfiltered popularity list.
            return _text_search_by_mood(mood, media_type="tv", max_results=max_results)

    data = _http_get_json("/discover/tv", discover_params)
    results = data.get("results", [])[:max_results]

    normalized = [
        {
            "id": item["id"],
            "media_type": "tv",
            "title": item.get("name", ""),
            "overview": item.get("overview", ""),
            "release_date": item.get("first_air_date", ""),
            "rating": item.get("vote_average", 0),
            "poster_url": poster_url(item.get("poster_path")),
        }
        for item in results
    ]

    return normalized


def search_tv_shows(query: str, max_results: int = 5) -> list[dict]:
    """Same as search_movies, but for TV shows -- see solution-design.md
    Section 12.2's search_tv_shows tool. TMDB's TV endpoint uses `name`/
    `first_air_date` instead of movie's `title`/`release_date`; both are
    normalized to the same output shape here so callers don't need to
    branch on media type."""
    data = _http_get_json("/search/tv", {"query": query, "include_adult": "false"})
    results = data.get("results", [])[:max_results]

    return [
        {
            "id": item["id"],
            "media_type": "tv",
            "title": item.get("name", ""),
            "overview": item.get("overview", ""),
            "release_date": item.get("first_air_date", ""),
            "rating": item.get("vote_average", 0),
            "poster_url": poster_url(item.get("poster_path")),
        }
        for item in results
    ]


def get_trailer_url(media_type: str, tmdb_id: int) -> str:
    """
    Looks up a title's official YouTube trailer via TMDB's /videos
    endpoint and returns a full youtube.com watch URL, or "" if no
    trailer is found. `media_type` must be "movie" or "tv".

    Only YouTube-hosted videos are considered -- TMDB also stores Vimeo
    references, but this project's QR-code trailer workaround (solution-
    design.md Section 13.2) specifically targets YouTube, so a Vimeo-
    only title falls through to the "no trailer" case rather than
    generating a QR code the workaround wasn't designed for.
    """
    if media_type not in ("movie", "tv"):
        raise TmdbClientError(f"Invalid media_type for get_trailer_url: {media_type}")

    data = _http_get_json(f"/{media_type}/{tmdb_id}/videos", {})
    videos = data.get("results", [])

    # Prefer an official trailer; fall back to any YouTube video (e.g. a
    # teaser) if no official trailer exists, since "no video at all" is
    # a worse user experience than a non-official one.
    official_trailers = [v for v in videos if v.get("site") == "YouTube" and v.get("type") == "Trailer" and v.get("official")]
    any_youtube = [v for v in videos if v.get("site") == "YouTube"]

    chosen = (official_trailers or any_youtube)
    if not chosen:
        return ""

    video_key = chosen[0]["key"]

    return f"https://www.youtube.com/watch?v={video_key}"


def get_watch_providers(media_type: str, tmdb_id: int, region: str | None = None) -> dict:
    """
    Looks up where a title can be watched in `region` (an ISO-3166-1
    country code, e.g. "AU"), via TMDB's /watch/providers endpoint.
    Returns a normalized dict:

        {"region": "AU",
         "link": "https://www.themoviedb.org/movie/550-fight-club/watch?locale=AU",
         "stream":  [{"name": "Netflix", "logo_url": "https://..."}],
         "rent":    [...],
         "buy":     [...]}

    All three provider lists may be empty (a title with no availability
    in that region at all), and `link`/`region` may be "" -- the detail
    screen (alexa_lambda/utils/apl.py's build_detail_document) renders a
    "not currently streaming in your region" note in that case rather
    than an empty section, so an empty result is a normal outcome, not
    an error.

    TMDB's response shape (`{"id": N, "results": {"AU": {"link": ...,
    "flatrate": [...], "rent": [...], "buy": [...]}}}`) and the
    "flatrate" naming for subscription streaming were verified against a
    real API response, not assumed. "flatrate" is renamed to "stream"
    here because that's what it means to a user reading it off a screen.

    ATTRIBUTION: TMDB sources this availability data from JustWatch and
    its terms require attributing them for it, so the detail screen
    renders a "Streaming data by JustWatch" line whenever any provider
    is shown -- see build_detail_document.
    """
    if media_type not in ("movie", "tv"):
        raise TmdbClientError(f"Invalid media_type for get_watch_providers: {media_type}")

    resolved_region = (region or os.environ.get("TMDB_WATCH_REGION") or DEFAULT_WATCH_REGION).upper()

    data = _http_get_json(f"/{media_type}/{tmdb_id}/watch/providers", {})
    region_data = (data.get("results") or {}).get(resolved_region) or {}

    def _providers(key: str) -> list[dict]:
        entries = region_data.get(key) or []
        # TMDB returns a display_priority per provider; sort by it so the
        # most prominent services appear first in the (truncated) list,
        # rather than relying on TMDB's array order.
        entries = sorted(entries, key=lambda p: p.get("display_priority", 999))
        return [
            {"name": p.get("provider_name", ""), "logo_url": logo_url(p.get("logo_path"))}
            for p in entries[:MAX_PROVIDERS_PER_CATEGORY]
            if p.get("provider_name")
        ]

    return {
        "region": resolved_region if region_data else "",
        "link": region_data.get("link", ""),
        "stream": _providers("flatrate"),
        "rent": _providers("rent"),
        "buy": _providers("buy"),
    }
