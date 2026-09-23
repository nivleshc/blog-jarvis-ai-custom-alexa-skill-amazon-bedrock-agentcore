"""
Unit tests for alexa_lambda/watchlist/watchlist.py
"""

import pytest
from moto import mock_aws
import boto3
import os
import time
import sys
from decimal import Decimal


# Add alexa_lambda to path
ALEXA_LAMBDA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ALEXA_LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, ALEXA_LAMBDA_ROOT)


@pytest.fixture
def watchlist_table(mocked_aws):
    """Use the table created in mocked_aws fixture."""
    from watchlist import watchlist

    # The table is already created in mocked_aws, just reset cached table reference
    watchlist._table = None
    watchlist._dynamodb = None

    yield mocked_aws["dynamodb"].Table("test-skill-watchlist-dev")

    # Cleanup
    watchlist._table = None
    watchlist._dynamodb = None


class TestWatchlist:
    """Test watchlist module functions."""

    def test_add_to_watchlist_creates_new_item(self, watchlist_table):
        """Test adding a new item to watchlist."""
        from watchlist import watchlist

        item = watchlist.add_to_watchlist(
            user_id="user123",
            media_type="movie",
            tmdb_id=550,
            title="Fight Club",
            poster_url="https://example.com/poster.jpg",
            rating=8.8,
            overview="An insomniac office worker...",
            release_date="1999-10-15",
            user_note="Classic movie",
        )

        assert item["userId"] == "user123"
        assert item["itemId"] == "movie:550"
        assert item["mediaType"] == "movie"
        assert item["tmdbId"] == 550
        assert item["title"] == "Fight Club"
        assert item["posterUrl"] == "https://example.com/poster.jpg"
        # Rating is stored as Decimal in DynamoDB
        assert item["rating"] == Decimal("8.8")
        assert item["overview"] == "An insomniac office worker..."
        assert item["releaseDate"] == "1999-10-15"
        assert item["userNote"] == "Classic movie"
        assert item["availability"] == {}
        assert item["availabilityUpdatedAt"] == 0
        assert "addedAt" in item
        assert "updatedAt" in item

    def test_add_to_watchlist_returns_existing_item(self, watchlist_table):
        """Test that adding duplicate returns existing item."""
        from watchlist import watchlist

        # Add first time
        item1 = watchlist.add_to_watchlist(
            user_id="user123",
            media_type="movie",
            tmdb_id=550,
            title="Fight Club",
            rating=8.8,
        )

        # Add second time (should return existing)
        item2 = watchlist.add_to_watchlist(
            user_id="user123",
            media_type="movie",
            tmdb_id=550,
            title="Fight Club",
            rating=9.0,  # Different rating - should not overwrite
        )

        assert item1["itemId"] == item2["itemId"]
        assert item2["rating"] == Decimal("8.8")  # Original rating preserved

    def test_remove_from_watchlist_removes_existing(self, watchlist_table):
        """Test removing an existing watchlist item."""
        from watchlist import watchlist

        # Add item first
        watchlist.add_to_watchlist(
            user_id="user123",
            media_type="movie",
            tmdb_id=550,
            title="Fight Club",
        )

        # Remove it
        result = watchlist.remove_from_watchlist("user123", "movie", 550)
        assert result is True

        # Verify it's gone
        item = watchlist.get_watchlist_item("user123", "movie", 550)
        assert item is None

    def test_remove_from_watchlist_returns_false_for_missing(self, watchlist_table):
        """Test removing non-existent item returns False."""
        from watchlist import watchlist

        result = watchlist.remove_from_watchlist("user123", "movie", 999)
        assert result is False

    def test_get_watchlist_item_returns_item(self, watchlist_table):
        """Test getting a single watchlist item."""
        from watchlist import watchlist

        watchlist.add_to_watchlist(
            user_id="user123",
            media_type="tv",
            tmdb_id=1399,
            title="Game of Thrones",
            rating=9.2,
        )

        item = watchlist.get_watchlist_item("user123", "tv", 1399)

        assert item is not None
        assert item["title"] == "Game of Thrones"
        assert item["mediaType"] == "tv"
        assert item["tmdbId"] == 1399
        assert item["rating"] == Decimal("9.2")

    def test_get_watchlist_item_returns_none_for_missing(self, watchlist_table):
        """Test getting non-existent item returns None."""
        from watchlist import watchlist

        item = watchlist.get_watchlist_item("user123", "movie", 999)
        assert item is None

    def test_get_watchlist_returns_all_items_sorted(self, watchlist_table):
        """Test getting all watchlist items sorted by most recent."""
        from watchlist import watchlist

        # Add multiple items with small delays to ensure different timestamps
        watchlist.add_to_watchlist(user_id="user123", media_type="movie", tmdb_id=1, title="Movie A")
        time.sleep(0.01)
        watchlist.add_to_watchlist(user_id="user123", media_type="movie", tmdb_id=2, title="Movie B")
        time.sleep(0.01)
        watchlist.add_to_watchlist(user_id="user123", media_type="tv", tmdb_id=3, title="Show C")

        items = watchlist.get_watchlist("user123")

        assert len(items) == 3
        # Should be sorted by most recent first (descending itemId)
        assert items[0]["title"] == "Show C"
        assert items[1]["title"] == "Movie B"
        assert items[2]["title"] == "Movie A"

    def test_get_watchlist_respects_limit(self, watchlist_table):
        """Test get_watchlist respects limit parameter."""
        from watchlist import watchlist

        for i in range(5):
            watchlist.add_to_watchlist(user_id="user123", media_type="movie", tmdb_id=i, title=f"Movie {i}")

        items = watchlist.get_watchlist("user123", limit=2)
        assert len(items) == 2

    def test_get_watchlist_empty_for_new_user(self, watchlist_table):
        """Test get_watchlist returns empty list for user with no items."""
        from watchlist import watchlist

        items = watchlist.get_watchlist("newuser")
        assert items == []

    def test_update_availability_updates_cache(self, watchlist_table):
        """Test updating availability cache for an item."""
        from watchlist import watchlist

        watchlist.add_to_watchlist(
            user_id="user123",
            media_type="movie",
            tmdb_id=550,
            title="Fight Club",
        )

        providers = {
            "stream": [{"name": "Netflix", "logo_path": "/netflix.png"}],
            "rent": [{"name": "Amazon", "logo_path": "/amazon.png"}],
            "buy": [],
            "link": "https://example.com/watch",
        }

        result = watchlist.update_availability("user123", "movie", 550, "AU", providers)
        assert result is True

        # Verify it was stored
        item = watchlist.get_watchlist_item("user123", "movie", 550)
        assert item["availability"]["region"] == "AU"
        assert len(item["availability"]["stream"]) == 1
        assert item["availability"]["stream"][0]["name"] == "Netflix"
        assert item["availability"]["rent"][0]["name"] == "Amazon"
        assert item["availability"]["buy"] == []
        assert item["availability"]["link"] == "https://example.com/watch"
        assert item["availabilityUpdatedAt"] > 0

    def test_update_availability_returns_false_for_missing_item(self, watchlist_table):
        """Test update_availability returns False for non-existent item."""
        from watchlist import watchlist

        providers = {"stream": [], "rent": [], "buy": [], "link": ""}
        result = watchlist.update_availability("user123", "movie", 999, "AU", providers)
        assert result is False

    def test_add_watchlist_note_updates_note(self, watchlist_table):
        """Test adding/updating a user note."""
        from watchlist import watchlist

        watchlist.add_to_watchlist(
            user_id="user123",
            media_type="movie",
            tmdb_id=550,
            title="Fight Club",
        )

        time.sleep(0.1)  # Ensure timestamp difference (100ms)

        result = watchlist.add_watchlist_note("user123", "movie", 550, "Must rewatch!")
        assert result is True

        item = watchlist.get_watchlist_item("user123", "movie", 550)
        assert item["userNote"] == "Must rewatch!"
        assert item["updatedAt"] >= item["addedAt"]  # Could be equal if same second

    def test_add_watchlist_note_returns_false_for_missing(self, watchlist_table):
        """Test adding note to non-existent item returns False."""
        from watchlist import watchlist

        result = watchlist.add_watchlist_note("user123", "movie", 999, "Note")
        assert result is False

    def test_is_in_watchlist_returns_true_for_existing(self, watchlist_table):
        """Test is_in_watchlist returns True for existing item."""
        from watchlist import watchlist

        watchlist.add_to_watchlist(
            user_id="user123",
            media_type="movie",
            tmdb_id=550,
            title="Fight Club",
        )

        assert watchlist.is_in_watchlist("user123", "movie", 550) is True

    def test_is_in_watchlist_returns_false_for_missing(self, watchlist_table):
        """Test is_in_watchlist returns False for non-existent item."""
        from watchlist import watchlist

        assert watchlist.is_in_watchlist("user123", "movie", 999) is False

    def test_make_item_id_format(self):
        """Test item ID format is correct."""
        from watchlist import watchlist

        assert watchlist._make_item_id("movie", 550) == "movie:550"
        assert watchlist._make_item_id("tv", 1399) == "tv:1399"

    def test_parse_item_id_format(self):
        """Test parsing item ID back to components."""
        from watchlist import watchlist

        media_type, tmdb_id = watchlist._parse_item_id("movie:550")
        assert media_type == "movie"
        assert tmdb_id == 550

        media_type, tmdb_id = watchlist._parse_item_id("tv:1399")
        assert media_type == "tv"
        assert tmdb_id == 1399

    def test_parse_item_id_raises_on_invalid(self):
        """Test parsing invalid item ID raises ValueError."""
        from watchlist import watchlist

        with pytest.raises(ValueError):
            watchlist._parse_item_id("invalid")
        with pytest.raises(ValueError):
            watchlist._parse_item_id("movie:tv:123")
        with pytest.raises(ValueError):
            watchlist._parse_item_id("movie:abc")

    def test_add_to_watchlist_requires_table_env_var(self, monkeypatch):
        """Test that missing SKILL_WATCHLIST_TABLE raises clear error."""
        from watchlist import watchlist

        # Reset cached table and remove env var
        watchlist._table = None
        watchlist._dynamodb = None
        monkeypatch.delenv("SKILL_WATCHLIST_TABLE", raising=False)

        with pytest.raises(ValueError, match="SKILL_WATCHLIST_TABLE environment variable not set"):
            watchlist._get_table()

    def test_different_users_have_separate_watchlists(self, watchlist_table):
        """Test that different users have isolated watchlists."""
        from watchlist import watchlist

        watchlist.add_to_watchlist(user_id="user1", media_type="movie", tmdb_id=1, title="Movie A")
        watchlist.add_to_watchlist(user_id="user2", media_type="movie", tmdb_id=2, title="Movie B")

        user1_items = watchlist.get_watchlist("user1")
        user2_items = watchlist.get_watchlist("user2")

        assert len(user1_items) == 1
        assert user1_items[0]["title"] == "Movie A"
        assert len(user2_items) == 1
        assert user2_items[0]["title"] == "Movie B"

    def test_tv_and_movie_same_tmdb_id_are_different(self, watchlist_table):
        """Test that movie:123 and tv:123 are separate entries."""
        from watchlist import watchlist

        watchlist.add_to_watchlist(user_id="user123", media_type="movie", tmdb_id=123, title="Movie")
        watchlist.add_to_watchlist(user_id="user123", media_type="tv", tmdb_id=123, title="Show")

        movie_item = watchlist.get_watchlist_item("user123", "movie", 123)
        tv_item = watchlist.get_watchlist_item("user123", "tv", 123)

        assert movie_item["title"] == "Movie"
        assert tv_item["title"] == "Show"
        assert movie_item["itemId"] == "movie:123"
        assert tv_item["itemId"] == "tv:123"

        all_items = watchlist.get_watchlist("user123")
        assert len(all_items) == 2