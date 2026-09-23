"""
Detail-screen voice actions and cross-screen navigation --
DetailScreenActionIntent (voice commands spoken while on a Boredom
Buster detail screen: add/remove watchlist, go back, open trailer),
the openTrailer UserEvent (tapping the QR code), and
ReturnToPreviousScreenIntent (returning to whatever screen the user
left via an OpenURL button press).
"""

import logging

from allowlist import check_allowlist
from utils import apl
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
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handle_open_trailer(event: dict, arguments: list, user_id: str) -> dict:
    """
    Handles the openTrailer UserEvent -- user tapped the QR code on the
    detail screen to open the YouTube trailer in their phone's browser.

    Uses APL's OpenURL command to open the trailer URL.
    """
    if len(arguments) < 2:
        logger.warning("openTrailer UserEvent missing trailer URL argument: %s", arguments)
        return build_error_response()

    trailer_url = arguments[1]
    if not trailer_url:
        logger.warning("openTrailer UserEvent with empty trailer URL")
        return build_error_response()

    # Just acknowledge and let the APL OpenURL command handle opening the URL
    speech_text = "Opening trailer."
    return build_speech_response(speech_text=speech_text, should_end_session=False)


def handle_detail_screen_action(event: dict, intent: dict, user_id: str, session_id: str, route_result, session_attributes: dict) -> dict:
    """
    Handles DetailScreenActionIntent - voice commands while on the detail screen.
    Actions: add to watchlist, go back, open trailer.
    """
    action_slot = intent.get("slots", {}).get("Action")
    if not action_slot or not action_slot.get("value"):
        logger.warning("DetailScreenActionIntent missing Action slot")
        # Fall back to showing the launch screen
        from .launch_screen import build_launch_response_with_cards
        return build_launch_response_with_cards()

    action = action_slot.get("value")
    recommendations = session_attributes.get(SESSION_ATTR_RECOMMENDATIONS, [])
    current_index = session_attributes.get(SESSION_ATTR_CURRENT_INDEX)

    if action == "add to watchlist":
        # Use the APL event handler logic for adding to watchlist
        if current_index is None or not (0 <= current_index < len(recommendations)):
            logger.warning("add to watchlist: no current recommendation in session")
            speech_text = "I'm not sure which title to add. Please go back and try again."
            return build_speech_response(speech_text=speech_text, should_end_session=False)

        recommendation = recommendations[current_index]
        title = recommendation.get("title", "that title")
        media_type = recommendation.get("media_type", "movie")
        tmdb_id = recommendation.get("id")
        poster_url = recommendation.get("poster_url", "")
        rating = recommendation.get("rating", 0.0)
        overview = recommendation.get("overview", "")
        release_date = recommendation.get("release_date", "")

        if not tmdb_id:
            logger.error("add to watchlist: missing tmdb_id")
            speech_text = f"Sorry, I couldn't add {title} to your watchlist."
            return build_speech_response(speech_text=speech_text, should_end_session=False)

        # Check allowlist (authorization)
        user = check_allowlist(user_id)
        if user is None:
            return build_not_authorized_response()

        from watchlist import watchlist as watchlist_client
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
            )
            speech_text = f"Added {title} to your watchlist."
            logger.info("Added to watchlist via voice: user=%s title=%s", user_id, title)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to add to watchlist via voice: %s", exc)
            speech_text = f"Sorry, I couldn't add {title} to your watchlist."

        # Re-render the SAME detail screen with updated watchlist status
        if apl.supports_apl(event):
            # After adding to watchlist, the item is now in watchlist
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
                },
            )
        return build_speech_response(speech_text=speech_text, should_end_session=False)

    elif action == "go back":
        # Return to recommendations grid
        if not recommendations:
            logger.warning("go back: no stored recommendations in session")
            from .launch_screen import build_launch_response_with_cards
            return build_launch_response_with_cards()

        media_type = session_attributes.get(SESSION_ATTR_MEDIA_TYPE, "")
        document, datasources = apl.build_recommendations_document(recommendations, media_type)
        return build_apl_directive_response(
            speech_text="Here are your recommendations again.",
            document=document,
            datasources=datasources,
            token=apl.APL_TOKEN_RECOMMENDATIONS,
            session_attributes={
                SESSION_ATTR_RECOMMENDATIONS: recommendations,
                SESSION_ATTR_MEDIA_TYPE: media_type,
                SESSION_ATTR_REGION: session_attributes.get(SESSION_ATTR_REGION, "AU"),
            },
        )

    elif action == "open trailer":
        # Open trailer via APL OpenURL command
        if current_index is None or not (0 <= current_index < len(recommendations)):
            logger.warning("open trailer: no current recommendation in session")
            speech_text = "I'm not sure which trailer to open. Please go back and try again."
            return build_speech_response(speech_text=speech_text, should_end_session=False)

        recommendation = recommendations[current_index]
        trailer_url = recommendation.get("trailer_url", "")
        if not trailer_url:
            speech_text = "No trailer available for this title."
            return build_speech_response(speech_text=speech_text, should_end_session=False)

        speech_text = "Opening trailer."
        return build_speech_response(speech_text=speech_text, should_end_session=False)

    elif action == "remove from watchlist":
        # Use the APL event handler logic for removing from watchlist
        if current_index is None or not (0 <= current_index < len(recommendations)):
            logger.warning("remove from watchlist: no current recommendation in session")
            speech_text = "I'm not sure which title to remove. Please go back and try again."
            return build_speech_response(speech_text=speech_text, should_end_session=False)

        recommendation = recommendations[current_index]
        title = recommendation.get("title", "that title")
        media_type = recommendation.get("media_type", "movie")
        tmdb_id = recommendation.get("id")

        if not tmdb_id:
            logger.error("remove from watchlist: missing tmdb_id")
            speech_text = f"Sorry, I couldn't remove {title} from your watchlist."
            return build_speech_response(speech_text=speech_text, should_end_session=False)

        # Check allowlist (authorization)
        user = check_allowlist(user_id)
        if user is None:
            return build_not_authorized_response()

        from watchlist import watchlist as watchlist_client
        try:
            watchlist_client.remove_from_watchlist(
                user_id=user_id,
                media_type=media_type,
                tmdb_id=tmdb_id,
            )
            speech_text = f"Removed {title} from your watchlist."
            logger.info("Removed from watchlist via voice: user=%s title=%s", user_id, title)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to remove from watchlist via voice: %s", exc)
            speech_text = f"Sorry, I couldn't remove {title} from your watchlist."

        # Re-render the SAME detail screen with updated watchlist status
        if apl.supports_apl(event):
            # After removing, the item is no longer in watchlist
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
                },
            )
        return build_speech_response(speech_text=speech_text, should_end_session=False)

    else:
        logger.warning("Unknown detail screen action: %s", action)
        from .launch_screen import build_launch_response_with_cards
        return build_launch_response_with_cards()


def handle_return_to_previous_screen(event: dict) -> dict:
    """
    Handles ReturnToPreviousScreenIntent - user said "return", "back to jarvis ai"
    to return to where they were before leaving the skill via button press.

    Restores the saved screen state if available, otherwise goes to main menu.
    """
    from .launch_screen import build_launch_response_with_cards
    from .watchlist import handle_view_watchlist

    session = event.get("session", {})
    session_attributes = session.get("attributes", {}) or {}
    last_screen = session_attributes.get(SESSION_ATTR_LAST_SCREEN)

    if not last_screen:
        # No saved screen - go to main menu
        logger.info("ReturnToPreviousScreen: no saved screen state, showing main menu")
        return build_launch_response_with_cards()

    screen_type = last_screen.get("type")
    user_id = session.get("user", {}).get("userId", "")

    if screen_type == "recommendations":
        # Restore recommendations grid
        recommendations = last_screen.get("recommendations", [])
        media_type = last_screen.get("media_type", "")
        if not recommendations:
            logger.warning("ReturnToPreviousScreen: saved recommendations screen has no data")
            return build_launch_response_with_cards()

        if apl.supports_apl(event):
            document, datasources = apl.build_recommendations_document(recommendations, media_type, user_id)
            return build_apl_directive_response(
                speech_text="Welcome back. Here are your recommendations.",
                document=document,
                datasources=datasources,
                token=apl.APL_TOKEN_RECOMMENDATIONS,
                session_attributes={
                    SESSION_ATTR_RECOMMENDATIONS: recommendations,
                    SESSION_ATTR_MEDIA_TYPE: media_type,
                    SESSION_ATTR_REGION: session_attributes.get(SESSION_ATTR_REGION, "AU"),
                },
            )
        return build_speech_response(
            speech_text="Welcome back. Here are your recommendations.",
            should_end_session=False,
        )

    elif screen_type == "detail":
        # Restore detail screen
        recommendation = last_screen.get("recommendation")
        recommendations = last_screen.get("recommendations", [])
        current_index = last_screen.get("current_index")
        in_watchlist = last_screen.get("in_watchlist", False)
        watchlist_date = last_screen.get("watchlist_date", "")

        if not recommendation:
            logger.warning("ReturnToPreviousScreen: saved detail screen has no recommendation data")
            return build_launch_response_with_cards()

        if apl.supports_apl(event):
            document, datasources = apl.build_detail_document(recommendation, in_watchlist, watchlist_date)
            return build_apl_directive_response(
                speech_text=f"Welcome back. Here's {recommendation.get('title', 'your title')}.",
                document=document,
                datasources=datasources,
                token=apl.APL_TOKEN_DETAIL,
                session_attributes={
                    SESSION_ATTR_RECOMMENDATIONS: recommendations,
                    SESSION_ATTR_CURRENT_INDEX: current_index,
                    SESSION_ATTR_MEDIA_TYPE: last_screen.get("media_type", ""),
                    SESSION_ATTR_REGION: session_attributes.get(SESSION_ATTR_REGION, "AU"),
                },
            )
        return build_speech_response(
            speech_text=f"Welcome back. Here's {recommendation.get('title', 'your title')}.",
            should_end_session=False,
        )

    elif screen_type == "watchlist":
        # Restore watchlist grid
        if not user_id:
            logger.error("ReturnToPreviousScreen: watchlist screen but no user_id")
            return build_launch_response_with_cards()

        return handle_view_watchlist(event, user_id)

    else:
        # Unknown screen type - go to main menu
        logger.warning("ReturnToPreviousScreen: unknown screen type %s", screen_type)
        return build_launch_response_with_cards()
