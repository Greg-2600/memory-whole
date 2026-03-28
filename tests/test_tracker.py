"""Unit tests for the tracker module."""

import sqlite3
import unittest

import db
import tracker


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


def _insert_headlines(conn: sqlite3.Connection, items: list[dict]) -> None:
    for item in items:
        db.upsert_headline(conn, **item)
    conn.commit()


class TestTrackStories(unittest.TestCase):
    """Tests for the tracking pipeline."""

    def test_single_headline_becomes_singleton_story(self) -> None:
        conn = _make_conn()
        _insert_headlines(
            conn,
            [
                {
                    "url": "http://example.com/solo",
                    "title": "Solo Headline",
                    "source": "Source A",
                    "published_at": "2026-03-27",
                    "first_seen": "2026-03-27",
                    "last_seen": "2026-03-27",
                    "summary": "Just one headline.",
                },
            ],
        )
        tracker.track_stories(conn)
        self.assertEqual(db.story_count(conn), 1)
        hid = conn.execute("SELECT id FROM headlines").fetchone()["id"]
        self.assertIsNotNone(db.get_story_for_headline(conn, hid))
        conn.close()

    @unittest.skipUnless(tracker._HAS_SKLEARN, "scikit-learn not installed")
    def test_similar_headlines_cluster_together(self) -> None:
        conn = _make_conn()
        _insert_headlines(
            conn,
            [
                {
                    "url": f"http://example.com/{i}",
                    "title": title,
                    "source": source,
                    "published_at": "2026-03-27",
                    "first_seen": "2026-03-27",
                    "last_seen": "2026-03-27",
                    "summary": title,
                }
                for i, (title, source) in enumerate(
                    [
                        ("Massive earthquake hits California coast", "CNN"),
                        ("California rocked by major earthquake", "BBC"),
                        ("Earthquake devastates California region", "NPR"),
                        ("Stock market rallies on tech earnings", "CNBC"),
                        ("Tech earnings drive stock market surge", "Fox"),
                    ]
                )
            ],
        )
        tracker.track_stories(conn)
        # Should have fewer stories than headlines due to clustering
        stories = db.story_count(conn)
        self.assertGreater(stories, 0)
        self.assertLess(stories, 5)
        conn.close()

    def test_track_stories_no_headlines(self) -> None:
        conn = _make_conn()
        # Should not raise when DB is empty
        tracker.track_stories(conn)
        self.assertEqual(db.story_count(conn), 0)
        conn.close()

    def test_track_stories_idempotent(self) -> None:
        conn = _make_conn()
        _insert_headlines(
            conn,
            [
                {
                    "url": "http://example.com/idem",
                    "title": "Idempotent Test",
                    "source": "A",
                    "published_at": "2026-03-27",
                    "first_seen": "2026-03-27",
                    "last_seen": "2026-03-27",
                    "summary": "",
                },
            ],
        )
        tracker.track_stories(conn)
        count1 = db.story_count(conn)
        tracker.track_stories(conn)
        count2 = db.story_count(conn)
        self.assertEqual(count1, count2)
        conn.close()

    def test_track_stories_returns_event_list(self) -> None:
        conn = _make_conn()
        _insert_headlines(
            conn,
            [
                {
                    "url": "http://example.com/ev1",
                    "title": "Event return test",
                    "source": "A",
                    "published_at": "2026-03-27",
                    "first_seen": "2026-03-27",
                    "last_seen": "2026-03-27",
                    "summary": "",
                },
            ],
        )
        events = tracker.track_stories(conn)
        self.assertIsInstance(events, list)
        conn.close()

    def test_track_stories_empty_db_returns_empty_list(self) -> None:
        conn = _make_conn()
        events = tracker.track_stories(conn)
        self.assertIsInstance(events, list)
        self.assertEqual(events, [])
        conn.close()

    def test_story_event_dataclass(self) -> None:
        event = tracker.StoryEvent(
            event_type="merge",
            survivor_id=1,
            survivor_title="Test Story",
            absorbed_ids=[2, 3],
            absorbed_titles=["Story B", "Story C"],
        )
        self.assertEqual(event.event_type, "merge")
        self.assertEqual(event.survivor_id, 1)
        self.assertEqual(len(event.absorbed_ids), 2)


if __name__ == "__main__":
    unittest.main()
