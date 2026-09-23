"""
Tests for tmdb_client.py. All HTTP calls to TMDB are mocked -- no real
network access during pytest, matching the same "no live external calls
in CI" principle used everywhere else in this project.

Field names in the mocked responses below (poster_path, overview,
vote_average, first_air_date, results[].site/type/official/key for
videos) match TMDB's real, documented response shapes -- verified
against developer.themoviedb.org's search-movie, search-tv, and
movie/tv-videos reference pages before writing these fixtures, not
invented.
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tmdb_client  # noqa: E402


def test_search_movies_normalizes_fields(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    fake_response = {
        "page": 1,
        "results": [
            {
                "id": 550,
                "title": "Fight Club",
                "overview": "A ticking-time-bomb insomniac...",
                "release_date": "1999-10-15",
                "vote_average": 8.433,
                "poster_path": "/pB8BM7pdSp6B6Ih7QZ4DrQ3PmJK.jpg",
            }
        ],
        "total_results": 1,
    }
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response) as mock_get:
        results = tmdb_client.search_movies("fight club")

    assert len(results) == 1
    assert results[0]["id"] == 550
    assert results[0]["media_type"] == "movie"
    assert results[0]["title"] == "Fight Club"
    assert results[0]["rating"] == 8.433
    assert results[0]["poster_url"] == "https://image.tmdb.org/t/p/w500/pB8BM7pdSp6B6Ih7QZ4DrQ3PmJK.jpg"

    call_args = mock_get.call_args
    assert call_args[0][0] == "/search/movie"
    assert call_args[0][1]["query"] == "fight club"


def test_search_tv_shows_normalizes_fields(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    fake_response = {
        "page": 1,
        "results": [
            {
                "id": 1399,
                "name": "Game of Thrones",
                "overview": "Seven noble families fight...",
                "first_air_date": "2011-04-17",
                "vote_average": 8.4,
                "poster_path": "/u3bZgnGQ9T01sWNhyveQz0wH0Hl.jpg",
            }
        ],
    }
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response):
        results = tmdb_client.search_tv_shows("epic fantasy")

    assert len(results) == 1
    assert results[0]["media_type"] == "tv"
    assert results[0]["title"] == "Game of Thrones"
    assert results[0]["release_date"] == "2011-04-17"


def test_search_movies_respects_max_results(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    fake_response = {"results": [{"id": i, "title": f"Movie {i}"} for i in range(10)]}
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response):
        results = tmdb_client.search_movies("test", max_results=3)

    assert len(results) == 3


def test_poster_url_returns_empty_string_for_missing_path():
    assert tmdb_client.poster_url(None) == ""
    assert tmdb_client.poster_url("") == ""


def test_poster_url_builds_full_url():
    assert tmdb_client.poster_url("/abc.jpg") == "https://image.tmdb.org/t/p/w500/abc.jpg"


def test_get_trailer_url_prefers_official_trailer(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    fake_response = {
        "results": [
            {"site": "YouTube", "type": "Teaser", "official": True, "key": "teaser123"},
            {"site": "YouTube", "type": "Trailer", "official": True, "key": "trailer456"},
            {"site": "Vimeo", "type": "Trailer", "official": True, "key": "vimeo789"},
        ]
    }
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response):
        url = tmdb_client.get_trailer_url("movie", 550)

    assert url == "https://www.youtube.com/watch?v=trailer456"


def test_get_trailer_url_falls_back_to_any_youtube_video(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    fake_response = {
        "results": [
            {"site": "YouTube", "type": "Teaser", "official": False, "key": "teaser999"},
        ]
    }
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response):
        url = tmdb_client.get_trailer_url("tv", 1399)

    assert url == "https://www.youtube.com/watch?v=teaser999"


def test_get_trailer_url_returns_empty_string_when_no_youtube_video(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    fake_response = {"results": [{"site": "Vimeo", "type": "Trailer", "official": True, "key": "vimeo1"}]}
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response):
        url = tmdb_client.get_trailer_url("movie", 1)

    assert url == ""


def test_get_trailer_url_rejects_invalid_media_type(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    try:
        tmdb_client.get_trailer_url("book", 1)
        raise AssertionError("expected TmdbClientError")
    except tmdb_client.TmdbClientError as exc:
        assert "Invalid media_type" in str(exc)


def test_missing_api_key_raises_clear_error(monkeypatch):
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    try:
        tmdb_client.search_movies("anything")
        raise AssertionError("expected TmdbClientError")
    except tmdb_client.TmdbClientError as exc:
        assert "TMDB_API_KEY" in str(exc)


def test_http_failure_wrapped_as_tmdb_client_error(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    with patch("urllib.request.urlopen", side_effect=Exception("network error")):
        try:
            tmdb_client.search_movies("anything")
            raise AssertionError("expected TmdbClientError")
        except tmdb_client.TmdbClientError as exc:
            assert "TMDB request" in str(exc)


# --------------------------------------------------------------------------
# get_watch_providers -- the "where to watch" data behind the detail
# screen's provider row. Response shape below mirrors a real TMDB
# /watch/providers response (verified against the live API), including
# the "flatrate" key name for subscription streaming.
# --------------------------------------------------------------------------

_WATCH_PROVIDERS_RESPONSE = {
    "id": 550,
    "results": {
        "AU": {
            "link": "https://www.themoviedb.org/movie/550-fight-club/watch?locale=AU",
            "flatrate": [
                {"provider_name": "Binge", "logo_path": "/binge.jpg", "provider_id": 1, "display_priority": 2},
                {"provider_name": "Netflix", "logo_path": "/netflix.jpg", "provider_id": 8, "display_priority": 1},
            ],
            "rent": [{"provider_name": "Apple TV", "logo_path": "/apple.jpg", "provider_id": 2, "display_priority": 3}],
            "buy": [{"provider_name": "Amazon Video", "logo_path": "/amazon.jpg", "provider_id": 10, "display_priority": 4}],
        },
        "US": {"link": "https://example.com/us", "flatrate": []},
    },
}


def test_get_watch_providers_normalizes_region_data(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    with patch.object(tmdb_client, "_http_get_json", return_value=_WATCH_PROVIDERS_RESPONSE):
        providers = tmdb_client.get_watch_providers("movie", 550, region="AU")

    assert providers["region"] == "AU"
    assert providers["link"].endswith("locale=AU")
    # "flatrate" is renamed to "stream", and entries are sorted by TMDB's
    # display_priority so the most prominent service appears first.
    assert [p["name"] for p in providers["stream"]] == ["Netflix", "Binge"]
    assert providers["stream"][0]["logo_url"] == "https://image.tmdb.org/t/p/w92/netflix.jpg"
    assert providers["rent"][0]["name"] == "Apple TV"
    assert providers["buy"][0]["name"] == "Amazon Video"


def test_get_watch_providers_returns_empty_for_unavailable_region(monkeypatch):
    """A title with no availability in the requested region is a normal
    outcome, not an error -- the detail screen renders a "not on
    streaming in your region" note for it."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    with patch.object(tmdb_client, "_http_get_json", return_value=_WATCH_PROVIDERS_RESPONSE):
        providers = tmdb_client.get_watch_providers("movie", 550, region="NZ")

    assert providers == {"region": "", "link": "", "stream": [], "rent": [], "buy": []}


def test_get_watch_providers_uses_region_environment_variable(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    monkeypatch.setenv("TMDB_WATCH_REGION", "us")
    with patch.object(tmdb_client, "_http_get_json", return_value=_WATCH_PROVIDERS_RESPONSE):
        providers = tmdb_client.get_watch_providers("movie", 550)

    # Lower-cased env value is upper-cased before lookup.
    assert providers["region"] == "US"


def test_get_watch_providers_caps_providers_per_category(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    many = {
        "results": {
            "AU": {
                "link": "",
                "flatrate": [
                    {"provider_name": f"Service {i}", "logo_path": f"/{i}.jpg", "display_priority": i}
                    for i in range(10)
                ],
            }
        }
    }
    with patch.object(tmdb_client, "_http_get_json", return_value=many):
        providers = tmdb_client.get_watch_providers("movie", 1, region="AU")

    assert len(providers["stream"]) == tmdb_client.MAX_PROVIDERS_PER_CATEGORY


def test_get_watch_providers_rejects_invalid_media_type(monkeypatch):
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    try:
        tmdb_client.get_watch_providers("book", 1)
        raise AssertionError("expected TmdbClientError")
    except tmdb_client.TmdbClientError as exc:
        assert "Invalid media_type" in str(exc)


def test_logo_url_empty_path_returns_empty_string():
    assert tmdb_client.logo_url(None) == ""
    assert tmdb_client.logo_url("") == ""


# --------------------------------------------------------------------------
# discover_movies_by_mood / discover_tv_shows_by_mood -- the genre-based
# discovery endpoints that replace text search for mood/genre queries.
# --------------------------------------------------------------------------


def test_discover_movies_by_mood_finds_comedy(monkeypatch):
    """Test that 'funny' mood maps to Comedy genre (35) and uses /discover/movie."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    fake_response = {
        "results": [
            {"id": 1, "title": "Superbad", "overview": "Coming of age comedy", "release_date": "2007-08-17", "vote_average": 7.6, "poster_path": "/poster1.jpg"},
            {"id": 2, "title": "The Hangover", "overview": "Vegas bachelor party gone wrong", "release_date": "2009-06-05", "vote_average": 7.7, "poster_path": "/poster2.jpg"},
        ]
    }
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response) as mock_get:
        results = tmdb_client.discover_movies_by_mood("funny", max_results=2)

    assert len(results) == 2
    assert results[0]["title"] == "Superbad"
    assert results[0]["media_type"] == "movie"
    assert results[0]["rating"] == 7.6

    # Verify the call used /discover/movie with with_genres=35 (Comedy)
    call_args = mock_get.call_args
    assert call_args[0][0] == "/discover/movie"
    assert call_args[0][1]["with_genres"] == "35"
    assert call_args[0][1]["vote_average.gte"] == "6.0"
    assert call_args[0][1]["sort_by"] == "popularity.desc"


def test_discover_movies_by_mood_handles_horror(monkeypatch):
    """Test that 'scary' mood maps to Horror genre (27)."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    fake_response = {"results": [{"id": 3, "title": "The Conjuring", "overview": "Haunted house", "release_date": "2013-07-19", "vote_average": 7.5, "poster_path": "/poster3.jpg"}]}
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response) as mock_get:
        results = tmdb_client.discover_movies_by_mood("scary horror", max_results=1)

    assert len(results) == 1
    assert results[0]["title"] == "The Conjuring"
    call_args = mock_get.call_args
    assert call_args[0][1]["with_genres"] == "27"  # Horror genre ID


def test_discover_movies_by_mood_handles_decade(monkeypatch):
    """Test that '80s' adds primary_release_date filters."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    fake_response = {"results": [{"id": 4, "title": "Back to the Future", "overview": "Time travel", "release_date": "1985-07-03", "vote_average": 8.5, "poster_path": "/poster4.jpg"}]}
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response) as mock_get:
        results = tmdb_client.discover_movies_by_mood("80s action", max_results=1)

    assert len(results) == 1
    assert results[0]["title"] == "Back to the Future"
    call_args = mock_get.call_args
    assert call_args[0][1]["primary_release_date.gte"] == "1980-01-01"
    assert call_args[0][1]["primary_release_date.lte"] == "1989-12-31"
    # Should also include Action genre (28)
    assert call_args[0][1]["with_genres"] == "28"


def test_discover_movies_by_mood_filters_by_region(monkeypatch):
    """Test that region parameter adds watch_region for provider availability
    lookups, WITHOUT filtering results down to only currently-streaming
    content (with_watch_providers/watch_monetization_types are deliberately
    NOT set -- see discover_movies_by_mood's region comment: the discover
    call should still return all popular/well-rated content for the region,
    with actual streaming availability shown per-title on the detail screen
    via get_watch_providers() instead)."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    fake_response = {"results": [{"id": 5, "title": "Available in AU", "overview": "Streaming in Australia", "release_date": "2020-01-01", "vote_average": 7.0, "poster_path": "/poster5.jpg"}]}
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response) as mock_get:
        results = tmdb_client.discover_movies_by_mood("comedy", max_results=1, region="AU")

    call_args = mock_get.call_args
    assert call_args[0][1]["watch_region"] == "AU"
    assert "with_watch_providers" not in call_args[0][1]
    assert "watch_monetization_types" not in call_args[0][1]


def test_discover_tv_shows_by_mood_finds_comedy(monkeypatch):
    """Test TV show discovery with Comedy genre (35)."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    fake_response = {"results": [{"id": 100, "name": "The Office", "overview": "Mockumentary sitcom", "first_air_date": "2005-03-24", "vote_average": 8.9, "poster_path": "/poster100.jpg"}]}
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response) as mock_get:
        results = tmdb_client.discover_tv_shows_by_mood("funny sitcom", max_results=1)

    assert len(results) == 1
    assert results[0]["title"] == "The Office"
    assert results[0]["media_type"] == "tv"
    call_args = mock_get.call_args
    assert call_args[0][0] == "/discover/tv"
    assert call_args[0][1]["with_genres"] == "35"  # Comedy for TV


def test_discover_tv_shows_by_mood_handles_sci_fi(monkeypatch):
    """Test that 'sci-fi' maps to TV Sci-Fi & Fantasy genre (10765)."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    fake_response = {"results": [{"id": 101, "name": "Stranger Things", "overview": "80s sci-fi horror", "first_air_date": "2016-07-15", "vote_average": 8.7, "poster_path": "/poster101.jpg"}]}
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response) as mock_get:
        results = tmdb_client.discover_tv_shows_by_mood("sci-fi series", max_results=1)

    call_args = mock_get.call_args
    assert call_args[0][1]["with_genres"] == "10765"  # Sci-Fi & Fantasy for TV


def test_discover_tv_shows_by_mood_filters_by_region(monkeypatch):
    """Test that region parameter adds watch_region for TV show discovery,
    without filtering to only currently-streaming content -- see
    test_discover_movies_by_mood_filters_by_region's docstring."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    fake_response = {"results": [{"id": 102, "name": "Available Show", "overview": "Streaming in GB", "first_air_date": "2020-01-01", "vote_average": 7.5, "poster_path": "/poster102.jpg"}]}
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response) as mock_get:
        results = tmdb_client.discover_tv_shows_by_mood("drama", max_results=1, region="GB")

    call_args = mock_get.call_args
    assert call_args[0][1]["watch_region"] == "GB"
    assert "with_watch_providers" not in call_args[0][1]
    assert "watch_monetization_types" not in call_args[0][1]


def test_discover_tv_shows_by_mood_falls_back_to_movie_genre_when_none(monkeypatch):
    """Test that when no TV genre exists for a mood (e.g. horror), the movie
    genre is used as fallback so we still get genre-constrained results."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    # "horror" maps to (27, None) — movie genre 27, but None for TV.
    # With the fallback, it should use 27 (Horror) instead of no genre at all.
    fake_response = {"results": [{"id": 200, "name": "The Haunting of Hill House", "overview": "Spooky mansion", "first_air_date": "2018-10-12", "vote_average": 7.4, "poster_path": "/poster200.jpg"}]}
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response) as mock_get:
        results = tmdb_client.discover_tv_shows_by_mood("scary horror", max_results=1)

    assert len(results) == 1
    assert results[0]["title"] == "The Haunting of Hill House"
    call_args = mock_get.call_args
    # Should use the movie Horror genre (27) as fallback for TV
    assert call_args[0][1]["with_genres"] == "27"


def test_discover_tv_shows_by_mood_min_rating_default_is_6(monkeypatch):
    """Test that the default min_rating is 6.0, matching discover_movies_by_mood."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    fake_response = {"results": [{"id": 300, "name": "A Show", "overview": "", "first_air_date": "2020-01-01", "vote_average": 7.0, "poster_path": "/poster300.jpg"}]}
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response) as mock_get:
        tmdb_client.discover_tv_shows_by_mood("comedy")

    call_args = mock_get.call_args
    assert call_args[0][1]["vote_average.gte"] == "6.0"


# --------------------------------------------------------------------------
# Franchise/proper-noun requests (e.g. "marvel", "star wars") match no entry
# in MOOD_TO_GENRE_IDS at all. These tests cover the genre -> TMDB keyword ->
# plain text search fallback chain -- see discover_movies_by_mood and
# discover_tv_shows_by_mood's docstrings, and _search_keyword_ids's
# docstring.
# --------------------------------------------------------------------------


def test_discover_movies_by_mood_uses_keyword_match_for_franchise(monkeypatch):
    """A franchise name with no genre match should be looked up against
    TMDB's /search/keyword endpoint, and the resulting keyword ID(s) fed
    into /discover/movie's with_keywords filter."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    keyword_response = {"results": [{"id": 180547, "name": "marvel cinematic universe"}]}
    discover_response = {
        "results": [
            {"id": 1726, "title": "Iron Man", "overview": "Genius billionaire playboy", "release_date": "2008-04-30", "vote_average": 7.6, "poster_path": "/ironman.jpg"},
        ]
    }

    def fake_get(path, params):
        if path == "/search/keyword":
            assert params["query"] == "marvel"
            return keyword_response
        assert path == "/discover/movie"
        return discover_response

    with patch.object(tmdb_client, "_http_get_json", side_effect=fake_get) as mock_get:
        results = tmdb_client.discover_movies_by_mood("marvel", max_results=5)

    assert len(results) == 1
    assert results[0]["title"] == "Iron Man"

    # Confirm both calls happened, and the discover call used with_keywords,
    # not an unfiltered request and not with_genres.
    calls = mock_get.call_args_list
    assert calls[0].args[0] == "/search/keyword"
    discover_call = calls[1]
    assert discover_call.args[0] == "/discover/movie"
    assert discover_call.args[1]["with_keywords"] == "180547"
    assert "with_genres" not in discover_call.args[1]


def test_discover_tv_shows_by_mood_uses_keyword_match_for_franchise(monkeypatch):
    """Same keyword fallback as the movie path, but for /discover/tv."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    keyword_response = {"results": [{"id": 12377, "name": "star wars"}]}
    discover_response = {
        "results": [
            {"id": 82856, "name": "The Mandalorian", "overview": "A lone bounty hunter", "first_air_date": "2019-11-12", "vote_average": 8.4, "poster_path": "/mando.jpg"},
        ]
    }

    def fake_get(path, params):
        if path == "/search/keyword":
            assert params["query"] == "star wars"
            return keyword_response
        assert path == "/discover/tv"
        return discover_response

    with patch.object(tmdb_client, "_http_get_json", side_effect=fake_get) as mock_get:
        results = tmdb_client.discover_tv_shows_by_mood("star wars", max_results=5)

    assert len(results) == 1
    assert results[0]["title"] == "The Mandalorian"

    calls = mock_get.call_args_list
    discover_call = calls[1]
    assert discover_call.args[1]["with_keywords"] == "12377"
    assert "with_genres" not in discover_call.args[1]


def test_discover_movies_by_mood_falls_back_to_text_search_when_no_keyword_match(monkeypatch):
    """When TMDB has no genre AND no keyword for the phrase (e.g. a
    specific title fragment), fall back to plain text search rather than
    an unfiltered popularity list -- this is the last link in the
    fallback chain."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    keyword_response = {"results": []}  # No keyword matches at all
    search_response = {
        "results": [
            {"id": 9999, "title": "Some Obscure Title", "overview": "A specific movie", "release_date": "2015-01-01", "vote_average": 6.5, "poster_path": "/obscure.jpg"},
        ]
    }

    def fake_get(path, params):
        if path == "/search/keyword":
            return keyword_response
        assert path == "/search/movie"
        assert params["query"] == "some obscure title"
        return search_response

    with patch.object(tmdb_client, "_http_get_json", side_effect=fake_get) as mock_get:
        results = tmdb_client.discover_movies_by_mood("some obscure title", max_results=5)

    assert len(results) == 1
    assert results[0]["title"] == "Some Obscure Title"

    calls = mock_get.call_args_list
    called_paths = [c.args[0] for c in calls]
    assert "/search/keyword" in called_paths
    assert "/discover/movie" not in called_paths
    assert "/search/movie" in called_paths


def test_discover_tv_shows_by_mood_falls_back_to_text_search_when_no_keyword_match(monkeypatch):
    """Same last-resort text-search fallback, for the TV discovery path."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    keyword_response = {"results": []}
    search_response = {
        "results": [
            {"id": 8888, "name": "Some Obscure Show", "overview": "A specific series", "first_air_date": "2019-01-01", "vote_average": 6.8, "poster_path": "/obscure_tv.jpg"},
        ]
    }

    def fake_get(path, params):
        if path == "/search/keyword":
            return keyword_response
        assert path == "/search/tv"
        return search_response

    with patch.object(tmdb_client, "_http_get_json", side_effect=fake_get) as mock_get:
        results = tmdb_client.discover_tv_shows_by_mood("some obscure show", max_results=5)

    assert len(results) == 1
    assert results[0]["title"] == "Some Obscure Show"

    calls = mock_get.call_args_list
    called_paths = [c.args[0] for c in calls]
    assert "/discover/tv" not in called_paths
    assert "/search/tv" in called_paths


def test_search_keyword_ids_returns_empty_list_on_tmdb_error(monkeypatch):
    """_search_keyword_ids should degrade to [] (not raise) if the TMDB
    keyword lookup itself fails, so a transient TMDB error on the keyword
    endpoint doesn't take down the whole discover call -- the caller's
    text-search fallback still runs."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    with patch.object(tmdb_client, "_http_get_json", side_effect=tmdb_client.TmdbClientError("boom")):
        assert tmdb_client._search_keyword_ids("marvel") == []


def test_search_keyword_ids_caps_number_of_ids(monkeypatch):
    """Only the first `max_keywords` keyword IDs are used, to avoid an
    overly broad with_keywords filter."""
    monkeypatch.setenv("TMDB_API_KEY", "fake-key")
    fake_response = {"results": [{"id": i} for i in range(10)]}
    with patch.object(tmdb_client, "_http_get_json", return_value=fake_response):
        ids = tmdb_client._search_keyword_ids("marvel", max_keywords=3)

    assert ids == [0, 1, 2]
