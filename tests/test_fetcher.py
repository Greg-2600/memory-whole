"""Unit tests for the fetcher module."""

import sqlite3
import unittest
from unittest.mock import patch, MagicMock

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

        count = fetcher.fetch_all_feeds(conn, config, max_items=10)
        conn.commit()

        self.assertEqual(count, 1)
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

        count = fetcher.fetch_all_feeds(conn, config, max_items=10)
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
        count = fetcher.fetch_all_feeds(conn, config, max_items=10)
        self.assertEqual(count, 0)
        mock_parse.assert_not_called()
        conn.close()

    def test_empty_config(self) -> None:
        conn = _make_conn()
        count = fetcher.fetch_all_feeds(conn, {}, max_items=10)
        self.assertEqual(count, 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
