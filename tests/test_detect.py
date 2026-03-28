import unittest
import datetime as dt
from pathlib import Path

from rss_reader import detect_important_clusters, write_calendar_html, TfidfVectorizer


@unittest.skipIf(TfidfVectorizer is None, "scikit-learn not installed")
class TestDetectImportant(unittest.TestCase):
    def test_detect_clusters_and_write_calendar(self, tmp_path: Path | None = None):
        if tmp_path is None:
            tmp_path = Path("./.tmp_test_output")
        # three items about the same event from different feeds/dates
        items = [
            {
                "title": "Big Fire in Downtown",
                "summary": "A large fire broke out in downtown area, multiple crews respond.",
                "link": "http://a.example/1",
                "_feed_name": "Feed A",
                "published_parsed": (2026, 2, 18, 12, 0, 0, 0, 0, 0),
            },
            {
                "title": "Downtown blaze spreads",
                "summary": "Fire spreads through two blocks, evacuations underway.",
                "link": "http://b.example/2",
                "_feed_name": "Feed B",
                "published_parsed": (2026, 2, 18, 13, 0, 0, 0, 0, 0),
            },
            {
                "title": "Aftermath of downtown fire",
                "summary": "Fire contained; investigations continue.",
                "link": "http://c.example/3",
                "_feed_name": "Feed C",
                "published_parsed": (2026, 2, 19, 9, 0, 0, 0, 0, 0),
            },
        ]

        clusters = detect_important_clusters(items, min_cluster_size=2)
        self.assertTrue(isinstance(clusters, list))
        self.assertTrue(len(clusters) >= 1)
        # Expect a cluster with mentions >=2 and multiple sources
        c = clusters[0]
        self.assertGreaterEqual(c.get("mentions", 0), 2)
        self.assertGreaterEqual(len(c.get("sources", [])), 2)

        # write calendar to tmp path
        outdir = tmp_path / "calendar_out"
        write_calendar_html(clusters, outdir, title="Test Calendar")
        self.assertTrue((outdir / "calendar.html").exists())


if __name__ == "__main__":
    unittest.main()
