"""Framing analysis for Memory Whole.

Detects sentiment divergence across political leans for each story.
Uses VADER (rule-based) sentiment on headlines grouped by lean, then
flags stories where left-leaning and right-leaning outlets frame the
same event with significantly different tone.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from statistics import mean

from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

import db
from utils import LEFT_LEANS, RIGHT_LEANS, lean_for_source

_analyzer = SentimentIntensityAnalyzer()


@dataclass
class FramingDivergence:
    """A story where left and right frame it very differently."""

    story_id: int
    title: str
    importance_score: float
    left_sentiment: float  # mean compound score [-1, 1]
    right_sentiment: float
    center_sentiment: float
    divergence: float  # abs(left - right)
    left_headlines: list[tuple[str, str, float]] = field(
        default_factory=list
    )  # (source, title, score)
    right_headlines: list[tuple[str, str, float]] = field(default_factory=list)


def analyze_framing(
    conn: sqlite3.Connection,
    *,
    min_sources: int = 3,
    min_divergence: float = 0.25,
    lookback_days: int = 14,
    reference_date: str | None = None,
) -> list[FramingDivergence]:
    """Find stories with significant sentiment divergence between left and right.

    Args:
        conn: Database connection.
        min_sources: Minimum total sources for a story to be analyzed.
        min_divergence: Minimum abs(left_compound - right_compound) to flag.
        lookback_days: Only consider stories active within this window.
        reference_date: ISO date string for "today" (default: actual today).

    Returns:
        List of FramingDivergence, sorted by divergence descending.
    """
    # Get stories with enough coverage in the lookback window
    stories = conn.execute(
        """SELECT s.id, s.representative_title, s.importance_score,
                  (SELECT COUNT(DISTINCT h.source)
                   FROM headlines h
                   WHERE h.story_id = s.id) AS source_count
           FROM stories s
           WHERE source_count >= ?
             AND s.last_seen >= date(COALESCE(?, 'now'), ?)
           ORDER BY s.importance_score DESC""",
        (min_sources, reference_date, f"-{lookback_days} days"),
    ).fetchall()

    results: list[FramingDivergence] = []

    for story in stories:
        sid = story["id"]
        headlines = db.get_story_headlines(conn, sid)

        left_scores: list[float] = []
        right_scores: list[float] = []
        center_scores: list[float] = []
        left_hl: list[tuple[str, str, float]] = []
        right_hl: list[tuple[str, str, float]] = []

        for hl in headlines:
            title = hl["title"] or ""
            source = hl["source"] or ""
            if not title:
                continue

            compound = _analyzer.polarity_scores(title)["compound"]
            lean = lean_for_source(source)

            if lean in LEFT_LEANS:
                left_scores.append(compound)
                left_hl.append((source, title, compound))
            elif lean in RIGHT_LEANS:
                right_scores.append(compound)
                right_hl.append((source, title, compound))
            else:
                center_scores.append(compound)

        # Need headlines from both sides to compare
        if not left_scores or not right_scores:
            continue

        left_avg = mean(left_scores)
        right_avg = mean(right_scores)
        center_avg = mean(center_scores) if center_scores else 0.0
        div = abs(left_avg - right_avg)

        if div >= min_divergence:
            results.append(
                FramingDivergence(
                    story_id=sid,
                    title=story["representative_title"] or "Story",
                    importance_score=float(story["importance_score"]),
                    left_sentiment=round(left_avg, 3),
                    right_sentiment=round(right_avg, 3),
                    center_sentiment=round(center_avg, 3),
                    divergence=round(div, 3),
                    left_headlines=sorted(left_hl, key=lambda x: x[2]),
                    right_headlines=sorted(right_hl, key=lambda x: x[2]),
                )
            )

    results.sort(key=lambda r: r.divergence, reverse=True)
    return results
