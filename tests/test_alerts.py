"""Tests for disappearance alerts."""

import sqlite3
from unittest.mock import patch, MagicMock

import db
from alerts import (
    DisappearanceEvent,
    find_new_disappearances,
    init_alert_tables,
    mark_alerted,
    run_alerts,
    send_ntfy,
    send_webhook,
)


def _setup_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    init_alert_tables(conn)
    return conn


def _insert_gone_story(conn, slug, title, peak=5):
    sid = db.create_story(
        conn, slug=slug, title=title, first_seen="2025-01-01", last_seen="2025-01-05"
    )
    conn.execute(
        "UPDATE stories SET status = 'gone', peak_source_count = ? WHERE id = ?",
        (peak, sid),
    )
    conn.commit()
    return sid


class TestFindDisappearances:
    def test_empty_db(self):
        conn = _setup_db()
        events = find_new_disappearances(conn, min_peak_sources=2)
        assert events == []

    def test_finds_gone_stories_above_threshold(self):
        conn = _setup_db()
        _insert_gone_story(conn, "big-story", "Big Story", peak=5)
        _insert_gone_story(conn, "small-story", "Small Story", peak=1)

        events = find_new_disappearances(conn, min_peak_sources=3)
        assert len(events) == 1
        assert events[0].title == "Big Story"

    def test_does_not_realert(self):
        conn = _setup_db()
        sid = _insert_gone_story(conn, "repeat", "Repeat Story", peak=5)
        events = find_new_disappearances(conn, min_peak_sources=3)
        assert len(events) == 1

        mark_alerted(conn, [sid])

        events2 = find_new_disappearances(conn, min_peak_sources=3)
        assert len(events2) == 0


class TestMarkAlerted:
    def test_marks_stories(self):
        conn = _setup_db()
        sid = _insert_gone_story(conn, "test", "Test", peak=4)
        mark_alerted(conn, [sid])

        row = conn.execute(
            "SELECT * FROM alerted_stories WHERE story_id = ?", (sid,)
        ).fetchone()
        assert row is not None

    def test_idempotent(self):
        conn = _setup_db()
        sid = _insert_gone_story(conn, "test", "Test", peak=4)
        mark_alerted(conn, [sid])
        mark_alerted(conn, [sid])  # should not raise

        count = conn.execute(
            "SELECT COUNT(*) AS n FROM alerted_stories WHERE story_id = ?", (sid,)
        ).fetchone()["n"]
        assert count == 1


class TestSendNtfy:
    @patch("alerts.urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: mock_resp
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        assert send_ntfy("test-topic", "Title", "Body") is True
        mock_urlopen.assert_called_once()

    def test_empty_topic_returns_false(self):
        assert send_ntfy("", "Title", "Body") is False

    @patch("alerts.urllib.request.urlopen", side_effect=Exception("network error"))
    def test_failure_returns_false(self, mock_urlopen):
        assert send_ntfy("topic", "Title", "Body") is False


class TestSendWebhook:
    @patch("alerts.urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: mock_resp
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        assert send_webhook("https://example.com/hook", {"key": "val"}) is True

    def test_empty_url_returns_false(self):
        assert send_webhook("", {"key": "val"}) is False


class TestRunAlerts:
    def test_disabled_config(self):
        conn = _setup_db()
        result = run_alerts(conn, {"alerts": {"enabled": False}})
        assert result == 0

    def test_no_alerts_section(self):
        conn = _setup_db()
        result = run_alerts(conn, {})
        assert result == 0

    @patch("alerts.send_ntfy", return_value=True)
    def test_sends_alerts_on_disappearance(self, mock_ntfy):
        conn = _setup_db()
        _insert_gone_story(conn, "vanished", "Vanished Story", peak=5)

        config = {
            "alerts": {
                "enabled": True,
                "ntfy_topic": "test-topic",
                "min_peak_sources": 3,
            }
        }
        count = run_alerts(conn, config)
        assert count == 1
        mock_ntfy.assert_called_once()

    @patch("alerts.send_ntfy", return_value=True)
    def test_does_not_resend(self, mock_ntfy):
        conn = _setup_db()
        _insert_gone_story(conn, "vanished", "Vanished Story", peak=5)

        config = {
            "alerts": {
                "enabled": True,
                "ntfy_topic": "test-topic",
                "min_peak_sources": 3,
            }
        }
        run_alerts(conn, config)
        mock_ntfy.reset_mock()

        # Second run should find nothing new
        count = run_alerts(conn, config)
        assert count == 0
        mock_ntfy.assert_not_called()


class TestDisappearanceEvent:
    def test_dataclass(self):
        e = DisappearanceEvent(
            story_id=1,
            title="Test",
            peak_source_count=5,
            importance_score=10.0,
            first_seen="2025-01-01",
            last_seen="2025-01-05",
        )
        assert e.story_id == 1
        assert e.peak_source_count == 5
