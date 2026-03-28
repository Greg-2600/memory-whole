import unittest
from pathlib import Path
import datetime as dt

from rss_reader import load_entries_from_markdown, detect_important_clusters, write_calendar_html, TfidfVectorizer


@unittest.skipIf(TfidfVectorizer is None, "scikit-learn not installed")
class TestFromMarkdown(unittest.TestCase):
    def test_aggregate_and_calendar(self, tmp_path: Path | None = None):
        if tmp_path is None:
            tmp_path = Path("./.tmp_markdown")
        out = tmp_path / "output"
        out.mkdir(parents=True, exist_ok=True)
        # create two daily markdown files with multiple stories
        d1 = out / "daily-news-2026-02-18.md"
        d1.write_text(
            """# Daily RSS Digest - 2026-02-18

## Big Fire in Downtown

- Published: 2026-02-18 12:00:00 UTC
- Source: Feed A
- Link: http://a.example/1

A large fire broke out in downtown area.

## Small Event

- Published: 2026-02-18 14:00:00 UTC
- Source: Feed D
- Link: http://d.example/4

A small event.
""",
            encoding="utf-8",
        )
        d2 = out / "daily-news-2026-02-19.md"
        d2.write_text(
            """# Daily RSS Digest - 2026-02-19

## Downtown blaze spreads

- Published: 2026-02-19 09:00:00 UTC
- Source: Feed B
- Link: http://b.example/2

Fire spreads through two blocks.

## Aftermath of downtown fire

- Published: 2026-02-19 10:00:00 UTC
- Source: Feed C
- Link: http://c.example/3

Fire contained; investigations continue.
""",
            encoding="utf-8",
        )

        entries = load_entries_from_markdown(out)
        self.assertTrue(len(entries) >= 4)

        clusters = detect_important_clusters(entries, min_cluster_size=2)
        self.assertTrue(len(clusters) >= 1)

        # write a full-range calendar into output
        write_calendar_html(clusters, out, title="Full Calendar", window_days=None)
        self.assertTrue((out / "calendar.html").exists())


if __name__ == "__main__":
    unittest.main()
