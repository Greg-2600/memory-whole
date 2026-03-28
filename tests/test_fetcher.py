"""Unit tests for the fetcher module."""

import sqlite3
import unittest
from unittest.mock import MagicMock, patch

import db
import fetcher


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


class TestFetchAllFeeds(unittest.TestCase):
    """Tests for fetcher.fetch_all_feeds with mocked network."""

    @patch("fetcher.feedparser.parse")
    def test_fetches_and_upserts(self, mock_parse: MagicMock) -> None:
        mock_entry = MagicMock()
        mock_entry.get = lambda key, default=None: {
            "title": "Test Article",
            "link": "http://example.com/article",
            "summary": "A test summary.",
            "content": None,
            "description": "A test description.",
            "published_parsed": (2026, 3, 27, 12, 0, 0, 0, 0, 0),
        }.get(key, default)

        mock_result = MagicMock()
        mock_result.entries = [mock_entry]
        mock_parse.return_value = mock_result

        conn = _make_conn()
        config = {
            "feeds": [
                {"name": "Test Feed", "url": "http://example.com/rss"},
            ],
            "settings": {
                "summary_max_sentences": 3,
                "summary_max_words": 50,
            },
        }

        count, health = fetcher.fetch_all_feeds(conn, config, max_items=10)
        conn.commit()

        self.assertEqual(count, 1)
        self.assertEqual(len(health), 1)
        self.assertTrue(health[0].ok)
        self.assertEqual(db.headline_count(conn), 1)
        row = conn.execute("SELECT title, source FROM headlines").fetchone()
        self.assertEqual(row["title"], "Test Article")
        self.assertEqual(row["source"], "Test Feed")
        conn.close()

    @patch("fetcher.feedparser.parse")
    def test_skips_entries_without_link(self, mock_parse: MagicMock) -> None:
        mock_entry = MagicMock()
        mock_entry.get = lambda key, default=None: {
            "title": "No Link",
            "link": "",
            "summary": "Missing link.",
        }.get(key, default)

        mock_result = MagicMock()
        mock_result.entries = [mock_entry]
        mock_parse.return_value = mock_result

        conn = _make_conn()
        config = {
            "feeds": [{"name": "Feed", "url": "http://example.com/rss"}],
            "settings": {},
        }

        count, health = fetcher.fetch_all_feeds(conn, config, max_items=10)
        conn.commit()
        self.assertEqual(count, 0)
        self.assertEqual(db.headline_count(conn), 0)
        conn.close()

    @patch("fetcher.feedparser.parse")
    def test_skips_feeds_without_url(self, mock_parse: MagicMock) -> None:
        conn = _make_conn()
        config = {
            "feeds": [{"name": "Bad Feed"}],
            "settings": {},
        }
        count, health = fetcher.fetch_all_feeds(conn, config, max_items=10)
        self.assertEqual(count, 0)
        mock_parse.assert_not_called()
        conn.close()

    def test_empty_config(self) -> None:
        conn = _make_conn()
        count, health = fetcher.fetch_all_feeds(conn, {}, max_items=10)
        self.assertEqual(count, 0)
        self.assertEqual(health, [])
        conn.close()

    @patch("fetcher.feedparser.parse")
    def test_feed_health_records_failure(self, mock_parse: MagicMock) -> None:
        mock_parse.side_effect = Exception("Connection refused")
        conn = _make_conn()
        config = {
            "feeds": [{"name": "Bad Feed", "url": "http://example.com/rss"}],
            "settings": {},
        }
        count, health = fetcher.fetch_all_feeds(conn, config, max_items=10)
        self.assertEqual(count, 0)
        self.assertEqual(len(health), 1)
        self.assertFalse(health[0].ok)
        self.assertIn("Connection refused", health[0].error)
        conn.close()

    @patch("fetcher.feedparser.parse")
    def test_feed_health_records_timing(self, mock_parse: MagicMock) -> None:
        mock_result = MagicMock()
        mock_result.entries = []
        mock_result.bozo = False
        mock_parse.return_value = mock_result
        conn = _make_conn()
        config = {
            "feeds": [{"name": "Feed", "url": "http://example.com/rss"}],
            "settings": {},
        }
        count, health = fetcher.fetch_all_feeds(conn, config, max_items=10)
        self.assertEqual(len(health), 1)
        self.assertTrue(health[0].ok)
        self.assertGreaterEqual(health[0].fetch_time_ms, 0)
        conn.close()


class TestValidateFeeds(unittest.TestCase):
    """Tests for feed configuration validation."""

    def test_valid_feed_passes(self) -> None:
        config = {
            "feeds": [
                {
                    "name": "CNN",
                    "url": "https://rss.cnn.com/rss.xml",
                    "lean": "center-left",
                },
            ]
        }
        results = fetcher.validate_feeds(config)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)
        self.assertEqual(results[0].warnings, [])

    def test_missing_name_is_error(self) -> None:
        config = {"feeds": [{"url": "https://example.com/rss"}]}
        results = fetcher.validate_feeds(config)
        self.assertFalse(results[0].ok)
        self.assertTrue(any("name" in e.lower() for e in results[0].errors))

    def test_missing_url_is_error(self) -> None:
        config = {"feeds": [{"name": "Feed"}]}
        results = fetcher.validate_feeds(config)
        self.assertFalse(results[0].ok)
        self.assertTrue(any("url" in e.lower() for e in results[0].errors))

    def test_bad_url_scheme_is_error(self) -> None:
        config = {"feeds": [{"name": "Feed", "url": "ftp://example.com/rss"}]}
        results = fetcher.validate_feeds(config)
        self.assertFalse(results[0].ok)
        self.assertTrue(any("scheme" in e.lower() for e in results[0].errors))

    def test_invalid_lean_is_warning(self) -> None:
        config = {
            "feeds": [
                {"name": "Feed", "url": "https://example.com/rss", "lean": "purple"},
            ]
        }
        results = fetcher.validate_feeds(config)
        self.assertTrue(results[0].ok)  # warning, not error
        self.assertTrue(len(results[0].warnings) > 0)

    def test_duplicate_name_is_warning(self) -> None:
        config = {
            "feeds": [
                {"name": "Feed", "url": "https://a.com/rss"},
                {"name": "Feed", "url": "https://b.com/rss"},
            ]
        }
        results = fetcher.validate_feeds(config)
        # Second feed should have the duplicate warning
        self.assertTrue(any("Duplicate" in w for w in results[1].warnings))

    def test_empty_feeds_returns_empty(self) -> None:
        results = fetcher.validate_feeds({})
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
