"""Unit tests for the db module."""

import sqlite3
import unittest

import db


class TestDBConnection(unittest.TestCase):
    """Tests for connect and init_db."""

    def test_init_db_creates_tables(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertIn("headlines", tables)
        self.assertIn("stories", tables)
        self.assertIn("daily_snapshots", tables)
        conn.close()

    def test_init_db_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        db.init_db(conn)
        db.init_db(conn)  # should not raise
        conn.close()


def _make_conn() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with schema initialized."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


class TestHeadlines(unittest.TestCase):
    """Tests for headline CRUD operations."""

    def test_upsert_headline_insert(self) -> None:
        conn = _make_conn()
        db.upsert_headline(
            conn,
            url="http://example.com/1",
            title="Test Headline",
            source="Source A",
            published_at="2026-03-27",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
            summary="A summary.",
        )
        conn.commit()
        self.assertEqual(db.headline_count(conn), 1)
        conn.close()

    def test_upsert_headline_updates_last_seen(self) -> None:
        conn = _make_conn()
        db.upsert_headline(
            conn,
            url="http://example.com/1",
            title="Headline",
            source="Source A",
            published_at="2026-03-27",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        # Re-upsert with later last_seen
        db.upsert_headline(
            conn,
            url="http://example.com/1",
            title="Headline",
            source="Source A",
            published_at="2026-03-27",
            first_seen="2026-03-27",
            last_seen="2026-03-28",
        )
        conn.commit()
        self.assertEqual(db.headline_count(conn), 1)
        row = conn.execute("SELECT last_seen FROM headlines").fetchone()
        self.assertEqual(row["last_seen"], "2026-03-28")
        conn.close()

    def test_upsert_keeps_longer_title(self) -> None:
        conn = _make_conn()
        db.upsert_headline(
            conn,
            url="http://example.com/1",
            title="Short",
            source="A",
            published_at=None,
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        db.upsert_headline(
            conn,
            url="http://example.com/1",
            title="A much longer title wins",
            source="A",
            published_at=None,
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        conn.commit()
        row = conn.execute("SELECT title FROM headlines").fetchone()
        self.assertEqual(row["title"], "A much longer title wins")
        conn.close()

    def test_get_recent_headlines(self) -> None:
        conn = _make_conn()
        db.upsert_headline(
            conn,
            url="http://example.com/1",
            title="Recent",
            source="A",
            published_at="2026-03-27",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        conn.commit()
        rows = db.get_recent_headlines(conn, days=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Recent")
        conn.close()


class TestStories(unittest.TestCase):
    """Tests for story CRUD and lifecycle."""

    def test_create_story(self) -> None:
        conn = _make_conn()
        sid = db.create_story(
            conn,
            slug="test-story",
            title="Test Story",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        conn.commit()
        self.assertIsInstance(sid, int)
        self.assertEqual(db.story_count(conn), 1)
        conn.close()

    def test_create_story_unique_slug(self) -> None:
        conn = _make_conn()
        sid1 = db.create_story(
            conn,
            slug="duplicate",
            title="First",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        sid2 = db.create_story(
            conn,
            slug="duplicate",
            title="Second",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        conn.commit()
        self.assertNotEqual(sid1, sid2)
        self.assertEqual(db.story_count(conn), 2)
        conn.close()

    def test_assign_headline_to_story(self) -> None:
        conn = _make_conn()
        db.upsert_headline(
            conn,
            url="http://example.com/1",
            title="Headline",
            source="A",
            published_at="2026-03-27",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        hid = conn.execute("SELECT id FROM headlines").fetchone()["id"]
        sid = db.create_story(
            conn,
            slug="story",
            title="Story",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        db.assign_headline_to_story(conn, hid, sid)
        conn.commit()
        self.assertEqual(db.get_story_for_headline(conn, hid), sid)
        conn.close()

    def test_merge_stories(self) -> None:
        conn = _make_conn()
        s1 = db.create_story(
            conn,
            slug="story-a",
            title="A",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        s2 = db.create_story(
            conn,
            slug="story-b",
            title="B",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        # Assign a headline to s2
        db.upsert_headline(
            conn,
            url="http://example.com/merge",
            title="Merge me",
            source="A",
            published_at="2026-03-27",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        hid = conn.execute("SELECT id FROM headlines").fetchone()["id"]
        db.assign_headline_to_story(conn, hid, s2)
        conn.commit()

        survivor = db.merge_stories(conn, [s1, s2])
        conn.commit()
        self.assertEqual(survivor, min(s1, s2))
        # Headline should now point to survivor
        self.assertEqual(db.get_story_for_headline(conn, hid), survivor)
        # Only one story should remain
        self.assertEqual(db.story_count(conn), 1)
        conn.close()

    def test_merge_stories_empty_raises(self) -> None:
        conn = _make_conn()
        with self.assertRaises(ValueError):
            db.merge_stories(conn, [])
        conn.close()

    def test_update_statuses(self) -> None:
        conn = _make_conn()
        # Active story: last_seen = today
        db.create_story(
            conn,
            slug="active",
            title="Active Story",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        # Gone story: last_seen = 10 days ago
        db.create_story(
            conn,
            slug="gone",
            title="Gone Story",
            first_seen="2026-03-10",
            last_seen="2026-03-15",
        )
        conn.commit()

        db.update_statuses(conn, today="2026-03-27")
        conn.commit()

        active = conn.execute(
            "SELECT status FROM stories WHERE slug = 'active'"
        ).fetchone()
        gone = conn.execute("SELECT status FROM stories WHERE slug = 'gone'").fetchone()
        self.assertEqual(active["status"], "active")
        self.assertEqual(gone["status"], "gone")
        conn.close()

    def test_refresh_story_deletes_orphan(self) -> None:
        conn = _make_conn()
        sid = db.create_story(
            conn,
            slug="orphan",
            title="Orphan",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        conn.commit()
        # No headlines assigned — refresh should delete
        db.refresh_story(conn, sid)
        conn.commit()
        self.assertEqual(db.story_count(conn), 0)
        conn.close()

    def test_refresh_story_updates_metadata(self) -> None:
        conn = _make_conn()
        sid = db.create_story(
            conn,
            slug="refresh-me",
            title="Old Title",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        db.upsert_headline(
            conn,
            url="http://example.com/r1",
            title="Short",
            source="A",
            published_at="2026-03-26",
            first_seen="2026-03-26",
            last_seen="2026-03-26",
        )
        db.upsert_headline(
            conn,
            url="http://example.com/r2",
            title="A much longer headline title here",
            source="B",
            published_at="2026-03-27",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        hids = [r["id"] for r in conn.execute("SELECT id FROM headlines").fetchall()]
        for hid in hids:
            db.assign_headline_to_story(conn, hid, sid)
        conn.commit()

        db.refresh_story(conn, sid)
        conn.commit()

        row = conn.execute(
            "SELECT representative_title, importance_score FROM stories WHERE id = ?",
            (sid,),
        ).fetchone()
        # Longest title should be picked as representative
        self.assertEqual(
            row["representative_title"], "A much longer headline title here"
        )
        self.assertGreater(row["importance_score"], 0)
        conn.close()

    def test_refresh_story_caps_persistence_boost(self) -> None:
        conn = _make_conn()
        recent_sid = db.create_story(
            conn,
            slug="recent",
            title="Recent Story",
            first_seen="2026-06-15",
            last_seen="2026-06-15",
        )
        old_sid = db.create_story(
            conn,
            slug="old",
            title="Old Story",
            first_seen="2025-01-01",
            last_seen="2026-06-15",
        )

        for i in range(5):
            db.upsert_headline(
                conn,
                url=f"http://example.com/recent-{i}",
                title=f"Recent headline {i}",
                source=f"S{i}",
                published_at="2026-06-15",
                first_seen="2026-06-15",
                last_seen="2026-06-15",
            )
        for i, hid in enumerate(
            [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM headlines ORDER BY url"
                ).fetchall()
            ][:5]
        ):
            db.assign_headline_to_story(conn, hid, recent_sid)

        for i in range(5):
            db.upsert_headline(
                conn,
                url=f"http://example.com/old-{i}",
                title=f"Old headline {i}",
                source=f"T{i}",
                published_at="2026-06-15",
                first_seen="2026-06-15",
                last_seen="2026-06-15",
            )
        for hid in [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM headlines WHERE url LIKE 'http://example.com/old-%' ORDER BY url"
            ).fetchall()
        ]:
            db.assign_headline_to_story(conn, hid, old_sid)

        conn.commit()

        db.refresh_story(conn, recent_sid)
        db.refresh_story(conn, old_sid)
        conn.commit()

        recent_score = conn.execute(
            "SELECT importance_score FROM stories WHERE id = ?",
            (recent_sid,),
        ).fetchone()["importance_score"]
        old_score = conn.execute(
            "SELECT importance_score FROM stories WHERE id = ?",
            (old_sid,),
        ).fetchone()["importance_score"]

        self.assertGreater(recent_score, 0)
        self.assertGreater(old_score, 0)
        self.assertLess(old_score / recent_score, 5)

        conn.close()

    def test_refresh_story_prefers_recent_activity(self) -> None:
        conn = _make_conn()
        old_sid = db.create_story(
            conn,
            slug="old-story",
            title="Old Story",
            first_seen="2025-01-01",
            last_seen="2026-06-15",
        )
        new_sid = db.create_story(
            conn,
            slug="new-story",
            title="New Story",
            first_seen="2026-06-10",
            last_seen="2026-06-15",
        )

        for i in range(10):
            db.upsert_headline(
                conn,
                url=f"http://example.com/old-{i}",
                title=f"Old headline {i}",
                source=f"S{i}",
                published_at="2025-01-01",
                first_seen="2025-01-01",
                last_seen="2025-01-01",
            )
        for i in range(1):
            db.upsert_headline(
                conn,
                url=f"http://example.com/old-recent-{i}",
                title=f"Old recent headline {i}",
                source="S0",
                published_at="2026-06-15",
                first_seen="2026-06-15",
                last_seen="2026-06-15",
            )

        for i in range(11):
            db.upsert_headline(
                conn,
                url=f"http://example.com/new-{i}",
                title=f"New headline {i}",
                source=f"T{i}",
                published_at="2026-06-15",
                first_seen="2026-06-15",
                last_seen="2026-06-15",
            )

        old_hids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM headlines WHERE url LIKE 'http://example.com/old-%' ORDER BY url"
            ).fetchall()
        ]
        new_hids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM headlines WHERE url LIKE 'http://example.com/new-%' ORDER BY url"
            ).fetchall()
        ]
        for hid in old_hids:
            db.assign_headline_to_story(conn, hid, old_sid)
        for hid in new_hids:
            db.assign_headline_to_story(conn, hid, new_sid)

        conn.commit()

        db.refresh_story(conn, old_sid)
        db.refresh_story(conn, new_sid)
        conn.commit()

        old_score = conn.execute(
            "SELECT importance_score FROM stories WHERE id = ?",
            (old_sid,),
        ).fetchone()["importance_score"]
        new_score = conn.execute(
            "SELECT importance_score FROM stories WHERE id = ?",
            (new_sid,),
        ).fetchone()["importance_score"]

        self.assertGreater(new_score, old_score)
        conn.close()


class TestQueryHelpers(unittest.TestCase):
    """Tests for query functions."""

    def test_get_top_stories_and_all_stories(self) -> None:
        conn = _make_conn()
        sid = db.create_story(
            conn,
            slug="top",
            title="Top",
            first_seen="2026-03-27",
            last_seen="2026-03-27",
        )
        conn.execute(
            "UPDATE stories SET importance_score = 100, status = 'active' WHERE id = ?",
            (sid,),
        )
        conn.commit()

        top = db.get_top_stories(conn)
        self.assertTrue(len(top) >= 1)
        self.assertEqual(top[0]["representative_title"], "Top")

        all_s = db.get_all_stories(conn)
        self.assertTrue(len(all_s) >= 1)
        conn.close()

    def test_get_disappeared_stories(self) -> None:
        conn = _make_conn()
        sid = db.create_story(
            conn,
            slug="vanished",
            title="Vanished",
            first_seen="2026-03-20",
            last_seen="2026-03-22",
        )
        conn.execute(
            "UPDATE stories SET status = 'gone', peak_source_count = 5 WHERE id = ?",
            (sid,),
        )
        conn.commit()

        disappeared = db.get_disappeared_stories(conn)
        self.assertTrue(len(disappeared) >= 1)
        conn.close()

    def test_headline_and_story_counts(self) -> None:
        conn = _make_conn()
        self.assertEqual(db.headline_count(conn), 0)
        self.assertEqual(db.story_count(conn), 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
