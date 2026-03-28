"""Disappearance alerts: notify when significant stories vanish from all feeds.

Supports two notification channels:
  1. ntfy.sh — free, open-source push notifications (https://ntfy.sh)
  2. Webhook — generic JSON POST to any URL

Alerts fire when a story that previously reached a peak source count above
the configured threshold transitions to 'gone' status.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any

import db

log = logging.getLogger(__name__)

# Internal table to track which stories we already alerted on
_ALERT_SCHEMA = """\
CREATE TABLE IF NOT EXISTS alerted_stories (
    story_id   INTEGER PRIMARY KEY REFERENCES stories(id) ON DELETE CASCADE,
    alerted_at TEXT NOT NULL
);
"""


@dataclass
class DisappearanceEvent:
    """A story that just transitioned to 'gone'."""

    story_id: int
    title: str
    peak_source_count: int
    importance_score: float
    first_seen: str
    last_seen: str


def init_alert_tables(conn: sqlite3.Connection) -> None:
    """Create the alerted_stories table if it doesn't exist."""
    conn.executescript(_ALERT_SCHEMA)
    conn.commit()


def find_new_disappearances(
    conn: sqlite3.Connection,
    min_peak_sources: int = 3,
) -> list[DisappearanceEvent]:
    """Find stories that are now 'gone' but haven't been alerted yet.

    Only returns stories whose peak_source_count meets the threshold.
    """
    init_alert_tables(conn)

    rows = conn.execute(
        """SELECT s.id, s.representative_title, s.peak_source_count,
                  s.importance_score, s.first_seen, s.last_seen
           FROM stories s
           WHERE s.status = 'gone'
             AND s.peak_source_count >= ?
             AND s.id NOT IN (SELECT story_id FROM alerted_stories)
           ORDER BY s.peak_source_count DESC, s.importance_score DESC""",
        (min_peak_sources,),
    ).fetchall()

    return [
        DisappearanceEvent(
            story_id=r["id"],
            title=r["representative_title"],
            peak_source_count=r["peak_source_count"],
            importance_score=float(r["importance_score"]),
            first_seen=r["first_seen"],
            last_seen=r["last_seen"],
        )
        for r in rows
    ]


def mark_alerted(conn: sqlite3.Connection, story_ids: list[int]) -> None:
    """Record that these stories have been alerted so we don't re-send."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for sid in story_ids:
        conn.execute(
            "INSERT OR IGNORE INTO alerted_stories (story_id, alerted_at) VALUES (?, ?)",
            (sid, now),
        )
    conn.commit()


def send_ntfy(topic: str, title: str, body: str, priority: str = "default") -> bool:
    """Send a push notification via ntfy.sh. Returns True on success."""
    if not topic:
        return False
    url = f"https://ntfy.sh/{topic}"
    data = body.encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": "newspaper,warning",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception as exc:
        log.warning("ntfy send failed: %s", exc)
        return False


def send_webhook(webhook_url: str, payload: dict[str, Any]) -> bool:
    """POST JSON to a webhook URL. Returns True on success."""
    if not webhook_url:
        return False
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        log.warning("webhook send failed: %s", exc)
        return False


def run_alerts(conn: sqlite3.Connection, config: dict[str, Any]) -> int:
    """Check for new disappearances and send notifications.

    Returns the number of alerts sent.
    """
    alert_cfg = config.get("alerts", {})
    if not alert_cfg.get("enabled", False):
        return 0

    min_peak = int(alert_cfg.get("min_peak_sources", 3))
    ntfy_topic = str(alert_cfg.get("ntfy_topic", "")).strip()
    webhook_url = str(alert_cfg.get("webhook_url", "")).strip()

    events = find_new_disappearances(conn, min_peak_sources=min_peak)
    if not events:
        return 0

    sent = 0
    alerted_ids: list[int] = []

    for event in events:
        title = f"Story Disappeared: {event.title[:80]}"
        body = (
            f"Peak: {event.peak_source_count} sources\n"
            f"Active: {event.first_seen} → {event.last_seen}\n"
            f"Score: {event.importance_score:.1f}"
        )

        ok = False
        if ntfy_topic:
            ok = send_ntfy(ntfy_topic, title, body, priority="high") or ok
        if webhook_url:
            payload = {
                "event": "story_disappeared",
                "story_id": event.story_id,
                "title": event.title,
                "peak_source_count": event.peak_source_count,
                "importance_score": event.importance_score,
                "first_seen": event.first_seen,
                "last_seen": event.last_seen,
            }
            ok = send_webhook(webhook_url, payload) or ok

        if ok:
            sent += 1
        alerted_ids.append(event.story_id)

    # Mark all as alerted even if delivery failed to avoid spam
    mark_alerted(conn, alerted_ids)
    log.info("Sent %d disappearance alerts (%d events)", sent, len(events))
    return sent
