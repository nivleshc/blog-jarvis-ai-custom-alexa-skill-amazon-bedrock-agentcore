"""
Watchlist screen and its UserEvent handlers -- viewing the watchlist
grid, viewing/removing an individual watchlist item, and add/remove
actions triggered from a Boredom Buster recommendation's detail screen.

All of these are lightweight local DynamoDB reads/writes (via
watchlist.watchlist), not Bedrock/AgentCore calls, except where noted --
see each handler's own docstring for its exact allowlist/rate-limit
requirements.
"""

import logging

from allowlist import check_allowlist
from utils import apl
from utils.apl import (
    APL_VERSION,
    COLOR_CARD,
    COLOR_CARD_BORDER,
    COLOR_TEXT_PRIMARY,
    COLOR_TEXT_SECONDARY,
    COLOR_TEXT_MUTED,
    COLOR_ACCENT_GOLD,
    _PLACEHOLDER_POSTER_URL,
    _GRADIENT_BOREDOM_BUSTER,
    _gradient_screen,
    _screen_header,
    _pill,
)
from utils.logging_config import emit_denial_metric
from utils.response import (
    build_apl_directive_response,
    build_error_response,
    build_not_authorized_response,
    build_speech_response,
)

from .common import (
    SESSION_ATTR_CURRENT_INDEX,
    SESSION_ATTR_MEDIA_TYPE,
    SESSION_ATTR_RECOMMENDATIONS,
    SESSION_ATTR_REGION,
    SESSION_ATTR_LAST_SCREEN,
    resolve_region,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handle_view_watchlist(event: dict, user_id: str) -> dict:
    """
    Handles the ViewWatchlistIntent -- user tapped "My Watchlist" on
    the launch screen or asked to see their watchlist.

    Shows all items in the user's watchlist with their current streaming
    availability. This is a lightweight DB read - no AgentCore call needed.
    """
    from watchlist import watchlist as watchlist_client

    if not user_id:
        logger.error("ViewWatchlist missing session.user.userId -- cannot proceed")
        return build_error_response()

    # Check allowlist (authorization)
    user = check_allowlist(user_id)
    if user is None:
        logger.info("Call denied: userId=%s intent=ViewWatchlist reason=not_approved", user_id)
        emit_denial_metric(user_id, "not_approved")
        return build_not_authorized_response()

    # Get watchlist items
    try:
        items = watchlist_client.get_watchlist(user_id, limit=50)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to get watchlist: %s", exc)
        return build_error_response()

    if not items:
        speech_text = "Your watchlist is empty. Tap a movie or TV show card and add it to your watchlist from the detail screen."
        if apl.supports_apl(event):
            # Show empty state message screen
            document, datasources = apl.build_message_document(
                title="📋 My Watchlist",
                body=speech_text,
                icon="📋",
                accent_color="#2C5F8A",
            )
            return build_apl_directive_response(
                speech_text=speech_text,
                document=document,
                datasources=datasources,
                token=apl.APL_TOKEN_MESSAGE,
                should_end_session=False,
            )
        return build_speech_response(speech_text=speech_text, should_end_session=False)

    speech_text = f"Here are {len(items)} items in your watchlist."
    logger.info("View watchlist: user=%s count=%d", user_id, len(items))

    # Build watchlist document
    if apl.supports_apl(event):
        document, datasources = build_watchlist_document(items)
        return build_apl_directive_response(
            speech_text=speech_text,
            document=document,
            datasources=datasources,
            token="watchlistScreen",
            should_end_session=False,
        )

    return build_speech_response(speech_text=speech_text, should_end_session=False)


def build_watchlist_document(items: list[dict]) -> tuple[dict, dict]:
    """
    Builds an APL document for displaying the watchlist.

    Similar to recommendations grid but with availability info shown
    on each card. Shows an empty state when the watchlist is empty.

    Trailer URLs and watch provider data are already stored in the
    watchlist items (added when items were saved), so no API calls needed.
    """
    card = {
        "type": "TouchWrapper",
        "id": "watchlistCard${index}",
        "onPress": [{"type": "SendEvent", "arguments": ["viewWatchlistItem", "${index}"]}],
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
                        "type": "Image",
                        "source": "${data.poster_url}",
                        "width": "100%",
                        "height": "330dp",
                        "scale": "best-fill",
                        "borderRadius": "14dp",
                        "overlayGradient": {
                            "type": "linear",
                            "angle": 0,
                            "colorRange": ["#000000CC", "transparent"],
                            "inputRange": [0, 0.45],
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
                # YouTube trailer button on the watchlist card - opens in Silk browser
                {
                    "type": "TouchWrapper",
                    "id": "watchlistTrailerButton${index}",
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
                    "type": "TouchWrapper",
                    "when": "${data.availability && data.availability.stream && data.availability.stream.length > 0 && data.availability.link}",
                    "onPress": [{"type": "OpenURL", "source": "${data.availability.link}"}],
                    "item": {
                        "type": "Text",
                        "text": "Streaming on ${data.availability.stream[0].name}",
                        "fontSize": "13dp",
                        "fontWeight": "600",
                        "color": "#1C8C7C",
                        "textAlign": "center",
                        "paddingTop": "4dp",
                    },
                },
                {
                    "type": "Text",
                    "when": "${data.availability && data.availability.stream && data.availability.stream.length > 0 && !data.availability.link}",
                    "text": "Streaming on ${data.availability.stream[0].name}",
                    "fontSize": "13dp",
                    "fontWeight": "600",
                    "color": "#1C8C7C",
                    "textAlign": "center",
                    "paddingTop": "4dp",
                },
                {
                    "type": "Text",
                    "when": "${!data.availability || !data.availability.stream || data.availability.stream.length == 0}",
                    "text": "Not currently streaming",
                    "fontSize": "13dp",
                    "color": COLOR_TEXT_MUTED,
                    "textAlign": "center",
                    "paddingTop": "4dp",
                },
                # Remove from watchlist button for each item
                {
                    "type": "TouchWrapper",
                    "id": "removeFromWatchlistItem",
                    "onPress": [{"type": "SendEvent", "arguments": ["removeFromWatchlistItem", "${data.tmdbId}", "${data.media_type}"]}],
                    "item": {
                        "type": "Frame",
                        "borderRadius": "20dp",
                        "backgroundColor": "#B94A48",
                        "paddingLeft": "16dp",
                        "paddingRight": "16dp",
                        "paddingTop": "8dp",
                        "paddingBottom": "8dp",
                        "item": {
                            "type": "Text",
                            "text": "🗑️ Remove from Watchlist",
                            "fontSize": "15dp",
                            "fontWeight": "600",
                            "color": COLOR_TEXT_PRIMARY,
                            "textAlign": "center",
                        },
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
            _pill("backButton", "‹  Back", ["launchAction", "go_home"], "#5A5A5A"),
            _pill("mainMenuButton", "🏠  Main Menu", ["launchAction", "go_home"], "#5A5A5A"),
        ],
    }

    # Build main content - show empty state if no items
    if items:
        main_content = {
            "type": "Sequence",
            "id": "watchlistGrid",
            "scrollDirection": "horizontal",
            "width": "100%",
            "grow": 1,
            "paddingLeft": "30dp",
            "paddingRight": "30dp",
            "data": "${payload.watchlistData.properties.items}",
            "item": card,
        }
    else:
        # Empty state - show a message instead of an empty grid
        main_content = {
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
                    "text": "📋 Your watchlist is empty",
                    "fontSize": "28dp",
                    "fontWeight": "700",
                    "color": COLOR_TEXT_PRIMARY,
                    "textAlign": "center",
                    "maxLines": 1,
                    "paddingBottom": "12dp",
                },
                {
                    "type": "Text",
                    "text": "Tap a movie or TV show card and add it to your watchlist from the detail screen.",
                    "fontSize": "20dp",
                    "color": COLOR_TEXT_SECONDARY,
                    "textAlign": "center",
                    "maxLines": 3,
                },
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
                        _screen_header("📋 My Watchlist", "Tap an item to see details"),
                        main_content,
                        footer,
                    ],
                )
            ],
        },
    }

    datasources = {
        "watchlistData": {
            "type": "object",
            "objectId": "watchlist",
            "properties": {
                "items": [
                    {
                        "title": item.get("title", ""),
                        "poster_url": item.get("posterUrl") or _PLACEHOLDER_POSTER_URL,
                        "media_type": item.get("mediaType", "movie"),
                        "mediaType": item.get("mediaType", "movie"),  # Keep original casing for APL template
                        "tmdbId": item.get("tmdbId"),
                        "rating": round(float(item.get("rating") or 0), 1),
                        "availability": item.get("availability", {}),
                        "hasTrailer": bool(item.get("trailer_url")),
                        "trailer_url": item.get("trailer_url", ""),
                    }
                    for item in items
                ]
            },
        }
    }

    return document, datasources


def handle_view_watchlist_item(event: dict, arguments: list, user_id: str, session_attributes: dict) -> dict:
    """
    Handles tapping an item in the watchlist grid to show the detail screen.
    """
    from watchlist import watchlist as watchlist_client

    try:
        index = int(arguments[1])
    except (IndexError, ValueError, TypeError):
        logger.warning("viewWatchlistItem UserEvent missing/invalid index argument: %s", arguments)
        return build_error_response()

    # Get watchlist items
    try:
        items = watchlist_client.get_watchlist(user_id, limit=50)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to get watchlist for viewWatchlistItem: %s", exc)
        return build_error_response()

    if index < 0 or index >= len(items):
        logger.warning("viewWatchlistItem index %d out of range (have %d items)", index, len(items))
        return build_error_response()

    item = items[index]

    # Build detail document - need to convert item format to recommendation format
    # Ensure trailer_url is available - fetch if missing
    trailer_url = item.get("trailer_url", "")
    if not trailer_url and item.get("tmdbId"):
        try:
            from agents.boredom_buster_agent.tmdb_client import get_trailer_url
            trailer_url = get_trailer_url(item.get("mediaType", "movie"), item.get("tmdbId"))
        except Exception:
            pass  # Silently ignore trailer fetch failures

    # Ensure watch_providers is available - convert from cached availability or fetch fresh
    watch_providers = {}
    availability = item.get("availability", {})
    # Resolve region for potential provider lookup (also used when persisting session attrs below)
    user_region = resolve_region(session_attributes, event.get("request", {}).get("locale"))

    if availability:
        # Convert watchlist availability format to watch_providers format
        # Watchlist stores: {"stream": [...], "rent": [...], "buy": [...], "link": "..."}
        # Detail screen expects the same format, so use it directly
        watch_providers = availability
    elif item.get("tmdbId"):
        # No cached availability - fetch fresh from TMDB
        try:
            from agents.boredom_buster_agent.tmdb_client import get_watch_providers
            watch_providers = get_watch_providers(item.get("mediaType", "movie"), item.get("tmdbId"), region=user_region)
        except Exception:
            pass  # Silently ignore provider fetch failures

    recommendation = {
        "title": item.get("title", ""),
        "poster_url": item.get("posterUrl"),
        "media_type": item.get("mediaType", "movie"),
        "rating": item.get("rating", 0),
        "overview": item.get("overview", ""),
        "release_date": item.get("releaseDate", ""),
        "trailer_url": trailer_url,
        "watch_providers": watch_providers,
    }

    # Format watchlist date
    from datetime import datetime
    watchlist_date = ""
    added_at = item.get("addedAt", 0)
    if added_at:
        # DynamoDB returns numbers as Decimal - convert to int
        dt = datetime.fromtimestamp(int(added_at))
        watchlist_date = dt.strftime("%b %d, %Y")

    speech_text = f"Here's more about {item.get('title', 'this title')}."

    if apl.supports_apl(event):
        # This item is from the watchlist, so it's already in the watchlist
        document, datasources = apl.build_detail_document(recommendation, in_watchlist=True, watchlist_date=watchlist_date)
        return build_apl_directive_response(
            speech_text=speech_text,
            document=document,
            datasources=datasources,
            token=apl.APL_TOKEN_DETAIL,
            session_attributes={
                "watchlist_detail_item": item,
                SESSION_ATTR_MEDIA_TYPE: item.get("mediaType", "movie"),
                SESSION_ATTR_REGION: user_region,
                # Store screen state for return functionality
                SESSION_ATTR_LAST_SCREEN: {
                    "type": "detail",
                    "recommendation": recommendation,
                    "recommendations": [],  # No recommendations list for watchlist items
                    "current_index": None,
                    "media_type": item.get("mediaType", "movie"),
                    "in_watchlist": True,
                    "watchlist_date": watchlist_date,
                },
            },
        )

    return build_speech_response(speech_text=speech_text, should_end_session=False)


def handle_remove_from_watchlist_item(event: dict, arguments: list, user_id: str, session_attributes: dict) -> dict:
    """
    Handles the removeFromWatchlistItem UserEvent from the watchlist screen.
    Removes the selected item from the user's watchlist and refreshes the watchlist screen.
    """
    from watchlist import watchlist as watchlist_client

    try:
        tmdb_id = arguments[1]
        media_type = arguments[2]
    except (IndexError, ValueError, TypeError):
        logger.warning("removeFromWatchlistItem UserEvent missing/invalid tmdbId or media_type argument: %s", arguments)
        return build_error_response()

    # Get watchlist items
    try:
        items = watchlist_client.get_watchlist(user_id, limit=50)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to get watchlist for removeFromWatchlistItem: %s", exc)
        return build_error_response()

    # Find item by tmdbId and media_type
    item = None
    for i in items:
        if i.get("tmdbId") == tmdb_id and i.get("mediaType") == media_type:
            item = i
            break

    if item is None:
        logger.warning("removeFromWatchlistItem item not found for tmdbId=%s media_type=%s", tmdb_id, media_type)
        return build_error_response()

    title = item.get("title", "that title")

    if not tmdb_id:
        logger.error("removeFromWatchlistItem missing tmdbId in watchlist item")
        return build_error_response()

    if not user_id:
        logger.error("removeFromWatchlistItem UserEvent missing session.user.userId -- cannot proceed")
        return build_error_response()

    # Check allowlist (authorization)
    user = check_allowlist(user_id)
    if user is None:
        logger.info("Call denied: userId=%s intent=RemoveFromWatchlistItem reason=not_approved", user_id)
        emit_denial_metric(user_id, "not_approved")
        return build_not_authorized_response()

    try:
        watchlist_client.remove_from_watchlist(
            user_id=user_id,
            media_type=media_type,
            tmdb_id=tmdb_id,
        )
        speech_text = f"Removed {title} from your watchlist."
        logger.info("Removed from watchlist item: user=%s media_type=%s tmdb_id=%s title=%s", user_id, media_type, tmdb_id, title)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to remove from watchlist item: %s", exc)
        speech_text = f"Sorry, I couldn't remove {title} from your watchlist."

    # Re-render the watchlist screen with updated content
    if apl.supports_apl(event):
        try:
            updated_items = watchlist_client.get_watchlist(user_id, limit=50)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to get updated watchlist: %s", exc)
            updated_items = []

        document, datasources = build_watchlist_document(updated_items)
        return build_apl_directive_response(
            speech_text=speech_text,
            document=document,
            datasources=datasources,
            token="watchlistScreen",
            should_end_session=False,
        )

    return build_speech_response(speech_text=speech_text, should_end_session=False)


def handle_remove_from_watchlist(
    event: dict, arguments: list, user_id: str, session_attributes: dict, recommendations: list
) -> dict:
    """
    Handles the removeFromWatchlist UserEvent from the detail screen.
    Removes the current item from the user's watchlist and refreshes the screen.

    This handler supports two scenarios:
    1. Removing from a recommendation detail screen (has SESSION_ATTR_CURRENT_INDEX)
    2. Removing from a watchlist item detail screen (has watchlist_detail_item)
    """
    # Check if we have a watchlist detail item (from viewing watchlist items)
    watchlist_item = session_attributes.get("watchlist_detail_item")
    current_index = None

    if watchlist_item:
        # Removing from a watchlist item's detail screen
        title = watchlist_item.get("title", "that title")
        media_type = watchlist_item.get("mediaType", "movie")
        tmdb_id = watchlist_item.get("tmdbId")

        # Build recommendation dict for re-rendering detail screen
        recommendation = {
            "title": title,
            "poster_url": watchlist_item.get("posterUrl", ""),
            "media_type": media_type,
            "id": tmdb_id,
            "rating": watchlist_item.get("rating", 0),
            "overview": watchlist_item.get("overview", ""),
            "release_date": watchlist_item.get("releaseDate", ""),
            "trailer_url": watchlist_item.get("trailer_url", ""),
            "watch_providers": watchlist_item.get("availability", {}),
        }
    else:
        # Removing from a recommendation detail screen (original flow)
        current_index = session_attributes.get(SESSION_ATTR_CURRENT_INDEX)
        if current_index is None or not (0 <= current_index < len(recommendations)):
            logger.warning("removeFromWatchlist UserEvent with no current recommendation in session")
            return build_error_response()

        recommendation = recommendations[current_index]
        title = recommendation.get("title", "that title")
        media_type = recommendation.get("media_type", "movie")
        tmdb_id = recommendation.get("id")

    if not user_id:
        logger.error("removeFromWatchlist UserEvent missing session.user.userId -- cannot proceed")
        return build_error_response()

    if not tmdb_id:
        logger.error("removeFromWatchlist UserEvent missing tmdb_id in recommendation")
        return build_error_response()

    # Check allowlist (authorization)
    user = check_allowlist(user_id)
    if user is None:
        logger.info("Call denied: userId=%s intent=RemoveFromWatchlist reason=not_approved", user_id)
        emit_denial_metric(user_id, "not_approved")
        return build_not_authorized_response()

    from watchlist import watchlist as watchlist_client

    try:
        watchlist_client.remove_from_watchlist(
            user_id=user_id,
            media_type=media_type,
            tmdb_id=tmdb_id,
        )
        speech_text = f"Removed {title} from your watchlist."
        logger.info("Removed from watchlist: user=%s media_type=%s tmdb_id=%s title=%s", user_id, media_type, tmdb_id, title)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to remove from watchlist: %s", exc)
        speech_text = f"Sorry, I couldn't remove {title} from your watchlist."

    # After removing, redirect based on source
    if apl.supports_apl(event):
        if watchlist_item:
            # From watchlist detail - refresh the watchlist grid
            try:
                updated_items = watchlist_client.get_watchlist(user_id, limit=50)
                document, datasources = build_watchlist_document(updated_items)
                return build_apl_directive_response(
                    speech_text=speech_text,
                    document=document,
                    datasources=datasources,
                    token="watchlistScreen",
                    should_end_session=False,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to refresh watchlist after removal: %s", exc)
                return build_error_response()
        else:
            # From recommendation detail - re-render detail screen
            document, datasources = apl.build_detail_document(recommendation, in_watchlist=False, watchlist_date="")
            return build_apl_directive_response(
                speech_text=speech_text,
                document=document,
                datasources=datasources,
                token=apl.APL_TOKEN_DETAIL,
                session_attributes={
                    SESSION_ATTR_RECOMMENDATIONS: recommendations,
                    SESSION_ATTR_CURRENT_INDEX: current_index,
                    SESSION_ATTR_MEDIA_TYPE: session_attributes.get(SESSION_ATTR_MEDIA_TYPE, ""),
                    SESSION_ATTR_REGION: session_attributes.get(SESSION_ATTR_REGION, "AU"),
                    # Store screen state for return functionality
                    SESSION_ATTR_LAST_SCREEN: {
                        "type": "detail",
                        "recommendation": recommendation,
                        "recommendations": recommendations,
                        "current_index": current_index,
                        "media_type": session_attributes.get(SESSION_ATTR_MEDIA_TYPE, ""),
                        "in_watchlist": False,
                        "watchlist_date": "",
                    },
                },
            )

    return build_speech_response(speech_text=speech_text, should_end_session=False)


def handle_add_to_watchlist(
    event: dict, arguments: list, user_id: str, session_attributes: dict, recommendations: list
) -> dict:
    """
    Handles the addToWatchlist UserEvent from the detail screen.

    This is a lightweight operation that only writes to DynamoDB -- no
    AgentCore/Bedrock call needed. Still goes through allowlist check for
    authorization but skips rate limiting since it's not an LLM call.
    """
    current_index = session_attributes.get(SESSION_ATTR_CURRENT_INDEX)
    if current_index is None or not (0 <= current_index < len(recommendations)):
        logger.warning("addToWatchlist UserEvent with no current recommendation in session")
        return build_error_response()

    recommendation = recommendations[current_index]
    title = recommendation.get("title", "that title")
    media_type = recommendation.get("media_type", "movie")
    tmdb_id = recommendation.get("id")
    poster_url = recommendation.get("poster_url", "")
    rating = recommendation.get("rating", 0.0)
    overview = recommendation.get("overview", "")
    release_date = recommendation.get("release_date", "")

    if not user_id:
        logger.error("addToWatchlist UserEvent missing session.user.userId -- cannot proceed")
        return build_error_response()

    if not tmdb_id:
        logger.error("addToWatchlist UserEvent missing tmdb_id in recommendation")
        return build_error_response()

    # Check allowlist (authorization)
    user = check_allowlist(user_id)
    if user is None:
        logger.info("Call denied: userId=%s intent=AddToWatchlist reason=not_approved", user_id)
        emit_denial_metric(user_id, "not_approved")
        return build_not_authorized_response()

    # Add to watchlist (no rate limit - this is a local DB write)
    from watchlist import watchlist as watchlist_client

    trailer_url = recommendation.get("trailer_url", "")
    watch_providers = recommendation.get("watch_providers", {})

    try:
        watchlist_client.add_to_watchlist(
            user_id=user_id,
            media_type=media_type,
            tmdb_id=tmdb_id,
            title=title,
            poster_url=poster_url,
            rating=rating,
            overview=overview,
            release_date=release_date,
            trailer_url=trailer_url,
            watch_providers=watch_providers,
        )
        speech_text = f"Added {title} to your watchlist."
        logger.info("Added to watchlist: user=%s media_type=%s tmdb_id=%s title=%s", user_id, media_type, tmdb_id, title)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to add to watchlist: %s", exc)
        speech_text = f"Sorry, I couldn't add {title} to your watchlist."

    # Re-render the SAME detail screen with updated content
    if apl.supports_apl(event):
        # After adding to watchlist, the item is now in watchlist
        # Check if it was already in watchlist (could have been added via voice or tap)
        from datetime import datetime
        in_watchlist = False
        watchlist_date = ""
        try:
            watchlist_item = watchlist_client.get_watchlist_item(
                user_id=user_id,
                media_type=recommendation.get("media_type", "movie"),
                tmdb_id=recommendation.get("id"),
            )
            if watchlist_item:
                in_watchlist = True
                added_at = watchlist_item.get("addedAt", 0)
                if added_at:
                    # DynamoDB returns numbers as Decimal - convert to int
                    dt = datetime.fromtimestamp(int(added_at))
                    watchlist_date = dt.strftime("%b %d, %Y")
        except Exception:
            pass  # Silently ignore watchlist check failures

        document, datasources = apl.build_detail_document(recommendation, in_watchlist=in_watchlist, watchlist_date=watchlist_date)
        return build_apl_directive_response(
            speech_text=speech_text,
            document=document,
            datasources=datasources,
            token=apl.APL_TOKEN_DETAIL,
            session_attributes={
                SESSION_ATTR_RECOMMENDATIONS: recommendations,
                SESSION_ATTR_CURRENT_INDEX: current_index,
                SESSION_ATTR_MEDIA_TYPE: session_attributes.get(SESSION_ATTR_MEDIA_TYPE, ""),
                SESSION_ATTR_REGION: session_attributes.get(SESSION_ATTR_REGION, "AU"),
                # Store screen state for return functionality
                SESSION_ATTR_LAST_SCREEN: {
                    "type": "detail",
                    "recommendation": recommendation,
                    "recommendations": recommendations,
                    "current_index": current_index,
                    "media_type": session_attributes.get(SESSION_ATTR_MEDIA_TYPE, ""),
                    "in_watchlist": in_watchlist,
                    "watchlist_date": watchlist_date,
                },
            },
        )

    return build_speech_response(speech_text=speech_text, should_end_session=False)
