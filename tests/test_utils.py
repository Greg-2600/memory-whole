"""Unit tests for the utils module."""

import datetime as dt
import unittest

from utils import (
    color_for_source,
    favicon_for_source,
    first_non_empty,
    item_datetime,
    slugify,
    strip_html,
    summarize_text,
)


class TestSlugify(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(slugify("Hello World!"), "hello-world")

    def test_special_chars(self) -> None:
        self.assertEqual(slugify("A & B / C"), "a-b-c")

    def test_empty_returns_item(self) -> None:
        self.assertEqual(slugify("***"), "item")

    def test_whitespace(self) -> None:
        self.assertEqual(slugify("  lots   of   spaces  "), "lots-of-spaces")


class TestStripHtml(unittest.TestCase):
    def test_removes_tags(self) -> None:
        self.assertEqual(strip_html("<p>hello</p>"), "hello")

    def test_unescapes_entities(self) -> None:
        self.assertEqual(strip_html("&amp; &lt;"), "& <")

    def test_collapses_whitespace(self) -> None:
        self.assertNotIn("  ", strip_html("<p>a</p>  <p>b</p>"))


class TestSummarize(unittest.TestCase):
    def test_respects_sentence_limit(self) -> None:
        text = "One. Two. Three. Four. Five."
        result = summarize_text(text, max_sentences=2, max_words=100)
        self.assertEqual(result, "One. Two.")

    def test_respects_word_limit(self) -> None:
        text = " ".join(["word"] * 50)
        result = summarize_text(text, max_sentences=100, max_words=5)
        self.assertTrue(result.endswith("..."))
        self.assertLessEqual(len(result.split()), 6)  # 5 words + "..."

    def test_empty_input(self) -> None:
        self.assertEqual(summarize_text(""), "")
        self.assertEqual(summarize_text("   "), "")

    def test_html_is_stripped(self) -> None:
        result = summarize_text("<b>Bold text</b>. Second.", max_sentences=1)
        self.assertNotIn("<b>", result)
        self.assertIn("Bold text", result)


class TestFirstNonEmpty(unittest.TestCase):
    def test_returns_first(self) -> None:
        self.assertEqual(first_non_empty("", None, "  hi  ", "x"), "hi")

    def test_returns_empty_on_all_empty(self) -> None:
        self.assertEqual(first_non_empty("", None, "   "), "")

    def test_non_string_skipped(self) -> None:
        self.assertEqual(first_non_empty(42, [], "ok"), "ok")


class TestItemDatetime(unittest.TestCase):
    def test_published_parsed(self) -> None:
        entry = {"published_parsed": dt.datetime(2026, 1, 15, 10, 0, 0).timetuple()}
        result = item_datetime(entry)
        self.assertEqual(result, dt.datetime(2026, 1, 15, 10, 0, 0, tzinfo=dt.timezone.utc))

    def test_updated_parsed_fallback(self) -> None:
        entry = {"updated_parsed": dt.datetime(2026, 6, 1, 8, 0, 0).timetuple()}
        result = item_datetime(entry)
        self.assertIsNotNone(result)

    def test_no_date_returns_none(self) -> None:
        self.assertIsNone(item_datetime({}))
        self.assertIsNone(item_datetime({"published_parsed": None}))


class TestFavicon(unittest.TestCase):
    def test_known_sources(self) -> None:
        for name, expected_domain in [
            ("CNN Top Stories", "cnn.com"),
            ("BBC (US & Canada)", "bbc.co.uk"),
            ("NPR News", "npr.org"),
            ("NY Times", "nytimes.com"),
            ("Breitbart", "breitbart.com"),
            ("Al Jazeera", "aljazeera.com"),
        ]:
            url = favicon_for_source(name)
            self.assertIn(expected_domain, url, f"Failed for {name}")

    def test_empty_returns_empty(self) -> None:
        self.assertEqual(favicon_for_source(""), "")


class TestColorForSource(unittest.TestCase):
    def test_known_sources_have_color(self) -> None:
        self.assertEqual(color_for_source("CNN Top Stories"), "#cc0000")
        self.assertEqual(color_for_source("BBC News"), "#bb1919")

    def test_unknown_source_returns_gray(self) -> None:
        result = color_for_source("Unknown Random Source")
        self.assertEqual(result, "#6b7280")

    def test_new_sources_have_colors(self) -> None:
        for name in [
            "ABC News",
            "CBS News",
            "PBS NewsHour",
            "NY Times",
            "Breitbart",
            "Daily Wire",
            "The Intercept",
            "Al Jazeera",
            "The Guardian US",
        ]:
            color = color_for_source(name)
            self.assertNotEqual(color, "#6b7280", f"{name} should have a color")


if __name__ == "__main__":
    unittest.main()
