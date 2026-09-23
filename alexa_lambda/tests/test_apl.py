"""
Tests for utils/apl.py -- Boredom Buster's APL document builders,
solution-design.md Section 13.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils import apl


def _sample_recommendation(**overrides):
    base = {
        "id": 550,
        "media_type": "movie",
        "title": "Fight Club",
        "overview": "A ticking-time-bomb insomniac...",
        "release_date": "1999-10-15",
        "rating": 8.433,
        "poster_url": "https://image.tmdb.org/t/p/w500/abc.jpg",
        "trailer_url": "https://www.youtube.com/watch?v=abc123",
    }
    base.update(overrides)
    return base


def test_supports_apl_true_when_interface_declared():
    event = {
        "context": {
            "System": {"device": {"supportedInterfaces": {"Alexa.Presentation.APL": {}}}},
        }
    }
    assert apl.supports_apl(event) is True


def test_supports_apl_false_when_interface_absent():
    event = {"context": {"System": {"device": {"supportedInterfaces": {}}}}}
    assert apl.supports_apl(event) is False


def test_supports_apl_false_on_malformed_event():
    assert apl.supports_apl({}) is False
    assert apl.supports_apl({"context": {}}) is False


def test_qr_code_url_encodes_target_url():
    url = apl.qr_code_url("https://www.youtube.com/watch?v=abc123")
    assert url.startswith(apl.QR_CODE_BASE_URL)
    assert "size=300x300" in url
    assert "abc123" in url
    # The target URL's own query string must be percent-encoded, not
    # passed through raw (which would corrupt the outer query string).
    assert "watch%3Fv%3Dabc123" in url or "watch%3Fv=abc123" in url or "%3F" in url


def test_qr_code_url_returns_empty_string_for_no_trailer():
    assert apl.qr_code_url("") == ""


def test_build_recommendations_document_has_valid_apl_structure():
    recommendations = [_sample_recommendation(title="Fight Club"), _sample_recommendation(title="Se7en", id=807)]
    document, datasources = apl.build_recommendations_document(recommendations, "movie")

    assert document["type"] == "APL"
    assert document["version"] == apl.APL_VERSION
    assert "mainTemplate" in document

    items = datasources["recommendationsData"]["properties"]["items"]
    assert len(items) == 2
    assert items[0]["title"] == "Fight Club"
    assert items[1]["title"] == "Se7en"


def test_build_recommendations_document_uses_placeholder_for_missing_poster():
    recommendations = [_sample_recommendation(poster_url="")]
    _document, datasources = apl.build_recommendations_document(recommendations, "movie")

    items = datasources["recommendationsData"]["properties"]["items"]
    assert items[0]["poster_url"] == apl._PLACEHOLDER_POSTER_URL


def test_build_recommendations_document_handles_empty_list():
    document, datasources = apl.build_recommendations_document([], "movie")
    assert document["type"] == "APL"
    assert datasources["recommendationsData"]["properties"]["items"] == []


def test_build_detail_document_includes_qr_code_when_trailer_present():
    recommendation = _sample_recommendation()
    document, datasources = apl.build_detail_document(recommendation)

    assert document["type"] == "APL"
    assert datasources["detailData"]["properties"]["title"] == "Fight Club"
    assert datasources["detailData"]["properties"]["qrCodeUrl"] != ""
    assert "qrserver.com" in datasources["detailData"]["properties"]["qrCodeUrl"]


def test_build_detail_document_omits_qr_code_when_no_trailer():
    recommendation = _sample_recommendation(trailer_url="")
    _document, datasources = apl.build_detail_document(recommendation)

    assert datasources["detailData"]["properties"]["qrCodeUrl"] == ""


def test_build_detail_document_tv_show_label():
    recommendation = _sample_recommendation(media_type="tv", title="Breaking Bad")
    _document, datasources = apl.build_detail_document(recommendation)

    assert datasources["detailData"]["properties"]["mediaTypeLabel"] == "TV Show"


def test_build_detail_document_movie_label():
    recommendation = _sample_recommendation(media_type="movie")
    _document, datasources = apl.build_detail_document(recommendation)

    assert datasources["detailData"]["properties"]["mediaTypeLabel"] == "Movie"


def test_build_detail_document_handles_missing_rating():
    recommendation = _sample_recommendation(rating=None)
    _document, datasources = apl.build_detail_document(recommendation)

    assert datasources["detailData"]["properties"]["rating"] == 0.0


# --------------------------------------------------------------------------
# Visual-polish coverage: screen color/icon presence, and icon centering
# inside the circular frame.
# --------------------------------------------------------------------------


def _walk(node):
    """Yields every dict in a nested APL document/datasource structure,
    so tests can assert on a property regardless of how deeply nested the
    component that carries it happens to be."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _root_frame(document):
    return document["mainTemplate"]["items"][0]


def test_circular_icon_centers_its_glyph_both_ways():
    """
    The reported misalignment: `Frame` holds exactly one child and has NO
    alignItems/justifyContent of its own, so the old version's attempt to
    center via those properties was ignored, and its paddingBottom
    actively pushed the glyph upward. The fix is a Text sized to the
    Frame's exact dimensions with textAlign AND textAlignVertical center.
    """
    icon = apl._circular_icon("${icon}", "#123456", diameter=140)

    assert icon["type"] == "Frame"
    assert icon["width"] == icon["height"] == "140dp"
    assert icon["borderRadius"] == "70dp"
    # No padding on the Frame -- padding shrinks the child's box and
    # decenters the glyph.
    assert "paddingBottom" not in icon
    assert "paddingTop" not in icon

    text = icon["item"]
    assert text["type"] == "Text"
    assert text["width"] == "140dp"
    assert text["height"] == "140dp"
    assert text["textAlign"] == "center"
    assert text["textAlignVertical"] == "center"


def test_message_document_uses_gradient_background_and_centered_icon():
    document, datasources = apl.build_message_document("Weather", "It's 21 degrees.", icon="⛅", accent_color="#2C6E9E")

    root = _root_frame(document)
    assert root["type"] == "Frame"
    assert root["background"]["type"] == "linear"
    assert len(root["background"]["colorRange"]) >= 2

    # The circular icon badge is present and centers its glyph.
    icons = [n for n in _walk(document) if n.get("textAlignVertical") == "center" and n.get("type") == "Text"]
    assert icons, "expected a vertically centered Text (the icon glyph)"

    assert datasources["messageData"]["properties"]["icon"] == "⛅"
    assert datasources["messageData"]["properties"]["accentColor"] == "#2C6E9E"


def test_message_document_never_uses_inline_text_markup():
    """Inline APL text markup is deliberately avoided project-wide -- an
    unsupported tag renders as literal characters on the device rather
    than failing loudly, so colored text is expressed with separate
    components instead."""
    document, _datasources = apl.build_message_document("Title", "Body")

    for node in _walk(document):
        text = node.get("text")
        if isinstance(text, str):
            assert "<span" not in text


def test_recommendations_document_uses_gradient_and_scrolling_row():
    recommendations = [_sample_recommendation(title=f"Title {i}") for i in range(5)]
    document, datasources = apl.build_recommendations_document(recommendations, "movie")

    root = _root_frame(document)
    assert root["background"]["type"] == "linear"

    sequences = [n for n in _walk(document) if n.get("id") == "recommendationsGrid"]
    assert len(sequences) == 1
    # A horizontally scrolling Sequence, so 3 titles don't leave gaps and
    # 5 don't overflow off a smaller Echo Show.
    assert sequences[0]["scrollDirection"] == "horizontal"

    assert len(datasources["recommendationsData"]["properties"]["items"]) == 5


def test_recommendations_document_cards_are_tappable_with_index():
    document, _datasources = apl.build_recommendations_document([_sample_recommendation()], "movie")

    send_events = [
        n for n in _walk(document) if n.get("type") == "SendEvent" and n.get("arguments", [None])[0] == "viewRecommendation"
    ]
    assert send_events
    assert send_events[0]["arguments"] == ["viewRecommendation", "${index}"]


# --------------------------------------------------------------------------
# Detail screen: trailer + where to watch. Reported ask: "when I click any
# of them it should drill down into them and also provide a link to the
# trailer as well as where to watch them".
# --------------------------------------------------------------------------


def _providers(**overrides):
    base = {
        "region": "AU",
        "link": "https://www.themoviedb.org/movie/550-fight-club/watch?locale=AU",
        "stream": [{"name": "Binge", "logo_url": "https://image.tmdb.org/t/p/w92/binge.jpg"}],
        "rent": [{"name": "Apple TV", "logo_url": "https://image.tmdb.org/t/p/w92/apple.jpg"}],
        "buy": [{"name": "Amazon Video", "logo_url": "https://image.tmdb.org/t/p/w92/amazon.jpg"}],
    }
    base.update(overrides)
    return base


def test_detail_document_shows_streaming_providers():
    recommendation = _sample_recommendation(watch_providers=_providers())
    _document, datasources = apl.build_detail_document(recommendation)
    properties = datasources["detailData"]["properties"]

    assert properties["hasWatchProviders"] is True
    assert properties["watchLabel"] == "Streaming on"
    assert [p["name"] for p in properties["watchProviders"]] == ["Binge"]


def test_detail_document_falls_back_to_rent_then_buy():
    """Fallback order is by usefulness to the user -- a subscription they
    may already have beats a rental, which beats a purchase -- not by
    TMDB's field order."""
    _doc, rent_only = apl.build_detail_document(_sample_recommendation(watch_providers=_providers(stream=[])))
    assert rent_only["detailData"]["properties"]["watchLabel"] == "Rent from"

    _doc, buy_only = apl.build_detail_document(_sample_recommendation(watch_providers=_providers(stream=[], rent=[])))
    assert buy_only["detailData"]["properties"]["watchLabel"] == "Buy from"


def test_detail_document_handles_title_unavailable_in_region():
    recommendation = _sample_recommendation(watch_providers=_providers(stream=[], rent=[], buy=[]))
    document, datasources = apl.build_detail_document(recommendation)

    assert datasources["detailData"]["properties"]["hasWatchProviders"] is False
    assert datasources["detailData"]["properties"]["watchProviders"] == []
    # The document still carries the "not available" branch, gated by
    # APL's `when`, rather than rendering an empty provider row.
    texts = [n.get("text", "") for n in _walk(document) if isinstance(n.get("text"), str)]
    assert any("Not on streaming" in t for t in texts)


def test_detail_document_handles_missing_watch_providers_key():
    """A recommendation stored before watch providers existed (or one
    whose TMDB lookup failed) must not break the detail screen."""
    recommendation = _sample_recommendation()
    recommendation.pop("watch_providers", None)

    _document, datasources = apl.build_detail_document(recommendation)

    assert datasources["detailData"]["properties"]["hasWatchProviders"] is False


def test_detail_document_attributes_justwatch_when_providers_shown():
    """TMDB sources this data from JustWatch and its terms require
    attribution."""
    document, _datasources = apl.build_detail_document(_sample_recommendation(watch_providers=_providers()))

    texts = [n.get("text", "") for n in _walk(document) if isinstance(n.get("text"), str)]
    assert any("JustWatch" in t for t in texts)


def test_detail_document_flags_trailer_presence():
    _doc, with_trailer = apl.build_detail_document(_sample_recommendation())
    assert with_trailer["detailData"]["properties"]["hasTrailer"] is True

    _doc, without_trailer = apl.build_detail_document(_sample_recommendation(trailer_url=""))
    assert without_trailer["detailData"]["properties"]["hasTrailer"] is False


def test_detail_document_shows_release_year_only():
    _document, datasources = apl.build_detail_document(_sample_recommendation(release_date="1999-10-15"))
    assert datasources["detailData"]["properties"]["releaseDate"] == "1999"


def test_detail_document_has_back_and_feedback_touch_targets():
    document, _datasources = apl.build_detail_document(_sample_recommendation(watch_providers=_providers()))

    arguments = [n["arguments"] for n in _walk(document) if n.get("type") == "SendEvent"]
    assert ["backToRecommendations"] in arguments
    assert ["recordFeedback", "liked"] in arguments
    assert ["recordFeedback", "disliked"] in arguments


# --------------------------------------------------------------------------
# Clarification screen with tappable mood chips.
# --------------------------------------------------------------------------


def test_build_clarification_document_renders_question_and_default_options():
    question = "What are you in the mood for?"
    document, datasources = apl.build_clarification_document(question)

    properties = datasources["clarifyData"]["properties"]
    assert properties["question"] == question
    assert len(properties["options"]) == len(apl.CLARIFY_MOOD_OPTIONS)
    assert {"emoji", "label", "value"} == set(properties["options"][0].keys())

    send_events = [n for n in _walk(document) if n.get("type") == "SendEvent"]
    assert send_events[0]["arguments"] == ["chooseMood", "${data.value}"]


def test_build_clarification_document_accepts_custom_options():
    _document, datasources = apl.build_clarification_document(
        "Movie or TV?", options=[("🎬", "Movie", "a movie"), ("📺", "TV", "a TV show")]
    )

    assert [o["label"] for o in datasources["clarifyData"]["properties"]["options"]] == ["Movie", "TV"]


def test_build_clarification_document_media_type_movie_header():
    document, datasources = apl.build_clarification_document("What mood?", media_type="movie")

    # Check the header title in the document
    texts = [n.get("text", "") for n in _walk(document) if isinstance(n.get("text"), str)]
    assert any("🍿 What kind of movie?" in t for t in texts)


def test_build_clarification_document_media_type_tv_header():
    document, datasources = apl.build_clarification_document("What mood?", media_type="tv")

    # Check the header title in the document
    texts = [n.get("text", "") for n in _walk(document) if isinstance(n.get("text"), str)]
    assert any("📺 What kind of TV show?" in t for t in texts)


def test_build_clarification_document_media_type_default_header():
    document, datasources = apl.build_clarification_document("What mood?")

    # Check the default header title in the document
    texts = [n.get("text", "") for n in _walk(document) if isinstance(n.get("text"), str)]
    assert any("🍿 What are you in the mood for?" in t for t in texts)


def test_clarify_mood_options_all_carry_a_searchable_value():
    """"Surprise me" deliberately sends a real genre phrase rather than an
    empty value, so the agent can search immediately instead of asking a
    second question."""
    for emoji, label, value in apl.CLARIFY_MOOD_OPTIONS:
        assert emoji and label and value
        assert value.strip() != ""
