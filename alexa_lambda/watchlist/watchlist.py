"""
watchlist.py -- Watchlist management for Boredom Buster.

Provides per-user watchlist functionality using DynamoDB.
Each entry tracks:
- TMDB ID and media type (movie/TV)
- Title, poster, rating
- Date added
- Region availability (cached and refreshed periodically)
- User notes/tags

The watchlist screen shows all items with their current streaming
availability in the user's region at a glance.
"""

import json
import logging
import os
import time
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger("boredom_buster_watchlist")
logger.setLevel(logging.INFO)

# DynamoDB table name from environment
DYNAMODB_REGION = os.environ.get("AWS_REGION", "us-east-1")
TABLE_ENV_VAR = "SKILL_WATCHLIST_TABLE"

# Cache DynamoDB resource across invocations
_dynamodb = None
_table = None


def _get_table_name():
    """Get the table name from environment, raise if not set."""
    table_name = os.environ.get(TABLE_ENV_VAR, "")
    if not table_name:
        raise ValueError(f"{TABLE_ENV_VAR} environment variable not set")
    return table_name


def _get_table():
    """Get or create DynamoDB table resource."""
    global _dynamodb, _table
    if _table is None:
        table_name = _get_table_name()
        _dynamodb = boto3.resource("dynamodb", region_name=DYNAMODB_REGION)
        _table = _dynamodb.Table(table_name)
    return _table


def _make_item_id(media_type: str, tmdb_id: int) -> str:
    """Create composite sort key: media_type:tmdb_id"""
    return f"{media_type}:{tmdb_id}"


def _parse_item_id(item_id: str) -> tuple[str, int]:
    """Parse composite sort key back to media_type and tmdb_id."""
    parts = item_id.split(":", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid itemId format: {item_id}")
    return parts[0], int(parts[1])


def add_to_watchlist(
    user_id: str,
    media_type: str,
    tmdb_id: int,
    title: str,
    poster_url: str = "",
    rating: float = 0.0,
    overview: str = "",
    release_date: str = "",
    trailer_url: str = "",
    watch_providers: dict | None = None,
    user_note: str = "",
) -> dict[str, Any]:
    """
    Add a title to the user's watchlist.

    Includes trailer_url and watch_providers to avoid repeated API calls.
    Returns the created item or existing item if already present.
    """
    table = _get_table()
    item_id = _make_item_id(media_type, tmdb_id)
    now = int(time.time())

    item = {
        "userId": user_id,
        "itemId": item_id,
        "mediaType": media_type,
        "tmdbId": tmdb_id,
        "title": title,
        "posterUrl": poster_url,
        "rating": Decimal(str(round(rating, 1))),
        "overview": overview,
        "releaseDate": release_date,
        "trailer_url": trailer_url,  # Store trailer URL to avoid repeated API calls
        "userNote": user_note,
        "addedAt": now,
        "updatedAt": now,
        # Store watch providers data to avoid repeated API calls
        # Format: {"region": "AU", "link": "...", "stream": [...], "rent": [...], "buy": [...]}
        "availability": watch_providers if watch_providers else {},
        "availabilityUpdatedAt": now if watch_providers else 0,
    }

    try:
        # Use conditional put to avoid overwriting existing
        # Check only itemId (sort key) since userId (partition key) may exist for other items
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(itemId)",
        )
        logger.info("Added to watchlist: user=%s media_type=%s tmdb_id=%d", user_id, media_type, tmdb_id)
        return item
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # Already exists - return existing
        existing = get_watchlist_item(user_id, media_type, tmdb_id)
        if existing:
            return existing
        # Race condition - try to get it again
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to add to watchlist: %s", exc)
        raise


def remove_from_watchlist(user_id: str, media_type: str, tmdb_id: int) -> bool:
    """
    Remove a title from the user's watchlist.

    Returns True if removed, False if not found.
    """
    table = _get_table()
    item_id = _make_item_id(media_type, tmdb_id)

    try:
        table.delete_item(
            Key={"userId": user_id, "itemId": item_id},
            ConditionExpression="attribute_exists(userId)",
        )
        logger.info("Removed from watchlist: user=%s media_type=%s tmdb_id=%d", user_id, media_type, tmdb_id)
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to remove from watchlist: %s", exc)
        raise


def get_watchlist_item(user_id: str, media_type: str, tmdb_id: int) -> dict | None:
    """Get a single watchlist item."""
    table = _get_table()
    item_id = _make_item_id(media_type, tmdb_id)

    try:
        response = table.get_item(Key={"userId": user_id, "itemId": item_id})
        return response.get("Item")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to get watchlist item: %s", exc)
        return None


def get_watchlist(user_id: str, limit: int = 100) -> list[dict]:
    """
    Get all items in a user's watchlist, sorted by most recently added.

    Returns list of items with availability info.
    """
    table = _get_table()

    try:
        response = table.query(
            KeyConditionExpression=Key("userId").eq(user_id),
            ScanIndexForward=False,  # Descending by itemId (most recent first)
            Limit=limit,
        )
        return response.get("Items", [])
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to get watchlist: %s", exc)
        return []


def update_availability(
    user_id: str,
    media_type: str,
    tmdb_id: int,
    region: str,
    providers: dict,
) -> bool:
    """
    Update the cached availability for a watchlist item.

    `providers` should be the output from tmdb_client.get_watch_providers().
    """
    table = _get_table()
    item_id = _make_item_id(media_type, tmdb_id)
    now = int(time.time())

    # Convert providers to JSON-serializable format
    availability_data = {
        "region": region,
        "stream": providers.get("stream", []),
        "rent": providers.get("rent", []),
        "buy": providers.get("buy", []),
        "link": providers.get("link", ""),
        "updatedAt": now,
    }

    try:
        table.update_item(
            Key={"userId": user_id, "itemId": item_id},
            UpdateExpression="SET availability = :avail, availabilityUpdatedAt = :now",
            ExpressionAttributeValues={
                ":avail": availability_data,
                ":now": now,
            },
            ConditionExpression="attribute_exists(userId)",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to update availability: %s", exc)
        return False


def refresh_all_availability(user_id: str, region: str) -> int:
    """
    Refresh availability for all items in a user's watchlist.

    Calls TMDB for each item (with rate limiting). Returns count updated.
    This should be called from a background Lambda, not inline.
    """
    from tmdb_client import get_watch_providers, TmdbClientError

    items = get_watchlist(user_id)
    updated = 0

    for item in items:
        try:
            media_type = item.get("mediaType", "movie")
            tmdb_id = item.get("tmdbId")
            if not tmdb_id:
                continue

            providers = get_watch_providers(media_type, tmdb_id, region)
            if update_availability(user_id, media_type, tmdb_id, region, providers):
                updated += 1
        except TmdbClientError as exc:
            logger.warning("TMDB error refreshing %s: %s", item.get("title", "unknown"), exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error refreshing watchlist item: %s", exc)

    return updated


def add_watchlist_note(user_id: str, media_type: str, tmdb_id: int, note: str) -> bool:
    """Add or update a user's personal note for a watchlist item."""
    table = _get_table()
    item_id = _make_item_id(media_type, tmdb_id)
    now = int(time.time())

    try:
        table.update_item(
            Key={"userId": user_id, "itemId": item_id},
            UpdateExpression="SET userNote = :note, updatedAt = :now",
            ExpressionAttributeValues={
                ":note": note,
                ":now": now,
            },
            ConditionExpression="attribute_exists(userId)",
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to update watchlist note: %s", exc)
        return False


def is_in_watchlist(user_id: str, media_type: str, tmdb_id: int) -> bool:
    """Check if a title is already in the user's watchlist."""
    return get_watchlist_item(user_id, media_type, tmdb_id) is not None