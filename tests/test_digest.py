"""Tests for daily digest generation."""

import sqlite3
from unittest.mock import patch

import db
from alerts import init_alert_tables
from digest import _build_digest_text, run_digest


def _setup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    init_alert_tables(conn)
    return conn


def _populate_stories(conn):
    """Add a few stories for digest testing."""
    sid1 = db.create_story(
        conn,
        slug="active-story",
        title="Active Story About Politics",
        first_seen="2025-01-14",
        last_seen="2025-01-15",
    )
    conn.execute(
        "UPDATE stories SET status = 'active', importance_score = 10.0, peak_source_count = 4 WHERE id = ?",
        (sid1,),
    )

    # Add some headlines for source counting
    for i, source in enumerate(["CNN Top Stories", "FOX News Latest", "NPR News"]):
        db.upsert_headline(
            conn,
            url=f"http://example.com/{i}",
            title=f"Active Story Headline {i}",
            source=source,
            published_at="2025-01-15",
            first_seen="2025-01-15",
            last_seen="2025-01-15",
        )
        conn.execute(
            "UPDATE headlines SET story_id = ? WHERE url = ?",
            (sid1, f"http://example.com/{i}"),
        )

    sid2 = db.create_story(
        conn,
        slug="gone-story",
        title="Disappeared Story Nobody Talks About",
        first_seen="2025-01-01",
        last_seen="2025-01-05",
    )
    conn.execute(
        "UPDATE stories SET status = 'gone', importance_score = 8.0, peak_source_count = 5 WHERE id = ?",
        (sid2,),
    )
    conn.commit()
    return sid1, sid2


class TestBuildDigestText:
    def test_empty_db(self):
        conn = _setup_db()
        text = _build_digest_text(conn, {})
        assert "Memory Mountain" in text
        assert "Digest" in text

    def test_includes_top_stories(self):
        conn = _setup_db()
        _populate_stories(conn)
        text = _build_digest_text(conn, {})
        assert "Active Story" in text
        assert "TOP STORIES" in text

    def test_includes_disappeared(self):
        conn = _setup_db()
        _populate_stories(conn)
        text = _build_digest_text(conn, {})
        assert "DISAPPEARED" in text
        assert "Disappeared Story" in text

    def test_includes_silence_gaps(self):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = _setup_db()
        sid = db.create_story(
            conn,
            slug="left-only-digest",
            title="Left Only Digest Story",
            first_seen=today,
            last_seen=today,
        )
        conn.execute(
            "UPDATE stories SET status = 'active', importance_score = 5.0 WHERE id = ?",
            (sid,),
        )
        # Only left sources
        for i, source in enumerate(
            ["CNN Top Stories", "MSNBC Top Stories", "NPR News"]
        ):
            db.upsert_headline(
                conn,
                url=f"http://silence-test.com/{i}",
                title=f"Silence headline {i}",
                source=source,
                published_at=today,
                first_seen=today,
                last_seen=today,
            )
            conn.execute(
                "UPDATE headlines SET story_id = ? WHERE url = ?",
                (sid, f"http://silence-test.com/{i}"),
            )
        conn.commit()

        config = {
            "silence": {"enabled": True, "min_sources_covering": 2, "lookback_days": 30}
        }
        text = _build_digest_text(conn, config)
        assert "SILENCE" in text


class TestRunDigest:
    def test_disabled_returns_false(self):
        conn = _setup_db()
        result = run_digest(conn, {"digest": {"enabled": False}})
        assert result is False

    def test_no_config_returns_false(self):
        conn = _setup_db()
        result = run_digest(conn, {})
        assert result is False

    def test_writes_to_file(self, tmp_path):
        conn = _setup_db()
        _populate_stories(conn)
        config = {"digest": {"enabled": True}}
        result = run_digest(conn, config, output_dir=str(tmp_path))
        assert result is True

        # Check that a digest file was written
        files = list(tmp_path.glob("digest-*.txt"))
        assert len(files) == 1
        content = files[0].read_text()
        assert "Memory Mountain" in content

    @patch("digest.send_ntfy", return_value=True)
    def test_sends_ntfy(self, mock_ntfy, tmp_path):
        conn = _setup_db()
        _populate_stories(conn)
        config = {
            "digest": {
                "enabled": True,
                "ntfy_topic": "test-digest",
            }
        }
        run_digest(conn, config, output_dir=str(tmp_path))
        mock_ntfy.assert_called_once()
        # Check topic was passed
        assert mock_ntfy.call_args[0][0] == "test-digest"
