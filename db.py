"""SQLite database layer for Memory Whole.

Stores every headline ever fetched, groups them into *stories* that persist
across days, and records daily snapshots so coverage can be tracked over time.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any  # noqa: F401  # used in type hints

SCHEMA = """\
CREATE TABLE IF NOT EXISTS headlines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT    UNIQUE NOT NULL,
    title       TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    published_at TEXT,
    first_seen  TEXT    NOT NULL,
    last_seen   TEXT    NOT NULL,
    summary     TEXT    DEFAULT '',
    story_id    INTEGER REFERENCES stories(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS stories (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    slug                 TEXT    UNIQUE NOT NULL,
    representative_title TEXT    NOT NULL,
    first_seen           TEXT    NOT NULL,
    last_seen            TEXT    NOT NULL,
    peak_date            TEXT,
    peak_source_count    INTEGER DEFAULT 0,
    status               TEXT    DEFAULT 'active'
                                 CHECK(status IN ('active','fading','gone')),
    importance_score     REAL    DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    story_id       INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    date           TEXT    NOT NULL,
    source_count   INTEGER DEFAULT 0,
    headline_count INTEGER DEFAULT 0,
    PRIMARY KEY (story_id, date)
);

CREATE INDEX IF NOT EXISTS idx_hl_source   ON headlines(source);
CREATE INDEX IF NOT EXISTS idx_hl_last     ON headlines(last_seen);
CREATE INDEX IF NOT EXISTS idx_hl_story    ON headlines(story_id);
CREATE INDEX IF NOT EXISTS idx_st_status   ON stories(status);
CREATE INDEX IF NOT EXISTS idx_st_score    ON stories(importance_score DESC);
CREATE INDEX IF NOT EXISTS idx_snap_date   ON daily_snapshots(date);
"""


# ------------------------------------------------------------------
# Connection helpers
# ------------------------------------------------------------------


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with WAL mode and foreign keys."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables and indexes if they don't exist."""
    conn.executescript(SCHEMA)
    conn.commit()


# ------------------------------------------------------------------
# Headlines
# ------------------------------------------------------------------


def upsert_headline(
    conn: sqlite3.Connection,
    *,
    url: str,
    title: str,
    source: str,
    published_at: str | None,
    first_seen: str,
    last_seen: str,
    summary: str = "",
) -> None:
    """Insert a headline or update ``last_seen`` if it already exists."""
    conn.execute(
        """INSERT INTO headlines
               (url, title, source, published_at, first_seen, last_seen, summary)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(url) DO UPDATE SET
               last_seen = MAX(headlines.last_seen, excluded.last_seen),
               first_seen = MIN(headlines.first_seen, excluded.first_seen),
               title = CASE WHEN length(excluded.title) > length(headlines.title)
                            THEN excluded.title ELSE headlines.title END
        """,
        (url, title, source, published_at, first_seen, last_seen, summary),
    )


def get_recent_headlines(conn: sqlite3.Connection, days: int = 14) -> list[sqlite3.Row]:
    """Headlines seen within the last *days* days."""
    anchor_row = conn.execute(
        "SELECT MAX(last_seen) AS anchor FROM headlines"
    ).fetchone()
    anchor = anchor_row["anchor"] if anchor_row and anchor_row["anchor"] else "now"
    return conn.execute(
        """SELECT id, url, title, source, published_at,
                  first_seen, last_seen, summary, story_id
           FROM headlines
           WHERE last_seen >= date(?, ? || ' days')
           ORDER BY last_seen DESC""",
        (anchor, f"-{days}"),
    ).fetchall()


def get_story_for_headline(conn: sqlite3.Connection, headline_id: int) -> int | None:
    row = conn.execute(
        "SELECT story_id FROM headlines WHERE id = ?", (headline_id,)
    ).fetchone()
    return row["story_id"] if row and row["story_id"] else None


def assign_headline_to_story(
    conn: sqlite3.Connection, headline_id: int, story_id: int
) -> None:
    conn.execute(
        "UPDATE headlines SET story_id = ? WHERE id = ?",
        (story_id, headline_id),
    )


def get_story_headlines(conn: sqlite3.Connection, story_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT id, url, title, source, published_at,
                  first_seen, last_seen, summary
           FROM headlines WHERE story_id = ?
           ORDER BY last_seen DESC, source""",
        (story_id,),
    ).fetchall()


# ------------------------------------------------------------------
# Stories
# ------------------------------------------------------------------


def create_story(
    conn: sqlite3.Connection,
    *,
    slug: str,
    title: str,
    first_seen: str,
    last_seen: str,
) -> int:
    """Create a new story and return its id."""
    # Ensure unique slug by appending numeric suffix if needed
    base_slug = slug[:60]
    candidate = base_slug
    n = 1
    while True:
        existing = conn.execute(
            "SELECT 1 FROM stories WHERE slug = ?", (candidate,)
        ).fetchone()
        if not existing:
            break
        candidate = f"{base_slug}-{n}"
        n += 1
    cur = conn.execute(
        """INSERT INTO stories
               (slug, representative_title, first_seen, last_seen)
           VALUES (?, ?, ?, ?)""",
        (candidate, title, first_seen, last_seen),
    )
    return cur.lastrowid  # type: ignore[return-value]


def merge_stories(conn: sqlite3.Connection, story_ids: list[int]) -> int:
    """Merge multiple stories into one (lowest id wins). Return survivor id."""
    if not story_ids:
        raise ValueError("story_ids must not be empty")
    keep = min(story_ids)
    others = [s for s in story_ids if s != keep]
    if others:
        for sid in others:
            conn.execute(
                "UPDATE headlines SET story_id = ? WHERE story_id = ?",
                (keep, sid),
            )
            conn.execute(
                "DELETE FROM daily_snapshots WHERE story_id = ?",
                (sid,),
            )
            conn.execute("DELETE FROM stories WHERE id = ?", (sid,))
    return keep


def refresh_story(conn: sqlite3.Connection, story_id: int) -> None:
    """Recalculate metadata for a single story from its headlines."""
    rows = conn.execute(
        """SELECT title, source, published_at, first_seen, last_seen
           FROM headlines WHERE story_id = ?""",
        (story_id,),
    ).fetchall()

    if not rows:
        conn.execute("DELETE FROM stories WHERE id = ?", (story_id,))
        return

    sources = {r["source"] for r in rows}
    first = min(r["first_seen"] for r in rows)
    # Use published_at as canonical last_seen so old stories don't appear
    # active just because they were re-fetched today.
    last = max(r["published_at"] or r["last_seen"] for r in rows)
    rep_title = max(rows, key=lambda r: len(r["title"]))["title"]
    mentions = len(rows)
    source_count = len(sources)

    days_active = max(1, _date_diff(last, first) + 1)
    velocity = mentions / days_active
    base = source_count * (1.0 + math.log1p(mentions))
    persistence = 1.0 + days_active / 7.0
    vel_factor = 1.0 + min(3.0, velocity) / 3.0
    score = base * persistence * vel_factor

    conn.execute(
        """UPDATE stories SET
               representative_title = ?,
               first_seen = ?,
               last_seen = ?,
               importance_score = ?
           WHERE id = ?""",
        (rep_title, first, last, score, story_id),
    )


# ------------------------------------------------------------------
# Daily snapshots & statuses
# ------------------------------------------------------------------


def update_daily_snapshots(conn: sqlite3.Connection, today: str | None = None) -> None:
    """Create or update today's snapshot for every story."""
    if today is None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    story_ids = [r["id"] for r in conn.execute("SELECT id FROM stories").fetchall()]
    for sid in story_ids:
        row = conn.execute(
            """SELECT COUNT(DISTINCT source) AS sc, COUNT(*) AS hc
               FROM headlines
               WHERE story_id = ? AND last_seen = ?""",
            (sid, today),
        ).fetchone()
        if row and row["hc"] > 0:
            conn.execute(
                """INSERT INTO daily_snapshots
                       (story_id, date, source_count, headline_count)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(story_id, date) DO UPDATE SET
                       source_count = excluded.source_count,
                       headline_count = excluded.headline_count""",
                (sid, today, row["sc"], row["hc"]),
            )
        # Update peak info
        peak = conn.execute(
            """SELECT date, source_count FROM daily_snapshots
               WHERE story_id = ?
               ORDER BY source_count DESC, date DESC LIMIT 1""",
            (sid,),
        ).fetchone()
        if peak:
            conn.execute(
                "UPDATE stories SET peak_date = ?, peak_source_count = ? WHERE id = ?",
                (peak["date"], peak["source_count"], sid),
            )


def backfill_daily_snapshots(conn: sqlite3.Connection) -> int:
    """Rebuild daily_snapshots from headline published_at data.

    For each story, creates snapshot rows for every distinct published date
    that any of its headlines have. Uses published_at (immutable) rather than
    last_seen (which gets updated on re-fetch) so historical timelines remain
    accurate.

    Returns the number of snapshot rows written.
    """
    conn.execute("DELETE FROM daily_snapshots")

    # Use published_at as the canonical date (falls back to first_seen)
    rows = conn.execute("""SELECT h.story_id,
                  COALESCE(h.published_at, h.first_seen) AS day,
                  COUNT(DISTINCT h.source) AS sc, COUNT(*) AS hc
           FROM headlines h
           WHERE h.story_id IS NOT NULL
             AND COALESCE(h.published_at, h.first_seen) IS NOT NULL
           GROUP BY h.story_id, day""").fetchall()

    count = 0
    for r in rows:
        conn.execute(
            """INSERT INTO daily_snapshots
                   (story_id, date, source_count, headline_count)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(story_id, date) DO UPDATE SET
                   source_count = MAX(daily_snapshots.source_count, excluded.source_count),
                   headline_count = MAX(daily_snapshots.headline_count, excluded.headline_count)""",
            (r["story_id"], r["day"], r["sc"], r["hc"]),
        )
        count += 1

    # Update peak info and recalculate first_seen / last_seen for all stories
    # so that old stories aren't all marked active after a bulk re-fetch.
    for s in conn.execute("SELECT id FROM stories").fetchall():
        peak = conn.execute(
            """SELECT date, source_count FROM daily_snapshots
               WHERE story_id = ?
               ORDER BY source_count DESC, date DESC LIMIT 1""",
            (s["id"],),
        ).fetchone()
        dates = conn.execute(
            """SELECT MIN(COALESCE(h.published_at, h.first_seen)) AS fs,
                      MAX(COALESCE(h.published_at, h.first_seen)) AS ls
               FROM headlines h WHERE h.story_id = ?""",
            (s["id"],),
        ).fetchone()
        updates: dict[str, Any] = {}
        if peak:
            updates["peak_date"] = peak["date"]
            updates["peak_source_count"] = peak["source_count"]
        if dates and dates["fs"]:
            updates["first_seen"] = dates["fs"]
        if dates and dates["ls"]:
            updates["last_seen"] = dates["ls"]
        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE stories SET {sets} WHERE id = ?",  # nosec B608 – sets is built from internal column-name keys only
                (*updates.values(), s["id"]),
            )

    conn.commit()
    return count


def update_statuses(
    conn: sqlite3.Connection,
    today: str | None = None,
    gone_days: int = 4,
) -> None:
    """Mark stories as active / fading / gone based on last_seen."""
    if today is None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn.execute(
        "UPDATE stories SET status = 'active' WHERE last_seen >= date(?, '-1 day')",
        (today,),
    )
    conn.execute(
        """UPDATE stories SET status = 'fading'
           WHERE last_seen < date(?, '-1 day')
             AND last_seen >= date(?, ? || ' days')""",
        (today, today, f"-{gone_days}"),
    )
    conn.execute(
        "UPDATE stories SET status = 'gone' WHERE last_seen < date(?, ? || ' days')",
        (today, f"-{gone_days}"),
    )


# ------------------------------------------------------------------
# Query helpers
# ------------------------------------------------------------------

_STORY_SELECT = """\
SELECT s.*,
       (SELECT COUNT(DISTINCT h.source)
        FROM headlines h WHERE h.story_id = s.id) AS source_count,
       (SELECT COUNT(*)
        FROM headlines h WHERE h.story_id = s.id) AS mention_count
FROM stories s"""


def get_top_stories(conn: sqlite3.Connection, limit: int = 30) -> list[sqlite3.Row]:
    return conn.execute(
        f"""{_STORY_SELECT}
            WHERE s.status IN ('active', 'fading')
            ORDER BY s.importance_score DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def get_disappeared_stories(
    conn: sqlite3.Connection, min_peak_sources: int = 1
) -> list[sqlite3.Row]:
    return conn.execute(
        f"""{_STORY_SELECT}
            WHERE s.peak_source_count >= ?
              AND s.status IN ('fading', 'gone')
            ORDER BY s.peak_source_count DESC,
                     s.importance_score DESC""",
        (min_peak_sources,),
    ).fetchall()


def get_all_stories(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(f"{_STORY_SELECT} ORDER BY s.importance_score DESC").fetchall()


def get_story_snapshots(
    conn: sqlite3.Connection, story_id: int, days: int = 30
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT date, source_count, headline_count
           FROM daily_snapshots
           WHERE story_id = ? AND date >= date('now', ? || ' days')
           ORDER BY date""",
        (story_id, f"-{days}"),
    ).fetchall()


def get_source_story_matrix(
    conn: sqlite3.Connection, days: int = 14
) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT h.source, s.id AS story_id,
                  s.representative_title, COUNT(*) AS cnt
           FROM headlines h
           JOIN stories s ON h.story_id = s.id
           WHERE h.last_seen >= date('now', ? || ' days')
           GROUP BY h.source, s.id
           ORDER BY s.importance_score DESC""",
        (f"-{days}",),
    ).fetchall()


def headline_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM headlines").fetchone()
    return row["n"] if row else 0


def story_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM stories").fetchone()
    return row["n"] if row else 0


def get_story_source_names(conn: sqlite3.Connection, story_id: int) -> list[str]:
    """Return distinct source names for a story, ordered alphabetically."""
    rows = conn.execute(
        "SELECT DISTINCT source FROM headlines WHERE story_id = ? ORDER BY source",
        (story_id,),
    ).fetchall()
    return [r["source"] for r in rows]


# ------------------------------------------------------------------
# Internals
# ------------------------------------------------------------------


def _date_diff(a: str, b: str) -> int:
    """Days between two ISO date strings (a − b)."""
    return (date.fromisoformat(a) - date.fromisoformat(b)).days
