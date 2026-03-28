"""Unit tests for rss_reader helpers and rendering."""

import datetime as dt
import tempfile
import unittest
from pathlib import Path

from rss_reader import (
    first_non_empty,
    item_datetime,
    load_config,
    markdown_for_feed,
    slugify,
    strip_html,
    summarize_text,
)


class TestHelpers(unittest.TestCase):
    """Tests for helper utilities."""

    def test_slugify_basic(self) -> None:
        """Slugify converts strings to safe slugs."""
        self.assertEqual(slugify("  Reuters World  "), "reuters-world")
        self.assertEqual(slugify("***"), "feed")

    def test_first_non_empty(self) -> None:
        """first_non_empty returns the first meaningful string."""
        self.assertEqual(first_non_empty(None, "", "  hi  ", "x"), "hi")
        self.assertEqual(first_non_empty(None, 123, "   "), "")

    def test_strip_html(self) -> None:
        """strip_html removes tags and unescapes entities."""
        self.assertEqual(strip_html("<p>Hello &amp; <b>world</b></p>"), "Hello & world")

    def test_summarize_text_sentence_and_word_limits(self) -> None:
        """summarize_text picks initial sentences and respects word limit."""
        text = (
            "First sentence. Second sentence! Third sentence? "
            "Fourth sentence should not appear."
        )
        summary = summarize_text(text, max_sentences=2, max_words=50)
        self.assertEqual(summary, "First sentence. Second sentence!")

        long_text = " ".join(["word"] * 120)
        long_summary = summarize_text(long_text, max_sentences=10, max_words=10)
        self.assertTrue(long_summary.endswith("..."))
        self.assertLessEqual(len(long_summary.split()), 10)

    def test_item_datetime_with_published_parsed(self) -> None:
        """item_datetime extracts a UTC datetime from parsed timetuple."""
        entry = {"published_parsed": dt.datetime(2026, 2, 20, 12, 30, 0).timetuple()}
        parsed = item_datetime(entry)
        self.assertIsNotNone(parsed)
        self.assertEqual(
            parsed, dt.datetime(2026, 2, 20, 12, 30, 0, tzinfo=dt.timezone.utc)
        )


class TestConfig(unittest.TestCase):
    """Tests for configuration loading and defaults."""

    def test_load_config_applies_defaults(self) -> None:
        """load_config should populate missing settings with defaults."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "feeds.yaml"
            config_path.write_text(
                """
feeds:
  - name: Example
    url: https://example.com/rss
""".strip() + "\n",
                encoding="utf-8",
            )

            config = load_config(config_path)
            settings = config["settings"]

            self.assertEqual(settings["output_dir"], "output")
            self.assertEqual(settings["max_items_per_feed"], 25)
            self.assertTrue(settings["merge_all_sources"])
            self.assertEqual(settings["merged_filename"], "daily-news-{date}.md")
            self.assertEqual(settings["daily_title"], "Daily RSS Digest - {date}")
            self.assertFalse(settings["write_individual_feeds"])

    def test_load_config_requires_nonempty_feeds(self) -> None:
        """load_config raises when `feeds` is missing or empty."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "feeds.yaml"
            config_path.write_text("settings: {}\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                load_config(config_path)


class TestMarkdown(unittest.TestCase):
    """Tests for Markdown rendering of feeds and entries."""

    def test_markdown_for_feed_with_entries(self) -> None:
        """markdown_for_feed should render entries including summary and link."""
        entry = {
            "title": "Example Title",
            "link": "https://example.com/article",
            "summary": (
                "<p>Summary sentence one. Summary sentence two. "
                "Summary sentence three.</p>"
            ),
            "published_parsed": dt.datetime(2026, 2, 20, 1, 2, 3).timetuple(),
            "source": {"title": "Example Source"},
        }

        output = markdown_for_feed("My Feed", "https://example.com/rss", [entry])

        self.assertIn("# My Feed", output)
        self.assertIn("Source: https://example.com/rss", output)
        self.assertIn("## Example Title", output)
        self.assertIn("- Source: Example Source", output)
        self.assertIn("- Link: https://example.com/article", output)
        self.assertIn("Summary sentence one. Summary sentence two.", output)

    def test_markdown_for_feed_respects_summary_params(self) -> None:
        """Ensure markdown_for_feed respects explicit summary parameters."""
        long_summary = (
            "Sentence one. Sentence two. Sentence three. Sentence four. "
            "Sentence five. Sentence six. "
            "Sentence seven."
        )
        entry = {
            "title": "Long Summary",
            "link": "https://example.com/long",
            "summary": f"<p>{long_summary}</p>",
            "published_parsed": dt.datetime(2026, 2, 20, 2, 3, 4).timetuple(),
            "source": {"title": "Example Source"},
        }

        # Request up to 4 sentences in the markdown output
        output = markdown_for_feed(
            "Feed", "url", [entry], max_sentences=4, max_words=200
        )
        self.assertIn("Sentence one.", output)
        self.assertIn("Sentence four.", output)
        self.assertNotIn("Sentence seven.", output)

    def test_markdown_for_feed_empty_entries(self) -> None:
        """markdown_for_feed returns a friendly message when no items exist."""
        output = markdown_for_feed("Empty Feed", "https://example.com/rss", [])
        self.assertIn("No items found.", output)


if __name__ == "__main__":
    unittest.main()
