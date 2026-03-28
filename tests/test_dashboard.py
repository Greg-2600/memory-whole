"""Unit tests for the dashboard module."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

import db
import dashboard


def _make_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


def _seed_data(conn: sqlite3.Connection) -> None:
    """Insert realistic seed data: multiple headlines across two stories."""
    for i, (url, title, source, pub) in enumerate(
        [
            ("http://a.com/1", "Big earthquake hits coast", "CNN", "2026-03-26"),
            ("http://b.com/1", "Major earthquake on the coast", "BBC", "2026-03-26"),
            ("http://c.com/1", "Earthquake devastates coastal towns", "NPR", "2026-03-27"),
            ("http://d.com/1", "Stock market rallies on earnings", "CNBC", "2026-03-27"),
            ("http://e.com/1", "Tech earnings drive market surge", "Fox", "2026-03-27"),
        ]
    ):
        db.upsert_headline(
            conn,
            url=url,
            title=title,
            source=source,
            published_at=pub,
            first_seen=pub,
            last_seen="2026-03-27",
            summary=f"Summary of: {title}",
        )

    # Create two stories and assign headlines
    s1 = db.create_story(
        conn,
        slug="earthquake",
        title="Earthquake hits coast",
        first_seen="2026-03-26",
        last_seen="2026-03-27",
    )
    s2 = db.create_story(
        conn,
        slug="stock-market",
        title="Stock market rallies",
        first_seen="2026-03-27",
        last_seen="2026-03-27",
    )

    headlines = conn.execute("SELECT id, url FROM headlines ORDER BY id").fetchall()
    for h in headlines[:3]:
        db.assign_headline_to_story(conn, h["id"], s1)
    for h in headlines[3:]:
        db.assign_headline_to_story(conn, h["id"], s2)

    # Refresh story metadata
    db.refresh_story(conn, s1)
    db.refresh_story(conn, s2)

    # Create snapshots
    db.update_daily_snapshots(conn, today="2026-03-27")

    # Set statuses
    conn.execute(
        "UPDATE stories SET status = 'active' WHERE id = ?", (s1,)
    )
    conn.execute(
        "UPDATE stories SET status = 'active' WHERE id = ?", (s2,)
    )
    conn.commit()


class TestDashboardGenerate(unittest.TestCase):
    """Tests that dashboard.generate produces output files."""

    def test_generates_index_html(self) -> None:
        conn = _make_conn()
        _seed_data(conn)

        with tempfile.TemporaryDirectory() as tmpdir:
            outdir = Path(tmpdir)
            dashboard.generate(conn, outdir)

            index = outdir / "index.html"
            self.assertTrue(index.exists(), "index.html should be generated")

            content = index.read_text(encoding="utf-8")
            self.assertIn("Memory Mountain", content)
            self.assertIn("Top Stories", content)
            self.assertIn("Disappeared", content)

        conn.close()

    def test_generates_story_detail_pages(self) -> None:
        conn = _make_conn()
        _seed_data(conn)

        with tempfile.TemporaryDirectory() as tmpdir:
            outdir = Path(tmpdir)
            dashboard.generate(conn, outdir)

            # Should have at least one story_*.html file
            story_files = list(outdir.glob("story_*.html"))
            self.assertTrue(
                len(story_files) >= 1,
                "At least one story detail page should be generated",
            )

            # Verify detail page content
            content = story_files[0].read_text(encoding="utf-8")
            self.assertIn("<html", content)

        conn.close()

    def test_empty_db_still_generates(self) -> None:
        conn = _make_conn()

        with tempfile.TemporaryDirectory() as tmpdir:
            outdir = Path(tmpdir)
            dashboard.generate(conn, outdir)

            index = outdir / "index.html"
            self.assertTrue(index.exists())

        conn.close()


if __name__ == "__main__":
    unittest.main()
