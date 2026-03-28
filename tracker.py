"""Story tracking: cluster headlines and manage story lifecycle.

Each run re-clusters recent headlines using TF-IDF + DBSCAN, maps clusters
to existing stories (or creates new ones), and updates daily snapshots so
we can detect rising and disappearing stories.

Also tracks merge/split events when stories converge or diverge between runs.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from typing import Any

import db
from utils import slugify

log = logging.getLogger(__name__)

try:
    from sklearn.cluster import DBSCAN
    from sklearn.decomposition import TruncatedSVD
    from sklearn.feature_extraction.text import TfidfVectorizer

    _HAS_SKLEARN = True
except ImportError:
    _HAS_SKLEARN = False


@dataclass
class StoryEvent:
    """A merge or split event detected during tracking."""

    event_type: str  # "merge" or "split"
    survivor_id: int
    survivor_title: str
    absorbed_ids: list[int] = field(default_factory=list)
    absorbed_titles: list[str] = field(default_factory=list)


def track_stories(
    conn: sqlite3.Connection,
    cluster_days: int = 14,
) -> list[StoryEvent]:
    """Main tracking routine: cluster → match → refresh → snapshot.

    Returns a list of merge/split events detected during this run.
    """
    events: list[StoryEvent] = []
    headlines = db.get_recent_headlines(conn, days=cluster_days)
    if len(headlines) < 2:
        # Not enough data to cluster — assign singletons as own stories
        for h in headlines:
            if not db.get_story_for_headline(conn, h["id"]):
                sid = db.create_story(
                    conn,
                    slug=slugify(h["title"])[:60],
                    title=h["title"],
                    first_seen=h["first_seen"],
                    last_seen=h["last_seen"],
                )
                db.assign_headline_to_story(conn, h["id"], sid)
        conn.commit()
        return events

    clusters = _cluster_headlines(headlines)

    for cluster_items in clusters:
        hids = [h["id"] for h in cluster_items]

        # Which stories do these headlines already belong to?
        existing: set[int] = set()
        for hid in hids:
            sid = db.get_story_for_headline(conn, hid)
            if sid:
                existing.add(sid)

        if not existing:
            # Brand-new story
            rep = max(cluster_items, key=lambda h: len(h["title"]))
            first = min(h["first_seen"] for h in cluster_items)
            last = max(h["last_seen"] for h in cluster_items)
            story_id = db.create_story(
                conn,
                slug=slugify(rep["title"])[:60],
                title=rep["title"],
                first_seen=first,
                last_seen=last,
            )
        elif len(existing) == 1:
            story_id = existing.pop()
        else:
            # Multiple existing stories belong to same cluster → merge
            # Record the merge event before it happens
            sorted_ids = sorted(existing)
            survivor = sorted_ids[0]
            absorbed = sorted_ids[1:]

            # Look up titles for the absorbed stories
            absorbed_titles = []
            for aid in absorbed:
                row = conn.execute(
                    "SELECT representative_title FROM stories WHERE id = ?",
                    (aid,),
                ).fetchone()
                if row:
                    absorbed_titles.append(row["representative_title"])

            survivor_row = conn.execute(
                "SELECT representative_title FROM stories WHERE id = ?",
                (survivor,),
            ).fetchone()
            survivor_title = (
                survivor_row["representative_title"] if survivor_row else "Story"
            )

            events.append(
                StoryEvent(
                    event_type="merge",
                    survivor_id=survivor,
                    survivor_title=survivor_title,
                    absorbed_ids=absorbed,
                    absorbed_titles=absorbed_titles,
                )
            )
            log.info(
                "Merging stories %s into %d (%s)",
                absorbed,
                survivor,
                survivor_title,
            )

            story_id = db.merge_stories(conn, list(existing))

        for hid in hids:
            db.assign_headline_to_story(conn, hid, story_id)

    # Refresh metadata for every story
    for row in conn.execute("SELECT id FROM stories").fetchall():
        db.refresh_story(conn, row["id"])

    db.update_daily_snapshots(conn)
    db.update_statuses(conn)
    conn.commit()
    return events


# ------------------------------------------------------------------
# Clustering
# ------------------------------------------------------------------


def _cluster_headlines(
    headlines: list[Any],
) -> list[list[Any]]:
    """Cluster headlines by text similarity.

    Returns a list of clusters (each a list of headline rows).
    Noise items (singletons) are each returned as their own 1-element cluster
    so every headline ends up in a story.
    """
    if not _HAS_SKLEARN:
        raise RuntimeError(
            "scikit-learn is required for story tracking "
            "(pip install scikit-learn numpy)"
        )

    docs: list[str] = []
    for h in headlines:
        text = (h["title"] or "") + " " + (h["summary"] or "")
        docs.append(text)

    vec = TfidfVectorizer(max_features=2000, stop_words="english")
    X = vec.fit_transform(docs)

    # Optional SVD for dimensionality reduction
    n_comp = min(100, max(1, X.shape[1] - 1), max(1, X.shape[0] - 1))
    if n_comp > 1:
        try:
            svd = TruncatedSVD(n_components=n_comp)
            Xr = svd.fit_transform(X)
        except Exception:
            Xr = X.toarray()
    else:
        Xr = X.toarray()

    scanner = DBSCAN(eps=0.45, min_samples=2, metric="cosine")
    labels = scanner.fit_predict(Xr)

    buckets: dict[int, list[Any]] = {}
    noise: list[list[Any]] = []
    for i, lbl in enumerate(labels):
        if lbl == -1:
            noise.append([headlines[i]])
        else:
            buckets.setdefault(int(lbl), []).append(headlines[i])

    return list(buckets.values()) + noise
