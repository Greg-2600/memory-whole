"""Silence detection: find stories covered by one side but not the other.

A "silence gap" exists when a story has meaningful coverage from left-leaning
sources but zero (or near-zero) coverage from right-leaning sources, or
vice versa.  This is the core insight of Memory Whole — revealing what
each side of the political spectrum chooses *not* to report.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

import db
from utils import LEFT_LEANS, RIGHT_LEANS, lean_for_source


@dataclass
class SilenceGap:
    """A story with asymmetric left/right coverage."""

    story_id: int
    title: str
    status: str
    importance_score: float
    left_sources: list[str] = field(default_factory=list)
    right_sources: list[str] = field(default_factory=list)
    center_sources: list[str] = field(default_factory=list)
    silent_side: str = ""  # "left" or "right"


def detect_silence(
    conn: sqlite3.Connection,
    min_sources_covering: int = 1,
    lookback_days: int = 7,
    reference_date: str | None = None,
) -> list[SilenceGap]:
    """Find stories with significant coverage on one side but silence on the other.

    Returns a list of SilenceGap objects sorted by importance.
    """
    if reference_date is None:
        reference_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Get stories with recent activity
    stories = conn.execute(
        """SELECT s.id, s.representative_title, s.status, s.importance_score
           FROM stories s
           WHERE s.last_seen >= date(?, ? || ' days')
             AND s.status IN ('active', 'fading')
           ORDER BY s.importance_score DESC""",
        (reference_date, f"-{lookback_days}"),
    ).fetchall()

    gaps: list[SilenceGap] = []

    for story in stories:
        sid = story["id"]
        headlines = db.get_story_headlines(conn, sid)

        left: list[str] = []
        right: list[str] = []
        center: list[str] = []

        seen_sources: set[str] = set()
        for h in headlines:
            src = h["source"]
            if src in seen_sources:
                continue
            seen_sources.add(src)
            lean = lean_for_source(src)
            if lean in LEFT_LEANS:
                left.append(src)
            elif lean in RIGHT_LEANS:
                right.append(src)
            else:
                center.append(src)

        # Check for silence: one side has coverage >= threshold, other has zero
        gap = SilenceGap(
            story_id=sid,
            title=story["representative_title"],
            status=story["status"],
            importance_score=float(story["importance_score"]),
            left_sources=left,
            right_sources=right,
            center_sources=center,
        )

        if len(left) >= min_sources_covering and len(right) == 0:
            gap.silent_side = "right"
            gaps.append(gap)
        elif len(right) >= min_sources_covering and len(left) == 0:
            gap.silent_side = "left"
            gaps.append(gap)

    # Sort: highest importance first
    gaps.sort(key=lambda g: g.importance_score, reverse=True)
    return gaps
