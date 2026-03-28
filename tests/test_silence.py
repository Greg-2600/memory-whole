"""Tests for silence detection."""

import sqlite3

import db
from silence import SilenceGap, detect_silence


def _setup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


def _insert_story(conn, slug, title, first_seen="2025-01-10", last_seen="2025-01-15"):
    return db.create_story(
        conn, slug=slug, title=title, first_seen=first_seen, last_seen=last_seen
    )


def _insert_headline(conn, url, title, source, story_id, last_seen="2025-01-15"):
    db.upsert_headline(
        conn,
        url=url,
        title=title,
        source=source,
        published_at=last_seen,
        first_seen=last_seen,
        last_seen=last_seen,
    )
    conn.execute("UPDATE headlines SET story_id = ? WHERE url = ?", (story_id, url))
    conn.commit()


class TestSilenceDetection:
    """Test silence gap detection."""

    def test_no_stories_returns_empty(self):
        conn = _setup_db()
        gaps = detect_silence(conn, lookback_days=30, reference_date="2025-01-15")
        assert gaps == []

    def test_balanced_coverage_no_gap(self):
        """Story covered by both left and right → no silence gap."""
        conn = _setup_db()
        sid = _insert_story(conn, "balanced", "Balanced Story")
        _insert_headline(conn, "http://a.com/1", "Story A", "CNN Top Stories", sid)
        _insert_headline(conn, "http://b.com/1", "Story B", "FOX News Latest", sid)
        _insert_headline(conn, "http://c.com/1", "Story C", "MSNBC Top Stories", sid)
        _insert_headline(conn, "http://d.com/1", "Story D", "Breitbart", sid)

        # Refresh to set status
        db.refresh_story(conn, sid)
        conn.execute("UPDATE stories SET status = 'active' WHERE id = ?", (sid,))
        conn.commit()

        gaps = detect_silence(
            conn, min_sources_covering=2, lookback_days=30, reference_date="2025-01-15"
        )
        assert len(gaps) == 0

    def test_right_silent(self):
        """Story covered by left sources only → right silent."""
        conn = _setup_db()
        sid = _insert_story(conn, "left-only", "Left-Only Story")
        _insert_headline(conn, "http://a.com/1", "Story", "CNN Top Stories", sid)
        _insert_headline(conn, "http://b.com/1", "Story", "MSNBC Top Stories", sid)
        _insert_headline(conn, "http://c.com/1", "Story", "NPR News", sid)

        db.refresh_story(conn, sid)
        conn.execute("UPDATE stories SET status = 'active' WHERE id = ?", (sid,))
        conn.commit()

        gaps = detect_silence(
            conn, min_sources_covering=2, lookback_days=30, reference_date="2025-01-15"
        )
        assert len(gaps) == 1
        assert gaps[0].silent_side == "right"
        assert (
            "CNN Top Stories" in gaps[0].left_sources
            or "MSNBC Top Stories" in gaps[0].left_sources
        )

    def test_left_silent(self):
        """Story covered by right sources only → left silent."""
        conn = _setup_db()
        sid = _insert_story(conn, "right-only", "Right-Only Story")
        _insert_headline(conn, "http://a.com/1", "Story", "FOX News Latest", sid)
        _insert_headline(conn, "http://b.com/1", "Story", "Breitbart", sid)
        _insert_headline(conn, "http://c.com/1", "Story", "Daily Wire", sid)

        db.refresh_story(conn, sid)
        conn.execute("UPDATE stories SET status = 'active' WHERE id = ?", (sid,))
        conn.commit()

        gaps = detect_silence(
            conn, min_sources_covering=2, lookback_days=30, reference_date="2025-01-15"
        )
        assert len(gaps) == 1
        assert gaps[0].silent_side == "left"

    def test_min_sources_threshold(self):
        """One source on left isn't enough to flag right as silent."""
        conn = _setup_db()
        sid = _insert_story(conn, "thin-left", "Thin Left Coverage")
        _insert_headline(conn, "http://a.com/1", "Story", "CNN Top Stories", sid)

        db.refresh_story(conn, sid)
        conn.execute("UPDATE stories SET status = 'active' WHERE id = ?", (sid,))
        conn.commit()

        gaps = detect_silence(
            conn, min_sources_covering=2, lookback_days=30, reference_date="2025-01-15"
        )
        assert len(gaps) == 0

    def test_center_only_no_gap(self):
        """Story covered only by center sources → no gap flagged."""
        conn = _setup_db()
        sid = _insert_story(conn, "center-only", "Center Story")
        _insert_headline(conn, "http://a.com/1", "Story", "AP News", sid)
        _insert_headline(conn, "http://b.com/1", "Story", "ABC News", sid)
        _insert_headline(conn, "http://c.com/1", "Story", "The Hill", sid)

        db.refresh_story(conn, sid)
        conn.execute("UPDATE stories SET status = 'active' WHERE id = ?", (sid,))
        conn.commit()

        gaps = detect_silence(
            conn, min_sources_covering=2, lookback_days=30, reference_date="2025-01-15"
        )
        assert len(gaps) == 0

    def test_silence_gap_is_dataclass(self):
        gap = SilenceGap(
            story_id=1,
            title="Test",
            status="active",
            importance_score=5.0,
            left_sources=["CNN"],
            right_sources=[],
            silent_side="right",
        )
        assert gap.story_id == 1
        assert gap.silent_side == "right"

    def test_multiple_gaps_sorted_by_importance(self):
        """Multiple silence gaps should come back sorted by importance."""
        conn = _setup_db()

        # Low importance story
        sid1 = _insert_story(conn, "low-imp", "Low Importance")
        _insert_headline(conn, "http://a.com/1", "Story", "CNN Top Stories", sid1)
        _insert_headline(conn, "http://b.com/1", "Story", "NPR News", sid1)
        db.refresh_story(conn, sid1)
        conn.execute("UPDATE stories SET status = 'active' WHERE id = ?", (sid1,))

        # High importance story (more headlines → higher score)
        sid2 = _insert_story(conn, "high-imp", "High Importance")
        for i in range(5):
            _insert_headline(
                conn, f"http://x{i}.com/1", f"Story {i}", "FOX News Latest", sid2
            )
        _insert_headline(conn, "http://y.com/1", "Story Y", "Breitbart", sid2)
        _insert_headline(conn, "http://z.com/1", "Story Z", "Daily Wire", sid2)
        db.refresh_story(conn, sid2)
        conn.execute("UPDATE stories SET status = 'active' WHERE id = ?", (sid2,))
        conn.commit()

        gaps = detect_silence(
            conn, min_sources_covering=2, lookback_days=30, reference_date="2025-01-15"
        )
        assert len(gaps) == 2
        # Higher importance should come first
        assert gaps[0].importance_score >= gaps[1].importance_score
