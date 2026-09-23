"""
APL (Alexa Presentation Language) document builders for this skill's
Echo Show screen experience -- solution-design.md Section 13.

FOUR DOCUMENTS:
- Recommendations grid (build_recommendations_document): a horizontally
  scrolling row of poster-art cards, each wrapped in a TouchWrapper that
  sends a UserEvent identifying which recommendation was tapped. This is
  the ONLY thing shown for a turn that produced recommendations -- the
  agent's own prose never appears on screen (see
  agents/boredom_buster_agent/agent.py's _build_spoken_summary for the
  other half of that fix, and this module's history note below).
- Detail screen (build_detail_document): poster, synopsis, rating, WHERE
  TO WATCH (streaming/rent/buy providers for the user's region), a QR
  code linking to the title's YouTube trailer, and like/dislike touch
  targets.
- Clarification screen (build_clarification_document): the question the
  agent asked, plus tappable answer chips, so the user can either speak
  or tap. Used when Boredom Buster asks about mood.
- Generic message screen (build_message_document): used by every other
  intent (AskBedrock, GetWeather, welcome, denials/errors) so the
  device's display actually reflects the current turn.

WHY QR CODES FOR TRAILERS: APL's Video component can't play YouTube
directly (verified against Amazon's own APL documentation), and
scraping/re-hosting YouTube's actual video stream would violate
YouTube's Terms of Service. A QR code the user scans with their phone is
the workaround -- solution-design.md Section 13.2.

VISUAL DESIGN, AND WHY IT LOOKS THE WAY IT DOES: an earlier version of
these documents was plain text on a flat background, which drew two
explicit complaints from real device testing -- "the screen graphics is
a bit bland" and "the icons are misaligned to the circles they are
inside." Both are addressed structurally rather than cosmetically:

- Every screen sits on a Frame with a linear `background` gradient
  (APL's Frame.background accepts a Color OR a Gradient -- confirmed
  against Amazon's APL Frame reference, which documents `background` as
  "Background fill that allows either a color or gradient"). Gradients
  cost nothing: no image assets, no network fetches, no hosting.
- The circular icon badge is a Frame whose single child is a Text sized
  to the Frame's exact dimensions with textAlign: center AND
  textAlignVertical: center. That combination is what actually centers a
  glyph inside the circle. The old version relied on the Frame's own
  alignItems/justifyContent -- properties Frame does not have (it "holds
  a single child" and exposes only border/background properties, per the
  same reference) -- plus a paddingBottom that pushed the glyph visibly
  off-center. Amazon's own Frame documentation demonstrates exactly this
  "Text item centered in the circle" pattern.

APL document version "2024.3" is used throughout, matching Amazon's own
current documentation examples (the RenderDocument example in
developer.amazon.com's apl-interface.html uses this exact version
string) -- not an arbitrary/invented value.
"""

import urllib.parse

APL_VERSION = "2024.3"
APL_TOKEN_RECOMMENDATIONS = "boredomBusterRecommendations"
APL_TOKEN_DETAIL = "boredomBusterDetail"
APL_TOKEN_CLARIFY = "boredomBusterClarify"
APL_TOKEN_MESSAGE = "jarvisAIMessage"

# Confirmed against goqr.me's own public API documentation
# (goqr.me/api/doc/create-qr-code/) -- no API key required, "data" is the
# URL-encoded content to encode, "size" is WIDTHxHEIGHT in pixels. Free,
# no account, no API key to store -- consistent with this project's
# standing "don't add anything that costs extra" constraint.
QR_CODE_BASE_URL = "https://api.qrserver.com/v1/create-qr-code/"
QR_CODE_SIZE = "300x300"

# A neutral placeholder so an Image component never points at an empty
# string (which some APL runtimes render as a broken-image glyph) when
# TMDB has no poster for a title. A generic placeholder image service --
# not any specific title's actual artwork.
_PLACEHOLDER_POSTER_URL = "https://placehold.co/260x390?text=No+Poster"

# Shared palette. Kept as named constants rather than inline hex strings
# so the four documents stay visually consistent with each other, and so
# a future re-theme is one edit per color instead of a search-and-replace
# across every layout.
COLOR_TEXT_PRIMARY = "#FFFFFF"
COLOR_TEXT_SECONDARY = "#B9C2CF"
COLOR_TEXT_MUTED = "#8892A0"
COLOR_CARD = "#1F2530"
COLOR_CARD_BORDER = "#333C4A"
COLOR_ACCENT_GOLD = "#F5C451"

# Background gradients, as APL Gradient objects (type/colorRange/
# inputRange/angle per APL's own data-types reference; angle 180 is
# top-to-bottom). Dark, low-brightness ends deliberately: an Echo Show
# often sits in a bedroom or kitchen, and a full-screen bright panel is
# unpleasant at night. Poster art and provider logos supply the color.
_GRADIENT_BOREDOM_BUSTER = {
    "type": "linear",
    "angle": 180,
    "colorRange": ["#241B3A", "#12161D"],
    "inputRange": [0, 1],
}
_GRADIENT_DETAIL = {
    "type": "linear",
    "angle": 135,
    "colorRange": ["#1B2440", "#101319"],
    "inputRange": [0, 1],
}


def supports_apl(event: dict) -> bool:
    """
    Checks whether the requesting device declares APL support, per
    Amazon's own guidance ("Always check supportedInterfaces before
    returning the Alexa.Presentation.APL directives"). Devices without a
    screen (or without APL support) never get an APL directive -- the
    skill still returns a normal spoken response either way, it just
    skips the visual layer.
    """
    interfaces = (
        event.get("context", {}).get("System", {}).get("device", {}).get("supportedInterfaces", {})
    )
    return "Alexa.Presentation.APL" in interfaces


def qr_code_url(target_url: str) -> str:
    """Builds a goqr.me QR code image URL encoding `target_url`. Returns
    "" if target_url is empty (e.g. no trailer found for a title) --
    callers skip rendering the QR code entirely in that case rather than
    generating a QR code that encodes nothing useful."""
    if not target_url:
        return ""
    encoded = urllib.parse.quote(target_url, safe="")
    return f"{QR_CODE_BASE_URL}?size={QR_CODE_SIZE}&data={encoded}"


def _gradient_screen(gradient: dict, items: list) -> dict:
    """
    Wraps `items` in a full-bleed Frame with a gradient background --
    the common root of every document in this module. Frame is used
    rather than Container because only Frame exposes the `background`
    property that accepts a Gradient (Container has backgroundColor
    only, which is a flat color).

    `backgroundColor` is ALSO set, to the gradient's darkest stop. This
    is the graceful-degradation path Amazon's own Frame documentation
    describes: a device running an APL runtime new enough to support
    `background` uses the gradient ("When you provide both background
    and backgroundColor, background is used"), and an older Echo Show
    that predates it falls back to the flat color instead of rendering
    no background at all.
    """
    return {
        "type": "Frame",
        "width": "100%",
        "height": "100%",
        "background": gradient,
        "backgroundColor": gradient["colorRange"][-1],
        "item": {
            "type": "Container",
            "width": "100%",
            "height": "100%",
            "items": items,
        },
    }


def _screen_header(title: str, subtitle: str = "") -> dict:
    """A consistent title/subtitle block across screens -- gives every
    document the same visual anchor instead of each one starting with a
    differently-sized line of text."""
    items = [
        {
            "type": "Text",
            "text": title,
            "fontSize": "30dp",
            "fontWeight": "700",
            "color": COLOR_TEXT_PRIMARY,
            "maxLines": 1,
        }
    ]
    if subtitle:
        items.append(
            {
                "type": "Text",
                "text": subtitle,
                "fontSize": "18dp",
                "color": COLOR_TEXT_MUTED,
                "maxLines": 1,
                "paddingTop": "2dp",
            }
        )
    return {
        "type": "Container",
        "width": "100%",
        "paddingLeft": "40dp",
        "paddingRight": "40dp",
        "paddingTop": "22dp",
        "paddingBottom": "10dp",
        "items": items,
    }


def _circular_icon(icon_binding: str, color_binding: str, diameter: int = 140) -> dict:
    """
    A glyph centered inside a filled circle.

    The centering is the whole point of this helper existing: `Frame`
    holds exactly one child and has no alignItems/justifyContent of its
    own, so the child must center itself. A Text sized to the Frame's
    exact width and height, with textAlign: center (horizontal) and
    textAlignVertical: center (vertical), does that.
    """
    size = f"{diameter}dp"
    return {
        "type": "Frame",
        "width": size,
        "height": size,
        "borderRadius": f"{diameter // 2}dp",
        "backgroundColor": color_binding,
        "item": {
            "type": "Text",
            "text": icon_binding,
            "width": size,
            "height": size,
            "fontSize": f"{int(diameter * 0.46)}dp",
            "textAlign": "center",
            "textAlignVertical": "center",
        },
    }


def build_recommendations_document(recommendations: list[dict], media_type: str = "", user_id: str = "", search_query: str = "") -> tuple[dict, dict]:
    """
    Builds the RenderDocument directive's `document` and `datasources`
    for the recommendations grid (solution-design.md Section 13.1).
    Returns (document, datasources) -- callers combine these with the
    fixed APL_TOKEN_RECOMMENDATIONS token when constructing the full
    directive (see utils/response.py's build_apl_directive_response).

    Args:
        recommendations: List of recommendation dictionaries
        media_type: Optional media type ('movie' or 'tv') to customize the header
        user_id: Optional user ID to check watchlist status
        search_query: Optional search query/category to show in the header

    Each card is a TouchWrapper wrapping poster art, the title, and a
    rating badge. Tapping a card sends a UserEvent with the
    recommendation's index -- handlers/handler.py reads this to look up
    the full recommendation (from the session-attribute-stored list) and
    render the detail screen for it, with no second agent call.

    A horizontal `Sequence` is used rather than a fixed Container row so
    3 recommendations don't leave awkward gaps and 5 don't overflow off
    the edge of a smaller Echo Show -- the row simply scrolls.

    RESPONSIVE DESIGN: Card width uses percentage-based sizing with
    minimum constraints so text never gets cut off on smaller screens.
    The title text uses `maxLines: 2` with `width: "100%"` to wrap
    properly instead of truncating.
    """
    # Check watchlist status for each recommendation if user_id provided
    watchlist_items = set()
    watchlist_dates = {}
    if user_id:
        try:
            from watchlist import watchlist as watchlist_client
            items = watchlist_client.get_watchlist(user_id, limit=200)
            for item in items:
                item_id = f"{item.get('mediaType', 'movie')}:{item.get('tmdbId', '')}"
                watchlist_items.add(item_id)
                watchlist_dates[item_id] = item.get('addedAt', 0)
        except Exception:
            pass  # Silently ignore watchlist check failures

    card = {
        "type": "TouchWrapper",
        "id": "recommendationCard${index}",
        "onPress": [{"type": "SendEvent", "arguments": ["viewRecommendation", "${index}"]}],
        "item": {
            "type": "Container",
            "width": "240dp",
            "minWidth": "200dp",
            "maxWidth": "100%",
            "paddingLeft": "10dp",
            "paddingRight": "10dp",
            "items": [
                {
                    "type": "Frame",
                    "width": "100%",
                    "maxWidth": "220dp",
                    "height": "330dp",
                    "borderRadius": "14dp",
                    "backgroundColor": COLOR_CARD,
                    "borderWidth": "2dp",
                    "borderColor": COLOR_CARD_BORDER,
                    "item": {
                        "type": "Container",
                        "width": "100%",
                        "height": "330dp",
                        "items": [
                            {
                                "type": "Image",
                                "source": "${data.poster_url}",
                                "width": "100%",
                                "height": "330dp",
                                "scale": "best-fill",
                                "borderRadius": "14dp",
                                # Darkens the bottom of the poster so the title
                                # underneath reads as part of the same card
                                # rather than floating text.
                                "overlayGradient": {
                                    "type": "linear",
                                    "angle": 0,
                                    "colorRange": ["#000000CC", "transparent"],
                                    "inputRange": [0, 0.45],
                                },
                            },
                            # Watchlist badge overlay - shows when item is in watchlist
                            {
                                "type": "Container",
                                "when": "${data.inWatchlist}",
                                "width": "100%",
                                "height": "100%",
                                "alignItems": "flex-end",
                                "paddingTop": "8dp",
                                "paddingRight": "8dp",
                                "items": [
                                    {
                                        "type": "Frame",
                                        "backgroundColor": "#1E6F5C",
                                        "borderRadius": "12dp",
                                        "paddingLeft": "8dp",
                                        "paddingRight": "8dp",
                                        "paddingTop": "4dp",
                                        "paddingBottom": "4dp",
                                        "item": {
                                            "type": "Text",
                                            "text": "✓ In Watchlist",
                                            "fontSize": "14dp",
                                            "fontWeight": "600",
                                            "color": COLOR_TEXT_PRIMARY,
                                            "textAlign": "center",
                                        },
                                    },
                                ],
                            },
                        ],
                    },
                },
                # YouTube trailer button on the card - opens in Silk browser
                {
                    "type": "TouchWrapper",
                    "id": "cardTrailerButton${index}",
                    "when": "${data.hasTrailer}",
                    "onPress": [
                        {"type": "OpenURL", "source": "${data.trailer_url}"}
                    ],
                    "item": {
                        "type": "Frame",
                        "width": "100%",
                        "borderRadius": "8dp",
                        "backgroundColor": "#FF0000",
                        "paddingTop": "8dp",
                        "paddingBottom": "8dp",
                        "item": {
                            "type": "Text",
                            "text": "▶  View Trailer",
                            "fontSize": "15dp",
                            "fontWeight": "700",
                            "color": "#FFFFFF",
                            "textAlign": "center",
                        },
                    },
                },
                {
                    "type": "Text",
                    "text": "${data.title}",
                    "textAlign": "center",
                    "maxLines": 2,
                    "fontSize": "19dp",
                    "fontWeight": "600",
                    "color": COLOR_TEXT_PRIMARY,
                    "paddingTop": "10dp",
                    "width": "100%",
                },
                # Watchlist date - shows when item was added to watchlist
                {
                    "type": "Text",
                    "when": "${data.inWatchlist && data.watchlistDate}",
                    "text": "Added ${data.watchlistDate}",
                    "fontSize": "13dp",
                    "color": "#1E6F5C",
                    "textAlign": "center",
                    "paddingTop": "2dp",
                },
                # Type and rating are separate Text components in a row,
                # rather than one string with inline markup, so the
                # rating can be gold while the type stays muted -- APL's
                # inline text markup is deliberately avoided throughout
                # this module, since an unsupported tag renders as
                # literal characters on the device rather than failing
                # loudly.
                {
                    "type": "Container",
                    "direction": "row",
                    "width": "100%",
                    "justifyContent": "center",
                    "paddingTop": "2dp",
                    "items": [
                        {
                            "type": "Text",
                            "text": "${data.media_type == 'tv' ? 'TV' : 'Movie'}",
                            "fontSize": "16dp",
                            "color": COLOR_TEXT_MUTED,
                        },
                        {
                            "type": "Text",
                            "text": "  ★ ${data.rating}",
                            "fontSize": "16dp",
                            "fontWeight": "600",
                            "color": COLOR_ACCENT_GOLD,
                        },
                    ],
                },
            ],
        },
    }

    # Determine header text based on media_type and search_query
    # Always show what category was selected
    if search_query:
        # Capitalize the search query for display
        category = search_query.title()
        if media_type == "movie":
            header_title = f"🍿 Movie Recommendations: {category}"
        elif media_type == "tv":
            header_title = f"📺 TV Show Recommendations: {category}"
        else:
            header_title = f"🍿 Recommendations: {category}"
    else:
        # No search query provided, just show media type
        if media_type == "movie":
            header_title = "🍿 Movie Recommendations"
        elif media_type == "tv":
            header_title = "📺 TV Show Recommendations"
        else:
            header_title = "🍿 Boredom Buster"

    header_subtitle = "Tap a title to see the trailer and where to watch"

    def _pill(component_id: str, label: str, arguments: list, background: str) -> dict:
        return {
            "type": "TouchWrapper",
            "id": component_id,
            "onPress": [{"type": "SendEvent", "arguments": arguments}],
            "item": {
                "type": "Frame",
                "borderRadius": "24dp",
                "backgroundColor": background,
                "item": {
                    "type": "Text",
                    "text": label,
                    "fontSize": "19dp",
                    "color": COLOR_TEXT_PRIMARY,
                    "textAlign": "center",
                    "textAlignVertical": "center",
                    "height": "48dp",
                    "paddingLeft": "24dp",
                    "paddingRight": "24dp",
                },
            },
        }

    footer = {
        "type": "Container",
        "direction": "row",
        "width": "100%",
        "justifyContent": "center",
        "paddingTop": "10dp",
        "paddingBottom": "18dp",
        "items": [
            _pill("mainMenuButton", "🏠  Main Menu", ["launchAction", "go_home"], "#5A5A5A"),
        ],
    }

    document = {
        "type": "APL",
        "version": APL_VERSION,
        "mainTemplate": {
            "parameters": ["payload"],
            "items": [
                _gradient_screen(
                    _GRADIENT_BOREDOM_BUSTER,
                    [
                        _screen_header(header_title, header_subtitle),
                        {
                            "type": "Sequence",
                            "id": "recommendationsGrid",
                            "scrollDirection": "horizontal",
                            "width": "100%",
                            "grow": 1,
                            "paddingLeft": "30dp",
                            "paddingRight": "30dp",
                            "data": "${payload.recommendationsData.properties.items}",
                            "item": card,
                        },
                        footer,
                    ],
                )
            ],
        },
    }

    # Helper to format timestamp
    from datetime import datetime
    def format_date(timestamp):
        if not timestamp:
            return ""
        try:
            dt = datetime.fromtimestamp(timestamp)
            return dt.strftime("%b %d, %Y")
        except Exception:
            return ""

    datasources = {
        "recommendationsData": {
            "type": "object",
            "objectId": "recommendations",
            "properties": {
                "items": [
                    {
                        "title": rec.get("title", ""),
                        "poster_url": rec.get("poster_url") or _PLACEHOLDER_POSTER_URL,
                        "media_type": rec.get("media_type", "movie"),
                        "rating": round(float(rec.get("rating") or 0), 1),
                        "hasTrailer": bool(rec.get("trailer_url")),
                        "trailer_url": rec.get("trailer_url", ""),
                        "inWatchlist": f"{rec.get('media_type', 'movie')}:{rec.get('id', '')}" in watchlist_items,
                        "watchlistDate": format_date(watchlist_dates.get(f"{rec.get('media_type', 'movie')}:{rec.get('id', '')}", 0)),
                    }
                    for rec in recommendations
                ]
            },
        }
    }

    return document, datasources


def _pick_watch_category(watch_providers: dict) -> tuple[str, list]:
    """
    Picks the single most useful "where to watch" category to show, and
    a label for it: subscription streaming if available, otherwise
    rental, otherwise purchase. Returns ("", []) when the title isn't
    available anywhere in the user's region.

    Only one category is shown rather than all three because the detail
    screen has room for one row of logos, and "included with a
    subscription you may already have" is strictly more useful to a user
    than "also available to buy" -- so the fallback order is by
    usefulness, not by TMDB's field order.
    """
    for key, label in (("stream", "Streaming on"), ("rent", "Rent from"), ("buy", "Buy from")):
        providers = watch_providers.get(key) or []
        if providers:
            return label, providers
    return "", []


def build_detail_document(recommendation: dict, in_watchlist: bool = False, watchlist_date: str = "") -> tuple[dict, dict]:
    """
    Builds the RenderDocument directive's `document` and `datasources`
    for a single recommendation's detail screen (solution-design.md
    Section 13.2). Three columns: poster, details (synopsis + where to
    watch), and the trailer QR code. Like/dislike touch targets sit in a
    footer, with a back button returning to the recommendations grid.

    WHERE TO WATCH is sourced from TMDB's watch-providers data, attached
    per-title by the agent (see agents/boredom_buster_agent/agent.py's
    _enrich_recommendations). TMDB gets that data from JustWatch and its
    terms require attributing them, so a "Streaming data by JustWatch"
    line renders whenever any provider is shown. When nothing is
    available in the user's region the section says so plainly rather
    than rendering an empty row -- an unavailable title is a normal
    outcome, not an error.

    Conditional sections (trailer QR vs. "no trailer", provider row vs.
    "not available") are driven by boolean flags in the datasource and
    APL's `when` property, rather than by building different Python dicts
    per case -- one document shape, so there's only one layout to reason
    about.

    Args:
        recommendation: The recommendation dict with title, media_type, etc.
        in_watchlist: Whether this item is already in the user's watchlist
        watchlist_date: Formatted date string when item was added to watchlist
    """
    trailer_url = recommendation.get("trailer_url", "")
    qr_url = qr_code_url(trailer_url)
    watch_providers = recommendation.get("watch_providers") or {}
    watch_label, providers = _pick_watch_category(watch_providers)

    # Determine watchlist button label and action based on watchlist status
    watchlist_button_label = "🗑️  Remove from Watchlist" if in_watchlist else "📋  Add to Watchlist"
    watchlist_button_action = ["removeFromWatchlist"] if in_watchlist else ["addToWatchlist"]
    watchlist_button_color = "#B94A48" if in_watchlist else "#2C5F8A"

    poster_column = {
        "type": "Container",
        "width": "210dp",
        "alignItems": "center",
        "items": [
            {
                "type": "Frame",
                "width": "200dp",
                "height": "300dp",
                "borderRadius": "14dp",
                "backgroundColor": COLOR_CARD,
                "borderWidth": "2dp",
                "borderColor": COLOR_CARD_BORDER,
                "item": {
                    "type": "Image",
                    "source": "${payload.detailData.properties.posterUrl}",
                    "width": "200dp",
                    "height": "300dp",
                    "scale": "best-fill",
                    "borderRadius": "14dp",
                },
            }
        ],
    }

    provider_logo = {
        "type": "TouchWrapper",
        "id": "providerLogo${index}",
        "onPress": [
            {"type": "OpenURL", "source": "${payload.detailData.properties.watchProvidersLink}"}
        ],
        "item": {
            "type": "Container",
            "width": "84dp",
            "alignItems": "center",
            "paddingRight": "10dp",
            "items": [
                {
                    "type": "Frame",
                    "width": "54dp",
                    "height": "54dp",
                    "borderRadius": "12dp",
                    "backgroundColor": "#FFFFFF",
                    "item": {
                        "type": "Image",
                        "source": "${data.logo_url}",
                        "width": "54dp",
                        "height": "54dp",
                        "scale": "best-fit",
                        "borderRadius": "12dp",
                    },
                },
                {
                    "type": "Text",
                    "text": "${data.name}",
                    "fontSize": "13dp",
                    "color": COLOR_TEXT_MUTED,
                    "textAlign": "center",
                    "maxLines": 2,
                    "paddingTop": "4dp",
                },
            ],
        },
    }

    details_column = {
        "type": "Container",
        "grow": 1,
        "paddingLeft": "26dp",
        "paddingRight": "20dp",
        "items": [
            {
                "type": "Text",
                "text": "${payload.detailData.properties.title}",
                "fontSize": "30dp",
                "fontWeight": "700",
                "color": COLOR_TEXT_PRIMARY,
                "maxLines": 2,
            },
            {
                "type": "Container",
                "direction": "row",
                "paddingTop": "4dp",
                "items": [
                    {
                        "type": "Text",
                        "text": "${payload.detailData.properties.mediaTypeLabel}  ·  ${payload.detailData.properties.releaseDate}",
                        "fontSize": "17dp",
                        "color": COLOR_TEXT_MUTED,
                    },
                    {
                        "type": "Text",
                        "text": "   ★ ${payload.detailData.properties.rating}",
                        "fontSize": "17dp",
                        "fontWeight": "600",
                        "color": COLOR_ACCENT_GOLD,
                    },
                ],
            },
            {
                "type": "Text",
                "text": "${payload.detailData.properties.overview}",
                "fontSize": "17dp",
                "color": COLOR_TEXT_SECONDARY,
                "maxLines": 6,
                "paddingTop": "12dp",
            },
            # --- Where to watch ---
            {
                "type": "Text",
                "when": "${payload.detailData.properties.hasWatchProviders}",
                "text": "${payload.detailData.properties.watchLabel}",
                "fontSize": "15dp",
                "fontWeight": "600",
                "color": COLOR_TEXT_MUTED,
                "paddingTop": "16dp",
                "paddingBottom": "6dp",
            },
            {
                "type": "Container",
                "when": "${payload.detailData.properties.hasWatchProviders}",
                "direction": "row",
                "data": "${payload.detailData.properties.watchProviders}",
                "item": provider_logo,
            },
            {
                "type": "Text",
                "when": "${payload.detailData.properties.hasWatchProviders}",
                "text": "Streaming data by JustWatch",
                "fontSize": "12dp",
                "color": COLOR_TEXT_MUTED,
                "paddingTop": "6dp",
            },
            {
                "type": "Text",
                "when": "${!payload.detailData.properties.hasWatchProviders}",
                "text": "Not on streaming in your region right now",
                "fontSize": "15dp",
                "color": COLOR_TEXT_MUTED,
                "paddingTop": "16dp",
            },
            # --- View Trailer button in details column ---
            {
                "type": "TouchWrapper",
                "when": "${payload.detailData.properties.hasTrailer}",
                "onPress": [
                    {"type": "OpenURL", "source": "${payload.detailData.properties.trailerUrl}"}
                ],
                "item": {
                    "type": "Frame",
                    "borderRadius": "12dp",
                    "backgroundColor": "#FF0000",
                    "width": "200dp",
                    "item": {
                        "type": "Text",
                        "text": "▶  View Trailer",
                        "fontSize": "18dp",
                        "fontWeight": "700",
                        "color": "#FFFFFF",
                        "textAlign": "center",
                        "textAlignVertical": "center",
                        "height": "48dp",
                        "paddingLeft": "20dp",
                        "paddingRight": "20dp",
                    },
                },
                "paddingTop": "16dp",
            },
            # --- Voice instruction for returning to recommendations ---
            {
                "type": "Text",
                "when": "${payload.detailData.properties.hasTrailer}",
                "text": "💡 Say \"Alexa, return\" to come back",
                "fontSize": "14dp",
                "color": COLOR_TEXT_MUTED,
                "textAlign": "left",
                "fontStyle": "italic",
                "paddingTop": "8dp",
            },
        ],
    }

    trailer_column = {
        "type": "Container",
        "width": "180dp",
        "alignItems": "center",
        "items": [
            {
                "type": "TouchWrapper",
                "id": "trailerQRButton",
                "when": "${payload.detailData.properties.hasTrailer}",
                "onPress": [
                    {"type": "SendEvent", "arguments": ["openTrailer", "${payload.detailData.properties.trailerUrl}"]}
                ],
                "item": {
                    "type": "Frame",
                    "width": "150dp",
                    "height": "150dp",
                    "borderRadius": "12dp",
                    "backgroundColor": "#FFFFFF",
                    "item": {
                        "type": "Image",
                        "source": "${payload.detailData.properties.qrCodeUrl}",
                        "width": "150dp",
                        "height": "150dp",
                        "scale": "best-fit",
                    },
                },
            },
            {
                "type": "Text",
                "when": "${payload.detailData.properties.hasTrailer}",
                "text": "▶ Tap QR code to open trailer",
                "fontSize": "15dp",
                "color": COLOR_TEXT_SECONDARY,
                "textAlign": "center",
                "paddingTop": "8dp",
            },
            # Direct YouTube link button - opens in Silk browser
            {
                "type": "TouchWrapper",
                "when": "${payload.detailData.properties.hasTrailer}",
                "onPress": [
                    {"type": "OpenURL", "source": "${payload.detailData.properties.trailerUrl}"}
                ],
                "item": {
                    "type": "Frame",
                    "borderRadius": "12dp",
                    "backgroundColor": "#FF0000",
                    "width": "150dp",
                    "item": {
                        "type": "Text",
                        "text": "▶  View Trailer",
                        "fontSize": "17dp",
                        "fontWeight": "700",
                        "color": "#FFFFFF",
                        "textAlign": "center",
                        "textAlignVertical": "center",
                        "height": "42dp",
                        "paddingLeft": "20dp",
                        "paddingRight": "20dp",
                    },
                },
            },
            {
                "type": "Text",
                "when": "${payload.detailData.properties.hasTrailer}",
                "text": "💡 Say \"Alexa, return\" to come back",
                "fontSize": "13dp",
                "color": COLOR_TEXT_MUTED,
                "textAlign": "center",
                "fontStyle": "italic",
                "paddingTop": "8dp",
            },
            {
                "type": "Text",
                "when": "${!payload.detailData.properties.hasTrailer}",
                "text": "No trailer available",
                "fontSize": "15dp",
                "color": COLOR_TEXT_MUTED,
                "textAlign": "center",
                "paddingTop": "60dp",
            },
        ],
    }

    def _pill(component_id: str, label: str, arguments: list, background: str) -> dict:
        return {
            "type": "TouchWrapper",
            "id": component_id,
            "onPress": [{"type": "SendEvent", "arguments": arguments}],
            "item": {
                "type": "Frame",
                "borderRadius": "24dp",
                "backgroundColor": background,
                "item": {
                    "type": "Text",
                    "text": label,
                    "fontSize": "19dp",
                    "color": COLOR_TEXT_PRIMARY,
                    "textAlign": "center",
                    "textAlignVertical": "center",
                    "height": "48dp",
                    "paddingLeft": "24dp",
                    "paddingRight": "24dp",
                },
            },
        }

    footer = {
        "type": "Container",
        "direction": "row",
        "width": "100%",
        "justifyContent": "center",
        "paddingTop": "10dp",
        "paddingBottom": "18dp",
        "items": [
            _pill("likeButton", "👍  Love it", ["recordFeedback", "liked"], "#1E6F5C"),
            {"type": "Container", "width": "18dp"},
            _pill("dislikeButton", "👎  Not for me", ["recordFeedback", "disliked"], "#7A3B3B"),
            {"type": "Container", "width": "18dp"},
            _pill("watchlistButton", watchlist_button_label, watchlist_button_action, watchlist_button_color),
            {"type": "Container", "width": "18dp"},
            _pill("mainMenuButton", "🏠  Main Menu", ["launchAction", "go_home"], "#5A5A5A"),
        ],
    }

    back_button = {
        "type": "TouchWrapper",
        "id": "backButton",
        "onPress": [{"type": "SendEvent", "arguments": ["backToRecommendations"]}],
        "item": {
            "type": "Text",
            "text": "‹  All picks",
            "fontSize": "18dp",
            "color": COLOR_TEXT_MUTED,
            "paddingLeft": "32dp",
            "paddingTop": "16dp",
            "paddingBottom": "6dp",
        },
    }

    document = {
        "type": "APL",
        "version": APL_VERSION,
        "mainTemplate": {
            "parameters": ["payload"],
            "items": [
                _gradient_screen(
                    _GRADIENT_DETAIL,
                    [
                        back_button,
                        {
                            "type": "Container",
                            "direction": "row",
                            "width": "100%",
                            "grow": 1,
                            "paddingLeft": "32dp",
                            "paddingRight": "32dp",
                            "items": [poster_column, details_column, trailer_column],
                        },
                        footer,
                    ],
                )
            ],
        },
    }

    datasources = {
        "detailData": {
            "type": "object",
            "objectId": "detail",
            "properties": {
                "title": recommendation.get("title", ""),
                "posterUrl": recommendation.get("poster_url") or _PLACEHOLDER_POSTER_URL,
                "overview": recommendation.get("overview", ""),
                "mediaTypeLabel": "TV Show" if recommendation.get("media_type") == "tv" else "Movie",
                "releaseDate": (recommendation.get("release_date") or "")[:4],
                "rating": round(float(recommendation.get("rating") or 0), 1),
                "qrCodeUrl": qr_url,
                "hasTrailer": bool(qr_url),
                "trailerUrl": trailer_url,
                "watchLabel": watch_label,
                "watchProviders": providers,
                "watchProvidersLink": watch_providers.get("link", ""),
                "hasWatchProviders": bool(providers),
                "inWatchlist": in_watchlist,
                "watchlistDate": watchlist_date,
            },
        }
    }

    return document, datasources


# Tappable answers offered alongside Boredom Buster's spoken clarifying
# question. Each entry is (emoji, label, the text sent to the agent as
# the answer). "Surprise me" deliberately sends a real genre phrase
# rather than an empty value, so the agent can search immediately
# instead of asking a second question. "Specific Search" triggers a
# second screen with popular franchise options and personalized suggestions.
CLARIFY_MOOD_OPTIONS = [
    ("😂", "Funny", "something funny"),
    ("😱", "Thrilling", "something thrilling"),
    ("🥰", "Feel good", "something feel good"),
    ("🎲", "Surprise me", "a popular crowd pleaser"),
    ("🔍", "Specific Search", "SHOW_KEYWORD_OPTIONS"),
]

# Popular franchise/keyword options shown on the specific search screen.
# Each entry is (emoji, label, search query text). Values are the plain
# franchise/proper-noun name, not padded with extra words like "superhero"
# or "dinosaur" -- these values are sent verbatim as the MoodOrGenre query
# (see handlers/handler.py's _handle_choose_mood), and
# tmdb_client.py's discover_movies_by_mood/discover_tv_shows_by_mood look
# them up against TMDB's /search/keyword endpoint (with_keywords) when no
# genre matches. TMDB's keyword names are themselves short proper nouns
# ("marvel cinematic universe", "star wars", "harry potter", etc.), so a
# clean value here matches its corresponding TMDB keyword far more
# reliably than a padded phrase would.
KEYWORD_SEARCH_OPTIONS = [
    ("🦸", "Marvel", "marvel"),
    ("🦇", "DC Comics", "dc comics"),
    ("⭐", "Star Wars", "star wars"),
    ("🏰", "Disney", "disney"),
    ("🧙", "Harry Potter", "harry potter"),
    ("🕷️", "Spider-Man", "spiderman"),
    ("🦖", "Jurassic Park", "jurassic park"),
    ("👽", "Sci-Fi Classics", "science fiction"),
]


def build_keyword_search_document(media_type: str = "", user_id: str = "") -> tuple[dict, dict]:
    """
    Builds a screen showing popular franchise/keyword options and personalized
    suggestions based on the user's watchlist and like/dislike history.

    Users can either tap one of the suggested options or speak their own
    specific keyword (like "spiderman movies" or "marvel tv shows").

    Args:
        media_type: Optional media type ('movie' or 'tv') to customize the header
        user_id: Optional user ID to generate personalized suggestions

    Returns:
        (document, datasources) tuple for APL RenderDocument directive
    """
    # Get personalized suggestions based on user's watchlist and preferences
    personalized_options = []
    if user_id:
        try:
            from watchlist import watchlist as watchlist_client
            # Get user's watchlist to extract common themes
            items = watchlist_client.get_watchlist(user_id, limit=50)

            # Extract keywords from watchlist titles to suggest related searches
            # This is a simple approach - could be enhanced with genre/keyword analysis
            keywords_found = set()
            for item in items:
                title = item.get('title', '').lower()
                # Check for popular franchises in watchlist
                if 'marvel' in title or 'avengers' in title or 'iron man' in title or 'spider' in title:
                    keywords_found.add('marvel')
                if 'batman' in title or 'superman' in title or 'justice league' in title:
                    keywords_found.add('dc')
                if 'star wars' in title or 'mandalorian' in title:
                    keywords_found.add('star wars')
                if 'harry potter' in title or 'fantastic beasts' in title:
                    keywords_found.add('harry potter')

            # Map found keywords to suggested searches. Values are the plain
            # franchise name (not "more marvel superhero") for the same reason
            # as KEYWORD_SEARCH_OPTIONS above -- these are sent verbatim as the
            # search query, and a clean name matches TMDB's keyword search far
            # more reliably than a padded phrase.
            keyword_map = {
                'marvel': ("🦸", "More Marvel", "marvel"),
                'dc': ("🦇", "More DC", "dc comics"),
                'star wars': ("⭐", "More Star Wars", "star wars"),
                'harry potter': ("🧙", "More Harry Potter", "harry potter"),
            }

            for keyword in keywords_found:
                if keyword in keyword_map:
                    personalized_options.append(keyword_map[keyword])
                    if len(personalized_options) >= 3:  # Limit to 3 personalized suggestions
                        break
        except Exception:
            pass  # Silently ignore errors getting personalized suggestions

    # Combine personalized options with popular franchise options
    # Remove duplicates by checking labels
    all_options = list(personalized_options)
    personalized_labels = {opt[1] for opt in personalized_options}

    for opt in KEYWORD_SEARCH_OPTIONS:
        if opt[1] not in personalized_labels:
            all_options.append(opt)

    # Limit to 8 total options to fit on screen
    all_options = all_options[:8]

    # Determine header based on media_type
    if media_type == "movie":
        header_title = "🔍 Search for specific movies"
        subtitle_text = "Pick a franchise or say your own search"
        example_text = "\"Spiderman movies\" • \"Marvel movies\" • \"80s action movies\""
    elif media_type == "tv":
        header_title = "🔍 Search for specific TV shows"
        subtitle_text = "Pick a franchise or say your own search"
        example_text = "\"Marvel TV shows\" • \"Star Wars shows\" • \"Crime dramas\""
    else:
        header_title = "🔍 Search for something specific"
        subtitle_text = "Pick one or say your own search"
        example_text = "\"Spiderman movies\" • \"Marvel TV shows\" • \"80s action\""

    chip = {
        "type": "TouchWrapper",
        "id": "keywordChip${index}",
        "onPress": [{"type": "SendEvent", "arguments": ["chooseMood", "${data.value}"]}],
        "item": {
            "type": "Container",
            "paddingLeft": "9dp",
            "paddingRight": "9dp",
            "alignItems": "center",
            "items": [
                {
                    "type": "Frame",
                    "width": "150dp",
                    "height": "150dp",
                    "borderRadius": "22dp",
                    "backgroundColor": COLOR_CARD,
                    "borderWidth": "2dp",
                    "borderColor": COLOR_CARD_BORDER,
                    "item": {
                        "type": "Container",
                        "width": "150dp",
                        "height": "150dp",
                        "justifyContent": "center",
                        "alignItems": "center",
                        "items": [
                            {
                                "type": "Text",
                                "text": "${data.emoji}",
                                "fontSize": "54dp",
                                "textAlign": "center",
                            },
                            {
                                "type": "Text",
                                "text": "${data.label}",
                                "fontSize": "16dp",
                                "fontWeight": "600",
                                "color": COLOR_TEXT_PRIMARY,
                                "textAlign": "center",
                                "paddingTop": "6dp",
                                "maxLines": 2,
                            },
                        ],
                    },
                }
            ],
        },
    }

    document = {
        "type": "APL",
        "version": APL_VERSION,
        "mainTemplate": {
            "parameters": ["payload"],
            "items": [
                _gradient_screen(
                    _GRADIENT_BOREDOM_BUSTER,
                    [
                        _screen_header(header_title, subtitle_text),
                        {
                            "type": "ScrollView",
                            "width": "100%",
                            "height": "100%",
                            "grow": 1,
                            "item": {
                                "type": "Container",
                                "width": "100%",
                                "paddingLeft": "40dp",
                                "paddingRight": "40dp",
                                "paddingTop": "20dp",
                                "paddingBottom": "20dp",
                                "items": [
                                    {
                                        "type": "Container",
                                        "direction": "row",
                                        "wrap": "wrap",
                                        "justifyContent": "center",
                                        "data": "${payload.keywordData.properties.options}",
                                        "item": chip,
                                    },
                                    {
                                        "type": "Text",
                                        "text": "💡 Tap one above, or say something like:",
                                        "fontSize": "18dp",
                                        "fontWeight": "600",
                                        "color": COLOR_TEXT_SECONDARY,
                                        "textAlign": "center",
                                        "paddingTop": "30dp",
                                        "paddingBottom": "12dp",
                                    },
                                    {
                                        "type": "Text",
                                        "text": "${payload.keywordData.properties.exampleText}",
                                        "fontSize": "16dp",
                                        "color": COLOR_TEXT_MUTED,
                                        "textAlign": "center",
                                        "paddingBottom": "20dp",
                                    },
                                ],
                            },
                        },
                        {
                            "type": "Container",
                            "direction": "row",
                            "width": "100%",
                            "justifyContent": "center",
                            "paddingTop": "10dp",
                            "paddingBottom": "18dp",
                            "items": [
                                _pill("backButtonKeyword", "‹  Back", ["backToMoodSelection"], "#5A5A5A"),
                                {"type": "Container", "width": "18dp"},
                                _pill("mainMenuButtonKeyword", "🏠  Main Menu", ["launchAction", "go_home"], "#5A5A5A"),
                            ],
                        },
                    ],
                )
            ],
        },
    }

    datasources = {
        "keywordData": {
            "type": "object",
            "objectId": "keyword",
            "properties": {
                "options": [
                    {"emoji": emoji, "label": label, "value": value} for emoji, label, value in all_options
                ],
                "exampleText": example_text,
            },
        }
    }

    return document, datasources


def build_clarification_document(question: str, options: list = None, media_type: str = "") -> tuple[dict, dict]:
    """
    Builds a screen for a turn where Boredom Buster asked a clarifying
    question: the question itself, plus a row of tappable answer chips.

    WHY TAPPABLE: slot elicitation (utils/response.py's
    build_elicit_slot_response) covers the voice path, but a short reply
    like "movie" is easy for Alexa's NLU to miss. These chips add a path
    that cannot fail at all, since a tap sends an exact, pre-agreed value
    rather than something Alexa's NLU has to recognize. Tapping sends a
    UserEvent ["chooseMood", "<answer text>"], handled by
    handlers/handler.py exactly as if the user had spoken it.

    Args:
        question: The clarifying question text
        options: List of (emoji, label, value) tuples for mood chips
        media_type: Optional media type ('movie' or 'tv') to customize the header
    """
    if options is None:
        options = CLARIFY_MOOD_OPTIONS

    # Determine header based on media_type
    if media_type == "movie":
        header_title = "🍿 What kind of movie?"
    elif media_type == "tv":
        header_title = "📺 What kind of TV show?"
    else:
        header_title = "🍿 What are you in the mood for?"

    chip = {
        "type": "TouchWrapper",
        "id": "moodChip${index}",
        "onPress": [{"type": "SendEvent", "arguments": ["chooseMood", "${data.value}"]}],
        "item": {
            "type": "Container",
            "paddingLeft": "9dp",
            "paddingRight": "9dp",
            "alignItems": "center",
            "items": [
                {
                    "type": "Frame",
                    "width": "150dp",
                    "height": "150dp",
                    "borderRadius": "22dp",
                    "backgroundColor": COLOR_CARD,
                    "borderWidth": "2dp",
                    "borderColor": COLOR_CARD_BORDER,
                    "item": {
                        "type": "Container",
                        "width": "150dp",
                        "height": "150dp",
                        "justifyContent": "center",
                        "alignItems": "center",
                        "items": [
                            {
                                "type": "Text",
                                "text": "${data.emoji}",
                                "fontSize": "54dp",
                                "textAlign": "center",
                            },
                            {
                                "type": "Text",
                                "text": "${data.label}",
                                "fontSize": "18dp",
                                "fontWeight": "600",
                                "color": COLOR_TEXT_PRIMARY,
                                "textAlign": "center",
                                "paddingTop": "6dp",
                            },
                        ],
                    },
                }
            ],
        },
    }

    document = {
        "type": "APL",
        "version": APL_VERSION,
        "mainTemplate": {
            "parameters": ["payload"],
            "items": [
                _gradient_screen(
                    _GRADIENT_BOREDOM_BUSTER,
                    [
                        {
                            "type": "Container",
                            "width": "100%",
                            "grow": 1,
                            "justifyContent": "center",
                            "alignItems": "center",
                            "paddingLeft": "40dp",
                            "paddingRight": "40dp",
                            "items": [
                                {
                                    "type": "Text",
                                    "text": header_title,
                                    "fontSize": "30dp",
                                    "fontWeight": "700",
                                    "color": COLOR_TEXT_PRIMARY,
                                    "textAlign": "center",
                                    "maxLines": 1,
                                    "paddingBottom": "8dp",
                                },
                                {
                                    "type": "Text",
                                    "text": "${payload.clarifyData.properties.question}",
                                    "fontSize": "24dp",
                                    "fontWeight": "400",
                                    "color": COLOR_TEXT_SECONDARY,
                                    "textAlign": "center",
                                    "maxLines": 3,
                                    "paddingBottom": "22dp",
                                },
                                {
                                    "type": "Container",
                                    "direction": "row",
                                    "justifyContent": "center",
                                    "data": "${payload.clarifyData.properties.options}",
                                    "item": chip,
                                },
                                {
                                    "type": "Text",
                                    "text": "Tap one, or just tell me",
                                    "fontSize": "16dp",
                                    "color": COLOR_TEXT_MUTED,
                                    "textAlign": "center",
                                    "paddingTop": "18dp",
                                },
                                {
                                    "type": "TouchWrapper",
                                    "id": "backButtonClarify",
                                    "onPress": [{"type": "SendEvent", "arguments": ["launchAction", "go_home"]}],
                                    "item": {
                                        "type": "Text",
                                        "text": "‹  Back",
                                        "fontSize": "18dp",
                                        "color": COLOR_TEXT_MUTED,
                                        "paddingLeft": "32dp",
                                        "paddingTop": "16dp",
                                        "paddingBottom": "6dp",
                                    },
                                },
                                {
                                    "type": "TouchWrapper",
                                    "id": "mainMenuButtonClarify",
                                    "onPress": [{"type": "SendEvent", "arguments": ["launchAction", "go_home"]}],
                                    "item": {
                                        "type": "Frame",
                                        "borderRadius": "24dp",
                                        "backgroundColor": "#5A5A5A",
                                        "item": {
                                            "type": "Text",
                                            "text": "🏠  Main Menu",
                                            "fontSize": "19dp",
                                            "color": COLOR_TEXT_PRIMARY,
                                            "textAlign": "center",
                                            "textAlignVertical": "center",
                                            "height": "48dp",
                                            "paddingLeft": "24dp",
                                            "paddingRight": "24dp",
                                        },
                                    },
                                },
                            ],
                        }
                    ],
                )
            ],
        },
    }

    datasources = {
        "clarifyData": {
            "type": "object",
            "objectId": "clarify",
            "properties": {
                "question": question,
                "options": [
                    {"emoji": emoji, "label": label, "value": value} for emoji, label, value in options
                ],
            },
        }
    }

    return document, datasources


# Emoji glyph + accent color per category, used by build_message_document
# to give the generic message screen a visual identity instead of plain
# text -- see that function's docstring for why this was added (a real
# user complaint: "the screen looks very bland with just text").
# Deliberately emoji + solid color, not an external image URL -- APL's
# Text component renders emoji natively with zero network dependency, so
# there's nothing to break/timeout/404 the way a hotlinked icon image
# could, and nothing to host or pay for.
ICON_WEATHER = ("⛅", "#2C6E9E")
ICON_MOVIE = ("🍿", "#7B3FA0")
ICON_CHAT = ("💬", "#1C8C7C")
ICON_ALERT = ("🚫", "#B94A48")
ICON_DEFAULT = ("🤖", "#2C6E9E")

_GRADIENT_MESSAGE = {
    "type": "linear",
    "angle": 160,
    "colorRange": ["#232A36", "#12151B"],
    "inputRange": [0, 1],
}


def build_message_document(title: str, body: str, icon: str = "🤖", accent_color: str = "#2C6E9E") -> tuple[dict, dict]:
    """
    Builds the generic "message screen" -- an emoji icon centered in a
    colored circle, a title, and a body of text, on a gradient
    background. Used by every intent EXCEPT Boredom Buster's
    recommendations/detail/clarification screens (which render their own
    dedicated documents) so that on an APL-capable device the screen
    actually reflects the current turn's response instead of silently
    keeping whatever was rendered last.

    A Simple Card (Alexa's other card option) only renders in the Alexa
    app, never on the Echo Show's physical screen -- an APL
    RenderDocument directive is the only mechanism that updates the
    device display. This generic screen ensures every intent's response
    actually shows on screen. `icon`/`accent_color` (chosen per-intent by
    handlers/handler.py's _pick_icon_and_accent) give each response type
    a distinct visual identity without needing a bespoke layout per
    intent the way Boredom Buster's own screens have.
    """
    document = {
        "type": "APL",
        "version": APL_VERSION,
        "mainTemplate": {
            "parameters": ["payload"],
            "items": [
                _gradient_screen(
                    _GRADIENT_MESSAGE,
                    [
                        {
                            "type": "Container",
                            "width": "100%",
                            "grow": 1,
                            "justifyContent": "center",
                            "alignItems": "center",
                            "paddingLeft": "56dp",
                            "paddingRight": "56dp",
                            "items": [
                                _circular_icon(
                                    "${payload.messageData.properties.icon}",
                                    "${payload.messageData.properties.accentColor}",
                                ),
                                {
                                    "type": "Text",
                                    "text": "${payload.messageData.properties.title}",
                                    "textAlign": "center",
                                    "fontSize": "28dp",
                                    "fontWeight": "700",
                                    "color": COLOR_TEXT_PRIMARY,
                                    "paddingTop": "22dp",
                                    "paddingBottom": "10dp",
                                    "maxLines": 1,
                                },
                                {
                                    "type": "Text",
                                    "text": "${payload.messageData.properties.body}",
                                    "textAlign": "center",
                                    "fontSize": "22dp",
                                    "color": COLOR_TEXT_SECONDARY,
                                    "maxLines": 6,
                                },
                                {
                                    "type": "TouchWrapper",
                                    "id": "backButtonMessage",
                                    "onPress": [{"type": "SendEvent", "arguments": ["launchAction", "go_home"]}],
                                    "item": {
                                        "type": "Text",
                                        "text": "‹  Back",
                                        "fontSize": "18dp",
                                        "color": COLOR_TEXT_MUTED,
                                        "paddingLeft": "32dp",
                                        "paddingTop": "16dp",
                                        "paddingBottom": "6dp",
                                    },
                                },
                                {
                                    "type": "TouchWrapper",
                                    "id": "mainMenuButtonMessage",
                                    "onPress": [{"type": "SendEvent", "arguments": ["launchAction", "go_home"]}],
                                    "item": {
                                        "type": "Frame",
                                        "borderRadius": "24dp",
                                        "backgroundColor": "#5A5A5A",
                                        "item": {
                                            "type": "Text",
                                            "text": "🏠  Main Menu",
                                            "fontSize": "19dp",
                                            "color": COLOR_TEXT_PRIMARY,
                                            "textAlign": "center",
                                            "textAlignVertical": "center",
                                            "height": "48dp",
                                            "paddingLeft": "24dp",
                                            "paddingRight": "24dp",
                                        },
                                    },
                                },
                            ],
                        }
                    ],
                )
            ],
        },
    }

    datasources = {
        "messageData": {
            "type": "object",
            "objectId": "message",
            "properties": {
                "title": title,
                "body": body,
                "icon": icon,
                "accentColor": accent_color,
            },
        }
    }

    return document, datasources


# -------------------------------------------------------------------------
# Launch screen with capability cards -- lets users tap to start a task
# instead of listening to a long voice prompt listing what they can do.
# -------------------------------------------------------------------------


def _make_launch_card(icon: str, label: str, value: str, accent_color: str) -> dict:
    """Creates a tappable card for the launch screen's capability grid."""
    return {
        "type": "TouchWrapper",
        "id": f"launchCard{label.replace(' ', '')}",
        "onPress": [{"type": "SendEvent", "arguments": ["launchAction", value]}],
        "item": {
            "type": "Container",
            "width": "260dp",
            "paddingLeft": "8dp",
            "paddingRight": "8dp",
            "alignItems": "center",
            "items": [
                {
                    "type": "Frame",
                    "width": "180dp",
                    "height": "180dp",
                    "borderRadius": "24dp",
                    "backgroundColor": COLOR_CARD,
                    "borderWidth": "2dp",
                    "borderColor": COLOR_CARD_BORDER,
                    "item": {
                        "type": "Container",
                        "width": "180dp",
                        "height": "180dp",
                        "justifyContent": "center",
                        "alignItems": "center",
                        "items": [
                            {
                                "type": "Text",
                                "text": icon,
                                "fontSize": "60dp",
                                "textAlign": "center",
                            },
                            {
                                "type": "Text",
                                "text": label,
                                "fontSize": "18dp",
                                "fontWeight": "600",
                                "color": COLOR_TEXT_PRIMARY,
                                "textAlign": "center",
                                "maxLines": 2,
                                "paddingTop": "10dp",
                                "width": "160dp",
                            },
                        ],
                    },
                },
            ],
        },
    }


def _pill(component_id: str, label: str, arguments: list, background: str) -> dict:
    """Create a pill-shaped button for APL documents."""
    return {
        "type": "TouchWrapper",
        "id": component_id,
        "onPress": [{"type": "SendEvent", "arguments": arguments}],
        "item": {
            "type": "Frame",
            "borderRadius": "24dp",
            "backgroundColor": background,
            "item": {
                "type": "Text",
                "text": label,
                "fontSize": "19dp",
                "color": COLOR_TEXT_PRIMARY,
                "textAlign": "center",
                "textAlignVertical": "center",
                "height": "48dp",
                "paddingLeft": "24dp",
                "paddingRight": "24dp",
            },
        },
    }


def build_launch_document(capabilities: list[dict]) -> tuple[dict, dict]:
    """
    Builds the launch screen with tappable capability cards in a single horizontal row.
    Cards scroll horizontally if there are too many to fit on screen.

    `capabilities` is a list of dicts with keys:
      - icon: emoji glyph for the card
      - label: short label shown on the card
      - value: string sent to the skill when tapped (via UserEvent)
      - accent_color: border/accent color for visual variety

    Returns (document, datasources) for an APL RenderDocument directive.
    """
    card = {
        "type": "TouchWrapper",
        "id": "launchCard${index}",
        "onPress": [{"type": "SendEvent", "arguments": ["launchAction", "${data.value}"]}],
        "item": {
            "type": "Container",
            "width": "100%",
            "paddingLeft": "8dp",
            "paddingRight": "8dp",
            "alignItems": "center",
            "items": [
                {
                    "type": "Frame",
                    "width": "180dp",
                    "height": "180dp",
                    "borderRadius": "24dp",
                    "backgroundColor": COLOR_CARD,
                    "borderWidth": "2dp",
                    "borderColor": "${data.accentColor}",
                    "item": {
                        "type": "Container",
                        "width": "180dp",
                        "height": "180dp",
                        "justifyContent": "center",
                        "alignItems": "center",
                        "items": [
                            {
                                "type": "Text",
                                "text": "${data.icon}",
                                "fontSize": "60dp",
                                "textAlign": "center",
                            },
                            {
                                "type": "Text",
                                "text": "${data.label}",
                                "fontSize": "18dp",
                                "fontWeight": "600",
                                "color": COLOR_TEXT_PRIMARY,
                                "textAlign": "center",
                                "maxLines": 2,
                                "paddingTop": "10dp",
                                "width": "160dp",
                            },
                        ],
                    },
                },
            ],
        },
    }

    footer = {
        "type": "Container",
        "direction": "row",
        "width": "100%",
        "justifyContent": "center",
        "paddingTop": "10dp",
        "paddingBottom": "18dp",
        "items": [
            _pill("mainMenuButtonLaunch", "🏠  Main Menu", ["launchAction", "go_home"], "#5A5A5A"),
        ],
    }

    document = {
        "type": "APL",
        "version": APL_VERSION,
        "mainTemplate": {
            "parameters": ["payload"],
            "items": [
                _gradient_screen(
                    _GRADIENT_BOREDOM_BUSTER,
                    [
                        _screen_header("🤖 Jarvis AI", "Tap a card or just ask me anything"),
                        {
                            "type": "Sequence",
                            "id": "launchCapabilitiesGrid",
                            "scrollDirection": "horizontal",
                            "width": "100%",
                            "grow": 1,
                            "paddingLeft": "12dp",
                            "paddingRight": "12dp",
                            "data": "${payload.launchData.properties.capabilities}",
                            "item": card,
                        },
                        {
                            "type": "Text",
                            "text": "Or say: \"Smart Assistant\" / \"Check the weather\" / \"Recommend a movie\" / \"Recommend a TV show\"",
                            "fontSize": "14dp",
                            "color": COLOR_TEXT_MUTED,
                            "textAlign": "center",
                            "paddingTop": "8dp",
                            "paddingBottom": "8dp",
                            "paddingLeft": "40dp",
                            "paddingRight": "40dp",
                        },
                        footer,
                    ],
                )
            ],
        },
    }

    datasources = {
        "launchData": {
            "type": "object",
            "objectId": "launch",
            "properties": {
                "capabilities": capabilities,
            },
        }
    }

    return document, datasources


# Smart Assistant token
APL_TOKEN_SMART_ASSISTANT = "smartAssistant"

_GRADIENT_SMART_ASSISTANT = {
    "type": "linear",
    "angle": 165,
    "colorRange": ["#1A3A52", "#12161D"],
    "inputRange": [0, 1],
}


def build_smart_assistant_document(answer_text: str, follow_ups: list[dict] = None, links: list[dict] = None) -> tuple[dict, dict]:
    """
    Builds a rich visual response for Smart Assistant with the answer displayed
    in a formatted card and optional follow-up suggestions as tappable chips.

    Args:
        answer_text: The answer text from the assistant
        follow_ups: Optional list of follow-up suggestions, each with:
                   {"label": "Short label", "value": "Full question text"}
        links: Optional list of extracted links from the response, each with:
               {"text": "Link text", "url": "https://..."}
               These are rendered as tappable cards that open in the browser

    Returns:
        (document, datasources) tuple for APL RenderDocument directive
    """
    if follow_ups is None:
        follow_ups = []
    if links is None:
        links = []

    # Format the answer text with basic structure
    # Split into paragraphs if there are line breaks
    paragraphs = [p.strip() for p in answer_text.split('\n') if p.strip()]

    # Build answer content items
    answer_items = []
    for para in paragraphs:
        answer_items.append({
            "type": "Text",
            "text": para,
            "fontSize": "18dp",
            "color": COLOR_TEXT_SECONDARY,
            "lineHeight": "1.5",
            "paddingBottom": "12dp",
        })

    # Answer card
    answer_card = {
        "type": "Container",
        "width": "100%",
        "paddingLeft": "32dp",
        "paddingRight": "32dp",
        "paddingTop": "12dp",
        "items": [
            {
                "type": "Frame",
                "backgroundColor": COLOR_CARD,
                "borderRadius": "16dp",
                "borderWidth": "1dp",
                "borderColor": COLOR_CARD_BORDER,
                "maxHeight": "320dp",
                "item": {
                    "type": "ScrollView",
                    "width": "100%",
                    "height": "100%",
                    "item": {
                        "type": "Container",
                        "padding": "20dp",
                        "items": answer_items,
                    },
                },
            },
        ],
    }

    # Follow-up chip component
    followup_chip = {
        "type": "TouchWrapper",
        "id": "followupChip${index}",
        "onPress": [{"type": "SendEvent", "arguments": ["askFollowup", "${data.value}"]}],
        "item": {
            "type": "Frame",
            "borderRadius": "20dp",
            "backgroundColor": "#1C8C7C",
            "borderWidth": "2dp",
            "borderColor": "#2CA893",
            "item": {
                "type": "Text",
                "text": "${data.label}",
                "fontSize": "16dp",
                "fontWeight": "600",
                "color": COLOR_TEXT_PRIMARY,
                "textAlign": "center",
                "paddingLeft": "16dp",
                "paddingRight": "16dp",
                "paddingTop": "10dp",
                "paddingBottom": "10dp",
            },
        },
        "paddingRight": "12dp",
        "paddingBottom": "12dp",
    }

    # Build main content
    main_content_items = [
        _screen_header("💬 Smart Assistant", "Ask me anything"),
        answer_card,
    ]

    # Add link cards section if there are extracted links
    if links:
        # Link card component - tappable cards that open URLs in browser
        link_card = {
            "type": "TouchWrapper",
            "id": "linkCard${index}",
            "onPress": [{"type": "OpenURL", "source": "${data.url}"}],
            "item": {
                "type": "Frame",
                "borderRadius": "12dp",
                "backgroundColor": "#1E3A5F",
                "borderWidth": "2dp",
                "borderColor": "#2C5F8A",
                "item": {
                    "type": "Container",
                    "direction": "row",
                    "alignItems": "center",
                    "paddingLeft": "16dp",
                    "paddingRight": "16dp",
                    "paddingTop": "12dp",
                    "paddingBottom": "12dp",
                    "items": [
                        {
                            "type": "Text",
                            "text": "🔗",
                            "fontSize": "20dp",
                            "paddingRight": "12dp",
                        },
                        {
                            "type": "Text",
                            "text": "${data.text}",
                            "fontSize": "16dp",
                            "fontWeight": "500",
                            "color": COLOR_TEXT_PRIMARY,
                            "maxLines": 2,
                            "grow": 1,
                        },
                        {
                            "type": "Text",
                            "text": "→",
                            "fontSize": "20dp",
                            "color": COLOR_TEXT_MUTED,
                            "paddingLeft": "8dp",
                        },
                    ],
                },
            },
            "paddingBottom": "12dp",
        }

        main_content_items.append({
            "type": "Container",
            "width": "100%",
            "paddingLeft": "32dp",
            "paddingRight": "32dp",
            "paddingTop": "16dp",
            "paddingBottom": "8dp",
            "items": [
                {
                    "type": "Text",
                    "text": "📚 Sources & References",
                    "fontSize": "20dp",
                    "fontWeight": "600",
                    "color": COLOR_TEXT_PRIMARY,
                    "paddingBottom": "12dp",
                },
                {
                    "type": "Container",
                    "width": "100%",
                    "data": "${payload.smartAssistantData.properties.links}",
                    "item": link_card,
                },
            ],
        })

    # Add follow-up section if there are follow-ups
    if follow_ups:
        main_content_items.append({
            "type": "Container",
            "width": "100%",
            "paddingLeft": "32dp",
            "paddingRight": "32dp",
            "paddingTop": "16dp",
            "paddingBottom": "8dp",
            "items": [
                {
                    "type": "Text",
                    "text": "Would you like to know more?",
                    "fontSize": "20dp",
                    "fontWeight": "600",
                    "color": COLOR_TEXT_PRIMARY,
                    "paddingBottom": "12dp",
                },
                {
                    "type": "Container",
                    "direction": "row",
                    "wrap": "wrap",
                    "data": "${payload.smartAssistantData.properties.followups}",
                    "item": followup_chip,
                },
                {
                    "type": "Text",
                    "text": "Or ask me anything else",
                    "fontSize": "15dp",
                    "color": COLOR_TEXT_MUTED,
                    "paddingTop": "8dp",
                },
            ],
        })

    # Footer with navigation buttons
    footer = {
        "type": "Container",
        "direction": "row",
        "width": "100%",
        "justifyContent": "center",
        "paddingTop": "16dp",
        "paddingBottom": "18dp",
        "items": [
            {
                "type": "TouchWrapper",
                "id": "newQuestionButton",
                "onPress": [{"type": "SendEvent", "arguments": ["launchAction", "smart_assistant"]}],
                "item": {
                    "type": "Frame",
                    "borderRadius": "24dp",
                    "backgroundColor": "#2C6E9E",
                    "item": {
                        "type": "Text",
                        "text": "🆕  New Question",
                        "fontSize": "18dp",
                        "fontWeight": "600",
                        "color": COLOR_TEXT_PRIMARY,
                        "textAlign": "center",
                        "textAlignVertical": "center",
                        "height": "48dp",
                        "paddingLeft": "24dp",
                        "paddingRight": "24dp",
                    },
                },
                "paddingRight": "12dp",
            },
            {
                "type": "TouchWrapper",
                "id": "mainMenuButton",
                "onPress": [{"type": "SendEvent", "arguments": ["launchAction", "go_home"]}],
                "item": {
                    "type": "Frame",
                    "borderRadius": "24dp",
                    "backgroundColor": "#5A5A5A",
                    "item": {
                        "type": "Text",
                        "text": "🏠  Main Menu",
                        "fontSize": "18dp",
                        "fontWeight": "600",
                        "color": COLOR_TEXT_PRIMARY,
                        "textAlign": "center",
                        "textAlignVertical": "center",
                        "height": "48dp",
                        "paddingLeft": "24dp",
                        "paddingRight": "24dp",
                    },
                },
            },
        ],
    }

    main_content_items.append(footer)

    document = {
        "type": "APL",
        "version": APL_VERSION,
        "mainTemplate": {
            "parameters": ["payload"],
            "items": [
                _gradient_screen(
                    _GRADIENT_SMART_ASSISTANT,
                    main_content_items,
                )
            ],
        },
    }

    datasources = {
        "smartAssistantData": {
            "type": "object",
            "objectId": "smartAssistant",
            "properties": {
                "answer": answer_text,
                "followups": follow_ups,
                "links": links,
            },
        }
    }

    return document, datasources
