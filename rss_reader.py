"""RSS reader / Memory Whole CLI.

Main entry point: fetches RSS feeds, stores headlines in a SQLite database,
clusters them into stories, tracks story lifecycles (active → fading → gone),
and generates an HTML dashboard.

Legacy sub-commands (--backpopulate, --detect-important, --from-markdown) are
still supported for backward compatibility.
"""

# pylint: disable=too-many-locals

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import re
from pathlib import Path
from typing import Any

# New pipeline modules
import feedparser
import yaml

import alerts
import dashboard
import db as db_mod
import digest
import fetcher
import tracker
from utils import (
    DEFAULT_USER_AGENT,
    favicon_for_source,
    first_non_empty,
    item_datetime,
    slugify,
    summarize_text,
)

# Optional heavy deps for legacy clustering path
try:
    from sklearn.cluster import DBSCAN as _dbscan_cls
    from sklearn.decomposition import TruncatedSVD as _truncated_svd_cls
    from sklearn.feature_extraction.text import TfidfVectorizer as _tfidf_vectorizer_cls
except ImportError:  # pragma: no cover - optional import
    _tfidf_vectorizer_cls = None
    _truncated_svd_cls = None
    _dbscan_cls = None

# Backward-compatible exports used by legacy tests/callers.
# pylint: disable=invalid-name
TfidfVectorizer = _tfidf_vectorizer_cls
TruncatedSVD = _truncated_svd_cls
DBSCAN = _dbscan_cls
# pylint: enable=invalid-name


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Memory Whole — track news headlines over time.",
    )
    parser.add_argument(
        "--config",
        default="feeds.yaml",
        help="Path to YAML config file (default: feeds.yaml)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Override output directory from config",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=None,
        help="Override max items per feed from config",
    )

    # --- New pipeline flags ---
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="Fetch feeds into the database but skip tracking and dashboard",
    )
    parser.add_argument(
        "--dashboard-only",
        action="store_true",
        help="Regenerate dashboard from existing database (no fetch)",
    )
    parser.add_argument(
        "--import-markdown",
        action="store_true",
        help="Import existing Markdown digest files into the database",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print a summary of story statuses and exit",
    )

    # --- Legacy flags (still work, print deprecation notice) ---
    parser.add_argument(
        "--summary-sentences",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--summary-words",
        type=int,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--backpopulate",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--detect-important",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--from-markdown",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--show-only-multi",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    """Load configuration from YAML and apply defaults.

    Raises FileNotFoundError when the path does not exist and ValueError when
    the `feeds` list is missing or empty.
    """
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}

    if "feeds" not in data or not isinstance(data["feeds"], list) or not data["feeds"]:
        raise ValueError("Config must contain a non-empty 'feeds' list")

    data.setdefault("settings", {})
    data["settings"].setdefault("output_dir", "output")
    data["settings"].setdefault("max_items_per_feed", 25)
    data["settings"].setdefault("merge_all_sources", True)
    data["settings"].setdefault("merged_filename", "daily-news-{date}.md")
    data["settings"].setdefault("daily_title", "Daily RSS Digest - {date}")
    data["settings"].setdefault("write_individual_feeds", False)
    data["settings"].setdefault("summary_max_sentences", 6)
    data["settings"].setdefault("summary_max_words", 300)
    # Allow feeds to be fetched with a configurable User-Agent (helps bypass simple bot blocks)
    data["settings"].setdefault("user_agent", DEFAULT_USER_AGENT)
    return data


def markdown_for_feed(
    feed_name: str,
    feed_url: str,
    entries: list[dict[str, Any]],
    max_sentences: int = 2,
    max_words: int = 80,
) -> str:
    """Render a list of feed `entries` into a Markdown string.

    The output includes a title, source, generation timestamp and a section
    for each item with published time, source, link and a short summary.
    """
    lines: list[str] = []
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines.append(f"# {feed_name}")
    lines.append("")
    lines.append(f"Source: {feed_url}")
    lines.append(f"Generated: {now}")
    lines.append("")

    if not entries:
        lines.append("No items found.")
        lines.append("")
        return "\n".join(lines)

    for entry in entries:
        title = html.unescape(first_non_empty(entry.get("title"), "Untitled"))
        link = first_non_empty(entry.get("link"), "")
        content_value = ""
        if isinstance(entry.get("content"), list) and entry.get("content"):
            first_content = entry["content"][0]
            if isinstance(first_content, dict):
                content_value = first_non_empty(first_content.get("value"), "")

        raw_text = first_non_empty(
            content_value,
            entry.get("summary"),
            entry.get("description"),
            "",
        )
        summary = summarize_text(
            raw_text, max_sentences=max_sentences, max_words=max_words
        )
        source = first_non_empty(
            (
                entry.get("source", {}).get("title")
                if isinstance(entry.get("source"), dict)
                else ""
            ),
            feed_name,
        )

        published_dt = item_datetime(entry)
        published = (
            published_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            if published_dt
            else "Unknown"
        )

        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"- Published: {published}")
        lines.append(f"- Source: {source}")
        if link:
            lines.append(f"- Link: {link}")
        lines.append("")
        if summary:
            lines.append(summary)
            lines.append("")

    return "\n".join(lines)


def write_markdown(path: Path, content: str) -> None:
    """Write `content` to `path`, creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")


def backpopulate_daily(
    config: dict[str, Any],
    output_dir: Path,
    max_items: int,
    max_sentences: int,
    max_words: int,
) -> int:
    """Backpopulate one merged daily markdown file per publish date.

    Returns the number of daily files written.
    """
    all_entries: list[dict[str, Any]] = []

    for feed_cfg in config["feeds"]:
        name = str(feed_cfg.get("name", "Unnamed Feed"))
        url = str(feed_cfg.get("url", "")).strip()
        if not url:
            continue

        # Use configured User-Agent to improve chance of successful fetch
        ua = config.get("settings", {}).get("user_agent")
        if ua:
            parsed = feedparser.parse(url, request_headers={"User-Agent": ua})
        else:
            parsed = feedparser.parse(url)
        for item in list(parsed.entries[:max_items]):
            item_copy = dict(item)
            item_copy["_feed_name"] = name
            item_copy["_feed_url"] = url
            all_entries.append(item_copy)

    entries_by_date: dict[str, list[dict[str, Any]]] = {}
    for item in all_entries:
        dt_item = item_datetime(item)
        if dt_item is None:
            continue
        day = dt_item.astimezone(dt.timezone.utc).strftime("%Y-%m-%d")
        entries_by_date.setdefault(day, []).append(item)

    for day, items in entries_by_date.items():
        items.sort(
            key=lambda x: item_datetime(x)
            or dt.datetime.min.replace(tzinfo=dt.timezone.utc),
            reverse=True,
        )
        title = str(
            config.get("settings", {}).get("daily_title", "Daily RSS Digest - {date}")
        ).format(date=day)
        content = markdown_for_feed(
            title, "multiple", items, max_sentences=max_sentences, max_words=max_words
        )
        filename = str(
            config.get("settings", {}).get("merged_filename", "daily-news-{date}.md")
        ).format(date=day)
        write_markdown(output_dir / filename, content)

    return len(entries_by_date)


def detect_important_clusters(
    entries: list[dict[str, Any]], min_cluster_size: int = 2
) -> list[dict[str, Any]]:
    """Cluster similar items across sources and produce scored story groups.

    Uses TF-IDF + optional SVD for dimensionality reduction and DBSCAN to
    cluster similar documents. Returned cluster dicts contain a
    representative title, score, start/end dates and member items.
    """
    if not entries:
        return []

    if _tfidf_vectorizer_cls is None or _dbscan_cls is None:
        raise RuntimeError(
            "scikit-learn is required for detect-important (install scikit-learn, numpy)"
        )

    docs: list[str] = []
    meta: list[dict[str, Any]] = []

    for e in entries:
        title = first_non_empty(e.get("title", ""))
        content_value = ""
        if isinstance(e.get("content"), list) and e.get("content"):
            first_content = e["content"][0]
            if isinstance(first_content, dict):
                content_value = first_non_empty(first_content.get("value"), "")
        raw_text = first_non_empty(
            content_value, e.get("summary"), e.get("description"), ""
        )
        text = (title + " ") + raw_text
        dt_item = item_datetime(e)
        day = dt_item.date() if dt_item else None
        docs.append(text)
        meta.append(
            {
                "date": day,
                "feed": e.get("_feed_name") or e.get("_feed_url") or "",
                "link": first_non_empty(e.get("link", "")),
                "title": title,
            }
        )

    if not any(d.strip() for d in docs):
        return []

    vec = _tfidf_vectorizer_cls(max_features=2000, stop_words="english")
    features = vec.fit_transform(docs)

    # Dimensionality reduction if practical to help clustering
    reduced_features = features
    try:
        if _truncated_svd_cls is not None:
            n_components = min(
                100,
                max(1, features.shape[1] - 1),
                max(1, features.shape[0] - 1),
            )
            if n_components > 1:
                svd = _truncated_svd_cls(n_components=n_components)
                reduced_features = svd.fit_transform(features)
            else:
                reduced_features = features.toarray()
        else:
            reduced_features = features.toarray()
    except (RuntimeError, TypeError, ValueError):
        reduced_features = features.toarray()

    # DBSCAN with cosine metric groups near-duplicate story texts
    # Slightly tighter eps to reduce over-clustering of loosely-related items
    dbscan_model = _dbscan_cls(eps=0.45, min_samples=min_cluster_size, metric="cosine")
    labels = dbscan_model.fit_predict(reduced_features)

    clusters: dict[int, list[int]] = {}
    for i, lbl in enumerate(labels):
        if lbl == -1:
            continue
        clusters.setdefault(int(lbl), []).append(i)

    results: list[dict[str, Any]] = []
    for lbl, idxs in clusters.items():
        sources = set()
        mentions = len(idxs)
        dates = [meta[i]["date"] for i in idxs if meta[i]["date"] is not None]
        start = min(dates) if dates else None
        end = max(dates) if dates else None
        for i in idxs:
            sources.add(meta[i]["feed"])

        # Score: prioritize cross-source signals, scale with mention count,
        # and factor in temporal persistence & velocity so stories that are
        # reported across multiple days or gain mentions rapidly score higher.
        source_count = len(sources)
        persistence_days = (end - start).days + 1 if (start and end) else 1
        velocity = (
            mentions / persistence_days if persistence_days > 0 else float(mentions)
        )
        base = float(source_count) * (1.0 + math.log1p(mentions))
        persistence_factor = 1.0 + (persistence_days / 7.0)
        # Cap velocity influence to avoid noisy spikes dominating
        velocity_factor = 1.0 + min(3.0, velocity) / 3.0
        score = base * persistence_factor * velocity_factor

        # Pick a representative title (longest non-empty title)
        rep_idx = max(idxs, key=lambda i: len((meta[i].get("title") or "")))
        rep_title = meta[rep_idx].get("title") or "Multiple reports"

        items = []
        for i in idxs:
            items.append(
                {
                    "title": meta[i].get("title"),
                    "link": meta[i].get("link"),
                    "feed": meta[i].get("feed"),
                    "date": (
                        meta[i].get("date").isoformat() if meta[i].get("date") else None
                    ),
                }
            )

        results.append(
            {
                "label": lbl,
                "score": score,
                "persistence_days": (end - start).days + 1 if (start and end) else 1,
                "velocity": (
                    mentions / ((end - start).days + 1)
                    if (start and end and (end - start).days >= 0)
                    else float(mentions)
                ),
                "start": start,
                "end": end,
                "rep_title": rep_title,
                "mentions": mentions,
                "sources": list(sources),
                "items": items,
            }
        )

    results.sort(key=lambda r: r["score"], reverse=True)
    # Include singleton (noise) items as their own low-scored clusters so
    # the calendar can show all stories, not only multi-source clusters.
    noise_idxs = [i for i, lbl in enumerate(labels) if lbl == -1]
    for i in noise_idxs:
        sources = {meta[i]["feed"]}
        mentions = 1
        start = meta[i]["date"]
        end = meta[i]["date"]
        score = 0.5 * (1.0 + math.log1p(mentions))
        rep_title = meta[i].get("title") or "Report"
        item = {
            "title": meta[i].get("title"),
            "link": meta[i].get("link"),
            "feed": meta[i].get("feed"),
            "date": meta[i].get("date").isoformat() if meta[i].get("date") else None,
        }
        results.append(
            {
                "label": f"noise-{i}",
                "score": score,
                "persistence_days": 1,
                "velocity": 1.0,
                "start": start,
                "end": end,
                "rep_title": rep_title,
                "mentions": mentions,
                "sources": list(sources),
                "items": [item],
            }
        )

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def write_calendar_html(  # pylint: disable=too-many-nested-blocks
    clusters: list[dict[str, Any]],
    output_dir: Path,
    title: str = "News Calendar",
    window_days: int | None = 30,
) -> None:
    """Write a simple calendar-like HTML that places clusters as events.

    Events span their start..end (single-day if end==start). Visual weight
    correlates with `score`.
    """
    if not clusters:
        return

    # Compute the display window: last `window_days` if provided, else full range
    today = dt.datetime.now(dt.timezone.utc).date()
    if window_days is None:
        # determine range from clusters
        all_dates = [c.get("start") for c in clusters if c.get("start")]
        all_dates += [c.get("end") for c in clusters if c.get("end")]
        all_dates = [d for d in all_dates if d is not None]
        if not all_dates:
            return
        start = min(all_dates)
        end = max(all_dates)
        num_days = (end - start).days + 1
    else:
        num_days = int(window_days)
        start = today - dt.timedelta(days=num_days - 1)
        end = today
    num_weeks = (num_days + 6) // 7
    # Align grid so columns are Monday..Sunday. Compute the Monday before `start`.
    start_monday = start - dt.timedelta(days=start.weekday())

    # Normalize scores for visual scaling
    scores = [c["score"] for c in clusters]
    max_score = max(scores) if scores else 1.0

    # Prepare event files and placement segments split by week
    output_dir.mkdir(parents=True, exist_ok=True)
    event_map: dict[str, str] = {}
    segments: list[dict[str, Any]] = []
    for c in clusters:
        # compute raw start/end dates
        s_raw = c.get("start") or c.get("end") or today
        e_raw = c.get("end") or c.get("start") or today
        if s_raw is None or e_raw is None:
            continue
        # clip to window
        s_clipped = max(start, s_raw)
        e_clipped = min(end, e_raw)
        if s_clipped > e_clipped:
            continue
        s_idx = (s_clipped - start_monday).days
        e_idx = (e_clipped - start_monday).days

        # write per-cluster detail page and remember filename
        label_slug = slugify(str(c.get("label")))
        event_fname = f"calendar_event_{label_slug}.html"
        event_map[label_slug] = event_fname
        event_lines = [
            "<!doctype html>",
            "<html><head><meta charset='utf-8'>",
            f"<title>{html.escape(str(c.get('rep_title') or 'Story'))}</title>",
            "<style>body{font-family:Arial,Helvetica,sans-serif;padding:20px}</style>",
            "</head><body>",
        ]
        event_lines.append(
            f"<h1>{html.escape(str(c.get('rep_title') or 'Story'))}</h1>"
        )
        event_lines.append(
            f"<p>Score: {float(c.get('score',0)):.2f} — Mentions: {int(c.get('mentions',0))}</p>"
        )
        event_lines.append("<ul>")
        for it in c.get("items", []):
            link = it.get("link") or "#"
            it_title = it.get("title") or link
            feed = it.get("feed") or ""
            date = it.get("date") or ""
            event_lines.append(
                f'<li><a href="{html.escape(link)}">{html.escape(it_title)}</a> — {html.escape(feed)} — {html.escape(date)}</li>'
            )
        event_lines.append("</ul>")
        event_lines.append("</body></html>")
        (output_dir / event_fname).write_text("\n".join(event_lines), encoding="utf-8")

        # split across weeks into segments (so an event spanning weeks is drawn per-week)
        day = s_idx
        while day <= e_idx:
            wk = day // 7
            wk_end = min(e_idx, (wk + 1) * 7 - 1)
            start_col = day % 7
            span = wk_end - day + 1
            weight = float(c.get("score", 1.0)) / float(max_score)
            # Cap font scaling to keep event labels readable and avoid overlap
            font_scale = min(1.2, 0.95 + weight * 0.6)
            segments.append(
                {
                    "week": wk,
                    "start_col": start_col,
                    "span": span,
                    "title": c.get("rep_title"),
                    "font_scale": font_scale,
                    "sources": c.get("sources", []),
                    "event_fname": event_fname,
                    "weight": weight,
                    "score": c.get("score"),
                    "label": c.get("label"),
                }
            )
            day = wk_end + 1

    # Build HTML
    # Build table-based calendar to allow multiple rows per week
    html_lines: list[str] = []
    html_lines.append("<!doctype html>")
    html_lines.append(
        "<html><head><meta charset='utf-8'><title>" + html.escape(title) + "</title>"
    )
    html_lines.append("<style>")
    html_lines.append(
        "body{font-family: Arial, Helvetica, sans-serif; padding:12px; font-size:13px}"
    )
    html_lines.append("table.calendar{border-collapse:collapse; width:100%}")
    html_lines.append(
        "table.calendar th, table.calendar td{border:1px solid #eee; padding:6px; vertical-align:top}"
    )
    html_lines.append(
        "table.calendar th{background:#f3f3f3; font-weight:700; text-align:center}"
    )
    html_lines.append(
        ".week-label{width:140px; font-weight:700; text-align:right; padding-right:12px}"
    )
    html_lines.append(
        ".event{color:white; padding:6px 8px; border-radius:6px; display:block; text-decoration:none; margin-bottom:4px; max-height:3.0em; overflow:hidden}"
    )
    html_lines.append(
        ".event img{width:16px; height:16px; vertical-align:middle; margin-right:6px; border-radius:3px}"
    )
    html_lines.append(
        ".legend{display:flex; gap:8px; align-items:center; margin-bottom:8px}"
    )
    html_lines.append(
        ".swatch{width:28px; height:16px; border-radius:4px; display:inline-block; border:1px solid rgba(0,0,0,0.08)}"
    )
    html_lines.append("</style></head><body>")
    html_lines.append(f"<h1>{html.escape(title)}</h1>")
    # legend with numeric range and a toggle to show only multi-source stories
    lo, hi = min(scores) if scores else 0.0, max(scores) if scores else 1.0
    html_lines.append("<div class='legend'>")
    html_lines.append(
        f"<div style='display:flex;flex-direction:column'><span style='font-weight:600'>Importance</span><small>{lo:.2f} — {hi:.2f}</small></div>"
    )
    html_lines.append("<div class='swatch' style='background:hsl(240,85%,45%)'></div>")
    html_lines.append("<div class='swatch' style='background:hsl(180,85%,45%)'></div>")
    html_lines.append("<div class='swatch' style='background:hsl(60,85%,45%)'></div>")
    html_lines.append("<div class='swatch' style='background:hsl(0,85%,45%)'></div>")
    html_lines.append("</div>")
    # Toggle control (client-side): when checked, show only multi-source stories
    html_lines.append(
        "<div style='margin:8px 0'><label><input type='checkbox' id='multiToggle' checked> Show only multi-source stories</label></div>"
    )

    html_lines.append("<table class='calendar'>")
    # header row with weekdays
    weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    html_lines.append("<tr><th></th>")
    for wd in weekdays:
        html_lines.append(f"<th>{html.escape(wd)}</th>")
    html_lines.append("</tr>")

    # organize segments by week and place them into rows to avoid overlap
    weeks_segments: dict[int, list[dict[str, Any]]] = {}
    for seg in segments:
        weeks_segments.setdefault(int(seg["week"]), []).append(seg)

    # compute per-week row placements
    week_rows: dict[int, list[list[tuple[int, int]]]] = {}
    seg_position: dict[int, list[dict[str, Any]]] = {}
    for wk in range(num_weeks):
        segs = weeks_segments.get(wk, [])
        rows: list[list[tuple[int, int]]] = []
        placements: list[dict[str, Any]] = []
        for s in segs:
            start_col = int(s["start_col"])  # 0..6
            end_col = start_col + int(s["span"]) - 1
            placed = False
            for r_idx, r in enumerate(rows):
                if all(
                    not (start_col <= r_end and end_col >= r_start)
                    for (r_start, r_end) in r
                ):
                    r.append((start_col, end_col))
                    placements.append({**s, "row": r_idx})
                    placed = True
                    break
            if not placed:
                rows.append([(start_col, end_col)])
                placements.append({**s, "row": len(rows) - 1})
        week_rows[wk] = rows
        seg_position[wk] = placements

    # render table rows per week
    for wk in range(num_weeks):
        wk_start = start_monday + dt.timedelta(days=wk * 7)
        rows = week_rows.get(wk, [])
        if not rows:
            # still render one empty row for the week
            html_lines.append(
                f"<tr><td class='week-label'>{html.escape(wk_start.isoformat())}</td>"
            )
            for d in range(7):
                day_idx = wk * 7 + d
                if day_idx >= num_days:
                    html_lines.append("<td></td>")
                else:
                    day_date = start + dt.timedelta(days=day_idx)
                    html_lines.append(f"<td>{html.escape(day_date.isoformat())}</td>")
            html_lines.append("</tr>")
            continue

        placements = seg_position.get(wk, [])
        max_rows = len(rows)
        for r in range(max_rows):
            html_lines.append("<tr>")
            if r == 0:
                html_lines.append(
                    f"<td class='week-label' rowspan='{max_rows}'>{html.escape(wk_start.isoformat())}</td>"
                )
            # for each day column, check if a placement starts here on this row
            for d in range(7):
                # find placement that starts at this day and row==r
                p = next(
                    (
                        p
                        for p in placements
                        if p.get("row") == r and p.get("start_col") == d
                    ),
                    None,
                )
                if p:
                    span = int(p.get("span", 1))
                    title = p.get("title") or "Story"
                    fname = p.get("event_fname")
                    weight = float(p.get("weight", 0.0))
                    hue = int((1.0 - max(0.0, min(1.0, weight))) * 240)
                    bg = f"hsl({hue},85%,45%)"
                    size_style = f"font-size:{p.get('font_scale',1.0):.2f}em;"
                    is_multi = 1 if len(p.get("sources", [])) > 1 else 0
                    # include up to two favicons for the event sources
                    srcs = p.get("sources", []) or []
                    icons_html = ""
                    for sname in srcs[:2]:
                        try:
                            ico = favicon_for_source(str(sname))
                        except (TypeError, ValueError):
                            ico = ""
                        if ico:
                            icons_html += f"<img src='{html.escape(ico)}' alt='{html.escape(str(sname))}'>"

                    # include useful data attributes for client-side filtering and sorting
                    data_sources = html.escape("|".join(srcs))
                    data_label = html.escape(str(p.get("label", "")))
                    data_score = float(p.get("score", 0.0))
                    data_mentions = int(p.get("mentions", 0))
                    data_start = p.get("start", "")
                    data_start = (
                        data_start.isoformat()
                        if hasattr(data_start, "isoformat")
                        else str(data_start)
                    )
                    data_end = p.get("end", "")
                    data_end = (
                        data_end.isoformat()
                        if hasattr(data_end, "isoformat")
                        else str(data_end)
                    )
                    html_lines.append(
                        f"<td colspan='{span}'><a class='event' data-label='{data_label}' data-sources='{data_sources}' data-score='{data_score:.3f}' data-mentions='{data_mentions}' data-start='{data_start}' data-end='{data_end}' data-multi='{is_multi}' href='{html.escape(fname)}' style='background:{bg};{size_style}' title='{html.escape(', '.join(srcs))}'>{icons_html}{html.escape(title)}</a></td>"
                    )
                else:
                    html_lines.append("<td></td>")
            html_lines.append("</tr>")

    html_lines.append("</table>")
    # Embed cluster metadata and add interactive controls (search, filters, sort, view)
    clusters_json = json.dumps(clusters, default=str)
    html_lines.append(f"<script>window.CLUSTERS = {clusters_json};</script>")
    html_lines.append(
        "<script>\n"
        "(function(){\n"
        "  // helper to select elements\n"
        "  function qs(sel){ return document.querySelector(sel); }\n"
        "  function qsa(sel){ return Array.from(document.querySelectorAll(sel)); }\n"
        "  const searchInput = document.createElement('input'); searchInput.placeholder='Search titles...'; searchInput.id='searchBox'; searchInput.style.marginRight='8px';\n"
        "  const sourceSelect = document.createElement('select'); sourceSelect.id='sourceFilter'; sourceSelect.style.marginRight='8px'; sourceSelect.multiple=true; sourceSelect.size=3;\n"
        "  const multiCheckbox = document.getElementById('multiToggle');\n"
        "  const sortSelect = document.createElement('select'); sortSelect.id='sortSelect'; sortSelect.style.marginLeft='8px';\n"
        "  sortSelect.innerHTML = '<option value='score'>Sort: score</option><option value='mentions'>Sort: mentions</option><option value='date'>Sort: date</option>';\n"
        "  const controls = document.createElement('div'); controls.style.marginBottom='8px'; controls.appendChild(searchInput); controls.appendChild(sourceSelect); controls.appendChild(sortSelect);\n"
        "  const legend = qs('.legend'); if(legend && legend.parentNode){ legend.parentNode.insertBefore(controls, legend.nextSibling); } else { document.body.insertBefore(controls, document.body.firstChild); }\n"
        "  // populate sources from clusters\n"
        "  const sourcesSet = new Set(); window.CLUSTERS.forEach(c=> (c.sources||[]).forEach(s=> sourcesSet.add(s)));\n"
        "  Array.from(sourcesSet).sort().forEach(s=>{ const opt = document.createElement('option'); opt.value=s; opt.text = s; sourceSelect.appendChild(opt); });\n"
        "  // filtering logic\n"
        "  function applyFilters(){\n"
        "    const q = (searchInput.value||'').toLowerCase();\n"
        "    const selectedSources = Array.from(sourceSelect.selectedOptions).map(o=>o.value);\n"
        "    const onlyMulti = multiCheckbox? multiCheckbox.checked : false;\n"
        "    qsa('.event').forEach(function(el){\n"
        "      const title = el.textContent.toLowerCase();\n"
        "      const sources = (el.getAttribute('data-sources')||'').split('|').filter(Boolean);\n"
        "      const score = parseFloat(el.getAttribute('data-score')||'0');\n"
        "      const mentions = parseInt(el.getAttribute('data-mentions')||'0');\n"
        "      const start = el.getAttribute('data-start')||'';\n"
        "      let visible = true;\n"
        "      if(q && !title.includes(q)) visible = false;\n"
        "      if(onlyMulti && el.getAttribute('data-multi') !== '1') visible = false;\n"
        "      if(selectedSources.length){ if(!selectedSources.some(s=> sources.includes(s))) visible = false; }\n"
        "      el.style.display = visible? 'inline-block' : 'none';\n"
        "    });\n"
        "  }\n"
        "  searchInput.addEventListener('input', applyFilters); sourceSelect.addEventListener('change', applyFilters); if(multiCheckbox) multiCheckbox.addEventListener('change', applyFilters);\n"
        "  // list view and sorting\n"
        "  const listBtn = document.createElement('button'); listBtn.textContent='List View'; listBtn.style.marginLeft='8px'; controls.appendChild(listBtn);\n"
        "  const listPane = document.createElement('div'); listPane.id='listPane'; listPane.style.marginTop='8px'; listPane.style.display='none'; listPane.style.maxHeight='60vh'; listPane.style.overflow='auto'; document.body.insertBefore(listPane, document.body.firstChild.nextSibling);\n"
        "  listBtn.addEventListener('click', function(){ if(listPane.style.display==='none'){ renderList(); listPane.style.display='block'; listBtn.textContent='Hide List'; } else { listPane.style.display='none'; listBtn.textContent='List View'; } });\n"
        "  sortSelect.addEventListener('change', function(){ if(listPane.style.display!=='none') renderList(); });\n"
        "  function renderList(){\n"
        "    const sortBy = sortSelect.value;\n"
        "    const filtered = window.CLUSTERS.filter(function(c){\n"
        "      // apply current filters to clusters\n"
        "      const q = (searchInput.value||'').toLowerCase();\n"
        "      if(q && !(String(c.rep_title||'').toLowerCase().includes(q))) return false;\n"
        "      if(multiCheckbox && multiCheckbox.checked && ( (c.sources||[]).length <= 1)) return false;\n"
        "      const selectedSources = Array.from(sourceSelect.selectedOptions).map(o=>o.value);\n"
        "      if(selectedSources.length){ if(!selectedSources.some(s=> (c.sources||[]).includes(s))) return false; }\n"
        "      return true;\n"
        "    });\n"
        "    filtered.sort(function(a,b){ if(sortBy==='score') return (b.score||0)-(a.score||0); if(sortBy==='mentions') return (b.mentions||0)-(a.mentions||0); if(sortBy==='date'){ return (b.start? Date.parse(b.start):0) - (a.start? Date.parse(a.start):0);} return 0; });\n"
        "    listPane.innerHTML = '';\n"
        "    filtered.forEach(function(c){ const div = document.createElement('div'); div.className='cluster-card'; div.style.border='1px solid #e6e6e6'; div.style.padding='8px'; div.style.margin='6px 0'; div.innerHTML = `<strong>${escapeHtml(c.rep_title||'')}</strong> <small>(${(c.sources||[]).join(', ')})</small><div>Score: ${(c.score||0).toFixed(3)} — Mentions: ${c.mentions||0}</div>`; listPane.appendChild(div); });\n"
        "  }\n"
        "  function escapeHtml(s){ return String(s).replace(/[&<>\"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',''':'&#39;'}[c]; }); }\n"
        "  // initial apply\n"
        "  applyFilters();\n"
        "})();\n"
        "</script>"
    )
    # Modal detail pane for event clusters
    html_lines.append("""
<div id='mm-modal' style='display:none;position:fixed;inset:0;background:rgba(0,0,0,0.5);align-items:center;justify-content:center;z-index:9999'>
    <div id='mm-modal-box' style='background:white;color:#111;padding:18px;border-radius:8px;max-width:900px;width:94%;max-height:80vh;overflow:auto;box-shadow:0 8px 24px rgba(0,0,0,0.2)'>
        <button id='mm-modal-close' style='float:right;border:none;background:#eee;padding:6px 10px;border-radius:4px;cursor:pointer'>Close</button>
        <h2 id='mm-modal-title' style='margin-top:8px'></h2>
        <div id='mm-modal-meta' style='color:#555;margin-bottom:8px'></div>
        <div id='mm-modal-items'></div>
    </div>
</div>
<script>
;(function(){
    function by(selector){ return document.querySelector(selector); }
    function byAll(selector){ return Array.from(document.querySelectorAll(selector)); }
    const modal = by('#mm-modal');
    const box = by('#mm-modal-box');
    const closeBtn = by('#mm-modal-close');
    const titleEl = by('#mm-modal-title');
    const metaEl = by('#mm-modal-meta');
    const itemsEl = by('#mm-modal-items');

    function openModalForLabel(label){
        const clusters = window.CLUSTERS || [];
        let cluster = clusters.find(c=> String(c.label)==String(label) || String(c.rep_title||'').toLowerCase().includes(String(label).toLowerCase()));
        if(!cluster){
            // fallback: try to match by title substring
            cluster = clusters.find(c=> String(c.rep_title||'').toLowerCase().includes(String(label).toLowerCase()));
        }
        if(!cluster) return;
        titleEl.textContent = cluster.rep_title || 'Story';
        metaEl.innerHTML = `Score: ${(cluster.score||0).toFixed(3)} — Mentions: ${cluster.mentions||0} — Sources: ${(cluster.sources||[]).join(', ')}`;
        itemsEl.innerHTML = '';
        (cluster.items||[]).forEach(function(it){
            const d = document.createElement('div'); d.style.marginBottom='8px';
            const a = document.createElement('a'); a.href = it.link || '#'; a.textContent = it.title || it.link || 'link'; a.target='_blank'; a.style.fontWeight='600';
            const meta = document.createElement('div'); meta.style.color='#444'; meta.textContent = `${it.feed||''} — ${it.date||''}`;
            d.appendChild(a); d.appendChild(meta); itemsEl.appendChild(d);
        });
        modal.style.display = 'flex';
        window.scrollTo(0,0);
    }

    function closeModal(){ modal.style.display = 'none'; }
    closeBtn.addEventListener('click', closeModal);
    modal.addEventListener('click', function(e){ if(e.target===modal) closeModal(); });

    // attach handlers to event anchors
    byAll('.event').forEach(function(el){
        el.addEventListener('click', function(ev){
            try{ ev.preventDefault(); ev.stopPropagation(); }catch(e){}
            const label = el.getAttribute('data-label') || el.textContent || el.getAttribute('title') || '';
            openModalForLabel(label);
        });
    });
})();
</script>
</body></html>
""")

    (output_dir / "calendar.html").write_text("\n".join(html_lines), encoding="utf-8")


def write_multi_source_md(clusters: list[dict[str, Any]], output_dir: Path) -> None:
    """Write a simple markdown file listing clusters with scoring metrics.

    Includes `persistence_days` and `velocity` so downstream analysis can
    ingest those numbers easily.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# Multi-source stories\n")
    if not clusters:
        lines.append("No clusters found.")
        (output_dir / "multi_source_stories.md").write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        return

    for c in sorted(clusters, key=lambda x: x.get("score", 0.0), reverse=True):
        title = c.get("rep_title") or "Story"
        score = float(c.get("score", 0.0))
        mentions = int(c.get("mentions", 0))
        sources = c.get("sources", [])
        start = c.get("start")
        end = c.get("end")
        persistence = int(c.get("persistence_days", 1))
        velocity = float(c.get("velocity", 0.0))

        lines.append(f"## {title}")
        lines.append("")
        lines.append(f"- Score: {score:.3f}")
        lines.append(f"- Mentions: {mentions}")
        lines.append(f"- Sources: {', '.join(sources)}")
        lines.append(f"- Start: {start}")
        lines.append(f"- End: {end}")
        lines.append(f"- Persistence_days: {persistence}")
        lines.append(f"- Velocity: {velocity:.3f}")
        lines.append("")
        # list member items
        for it in c.get("items", []):
            link = it.get("link") or ""
            it_title = it.get("title") or link
            feed = it.get("feed") or ""
            date = it.get("date") or ""
            lines.append(f"- [{it_title}]({link}) — {feed} — {date}")
        lines.append("")

    (output_dir / "multi_source_stories.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def write_review_ui(clusters: list[dict[str, Any]], output_dir: Path) -> None:
    """Write a small static review UI (JSON + HTML) for manual annotation.

    The page loads `review.json`, shows each cluster with score and items,
    lets the reviewer mark `important` or add `notes`, and export annotations
    as JSON for downstream analysis.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    # Prepare JSON data
    review_data = []
    for c in clusters:
        review_data.append(
            {
                "label": c.get("label"),
                "rep_title": c.get("rep_title"),
                "score": float(c.get("score", 0.0)),
                "mentions": int(c.get("mentions", 0)),
                "sources": c.get("sources", []),
                "start": str(c.get("start")),
                "end": str(c.get("end")),
                "items": c.get("items", []),
            }
        )

    (output_dir / "review.json").write_text(
        json.dumps(review_data, indent=2), encoding="utf-8"
    )

    # Simple HTML UI
    html_lines = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'><title>Review: Multi-source Stories</title>",
        "<style>body{font-family:Arial,Helvetica,sans-serif;padding:12px} .cluster{border:1px solid #ddd;padding:8px;margin:8px 0;border-radius:6px} .meta{font-size:12px;color:#555}</style>",
        "</head><body>",
        "<h1>Review Multi-source Stories</h1>",
        "<p>Mark clusters as important, add notes, and export annotations.</p>",
        "<div id='list'></div>",
        "<p><button id='export'>Export Annotations</button> <a id='download' style='display:none'>Download</a></p>",
        "<script>",
        "async function load(){",
        "  const resp = await fetch('review.json');",
        "  const data = await resp.json();",
        "  const list = document.getElementById('list');",
        "  data.forEach(function(c, idx){",
        "    const div = document.createElement('div'); div.className='cluster';",
        "    div.innerHTML = `<h3>${c.rep_title}</h3><div class='meta'>Score: ${c.score.toFixed(3)} — Mentions: ${c.mentions} — Sources: ${c.sources.join(', ')} — ${c.start} → ${c.end}</div>`;",
        "    const imp = document.createElement('label'); imp.innerHTML = '<input type=checkbox class=imp> Important';",
        "    const notes = document.createElement('div'); notes.innerHTML = '<textarea class=notes style=\"width:100%;height:60px\" placeholder=\"notes\"></textarea>';",
        "    div.appendChild(imp); div.appendChild(notes);",
        "    // items list",
        "    const ul = document.createElement('ul'); c.items.forEach(function(it){ const li = document.createElement('li'); li.innerHTML = '<a href=\"' + it.link + '\" target=_blank>' + (it.title||'') + '</a> — ' + (it.feed||'') + ' — ' + (it.date||''); ul.appendChild(li); });",
        "    div.appendChild(ul);",
        "    list.appendChild(div);",
        "  });",
        "}",
        "document.getElementById('export').addEventListener('click', async function(){",
        "  const clusters = document.querySelectorAll('.cluster');",
        "  const out = [];",
        "  clusters.forEach(function(div, idx){",
        "    const title = div.querySelector('h3').innerText;",
        "    const imp = div.querySelector('.imp').checked;",
        "    const notes = div.querySelector('.notes').value;",
        "    out.push({index:idx,title,important:imp,notes});",
        "  });",
        "  const blob = new Blob([JSON.stringify(out, null, 2)], {type:'application/json'});",
        "  const url = URL.createObjectURL(blob);",
        "  const a = document.getElementById('download'); a.href = url; a.download='annotations.json'; a.style.display='inline'; a.textContent='Download annotations.json';",
        "});",
        "load();",
        "</script>",
    ]

    (output_dir / "review.html").write_text("\n".join(html_lines), encoding="utf-8")


def load_entries_from_markdown(output_dir: Path) -> list[dict[str, Any]]:
    """Scan `output_dir` for Markdown digest files and parse entries.

    Expects files with sections starting `## Title` and metadata lines like
    `- Published: 2026-..` `- Source:` and `- Link:` followed by summary.
    Returns a list of dicts compatible with other processing functions.
    """
    entries: list[dict[str, Any]] = []
    md_files = sorted(output_dir.glob("*.md"))
    for md in md_files:
        text = md.read_text(encoding="utf-8")
        sections = re.split(r"\n## \s*", text)
        # first section is header
        for sec in sections[1:]:
            lines = sec.splitlines()
            title = lines[0].strip() if lines else "Untitled"
            published = None
            source = None
            link = None
            body_lines: list[str] = []
            for ln in lines[1:]:
                m = re.match(r"- Published:\s*(.+)", ln)
                if m:
                    dt_str = m.group(1).strip()
                    try:
                        dt_obj = dt.datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S UTC")
                        published = (
                            dt_obj.year,
                            dt_obj.month,
                            dt_obj.day,
                            dt_obj.hour,
                            dt_obj.minute,
                            dt_obj.second,
                            0,
                            0,
                            0,
                        )
                    except ValueError:
                        published = None
                    continue
                m = re.match(r"- Source:\s*(.+)", ln)
                if m:
                    source = m.group(1).strip()
                    continue
                m = re.match(r"- Link:\s*(.+)", ln)
                if m:
                    link = m.group(1).strip()
                    continue
                body_lines.append(ln)

            summary = "\n".join(body_lines).strip()
            entry: dict[str, Any] = {"title": title, "summary": summary}
            if link:
                entry["link"] = link
            if source:
                entry["_feed_name"] = source
            if published:
                entry["published_parsed"] = tuple(published)
            entries.append(entry)
    return entries


def main() -> None:  # pylint: disable=too-many-locals
    """Entry point: parse args, load config, run pipeline."""
    args = parse_args()
    config_path = Path(args.config)
    config = load_config(config_path)

    settings = config["settings"]
    output_dir = Path(args.output_dir or settings["output_dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    max_items = (
        args.max_items
        if args.max_items is not None
        else int(settings["max_items_per_feed"])
    )

    db_path = output_dir / "memory_whole.db"

    # ------------------------------------------------------------------
    # Legacy code paths (backward compat)
    # ------------------------------------------------------------------
    if getattr(args, "backpopulate", False):
        summary_max_sentences = (
            args.summary_sentences
            if getattr(args, "summary_sentences", None) is not None
            else int(settings.get("summary_max_sentences", 6))
        )
        summary_max_words = (
            args.summary_words
            if getattr(args, "summary_words", None) is not None
            else int(settings.get("summary_max_words", 300))
        )
        count = backpopulate_daily(
            config,
            output_dir,
            max_items,
            summary_max_sentences,
            summary_max_words,
        )
        print(f"Backpopulated {count} daily file(s) in {output_dir.resolve()}")
        return

    if getattr(args, "detect_important", False) and getattr(
        args, "from_markdown", False
    ):
        # Legacy calendar generation from markdown — run old path
        _legacy_detect_from_markdown(settings, output_dir, args)
        return

    # ------------------------------------------------------------------
    # New pipeline
    # ------------------------------------------------------------------
    conn = db_mod.connect(db_path)
    db_mod.init_db(conn)

    # --import-markdown: load existing .md digests into the database
    if getattr(args, "import_markdown", False):
        entries = load_entries_from_markdown(output_dir)
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        imported = 0
        for e in entries:
            link = first_non_empty(e.get("link"), "")
            if not link:
                continue
            title = first_non_empty(e.get("title"), "Untitled")
            source = first_non_empty(e.get("_feed_name"), "Unknown")
            pub_dt = item_datetime(e)
            published_at = pub_dt.strftime("%Y-%m-%d") if pub_dt else None
            first_seen = published_at or today
            summary = first_non_empty(e.get("summary"), "")
            db_mod.upsert_headline(
                conn,
                url=link,
                title=title,
                source=source,
                published_at=published_at,
                first_seen=first_seen,
                last_seen=first_seen,
                summary=summary,
            )
            imported += 1
        conn.commit()
        print(f"Imported {imported} headlines from markdown into {db_path}")
        # Track after import — use wide window to capture all historical data
        story_events = tracker.track_stories(conn, cluster_days=365)
        if story_events:
            print(f"  {len(story_events)} story merge/split event(s) detected")
        # Backfill snapshots from historical headline dates (not just today)
        snap_count = db_mod.backfill_daily_snapshots(conn)
        print(f"  Backfilled {snap_count} daily snapshot rows")
        dashboard.generate(conn, output_dir, config=config)
        print(f"Dashboard written to: {output_dir / 'index.html'}")
        conn.close()
        return

    # --status: print summary
    if getattr(args, "status", False):
        total_h = db_mod.headline_count(conn)
        total_s = db_mod.story_count(conn)
        top = db_mod.get_top_stories(conn, limit=10)
        disappeared = db_mod.get_disappeared_stories(conn)
        print(f"Headlines: {total_h}  Stories: {total_s}")
        if top:
            print("\nTop stories:")
            for s in top:
                print(
                    f"  [{s['status'].upper():6s}] {s['representative_title'][:60]}  "
                    f"({s['source_count']} sources, score {s['importance_score']:.1f})"
                )
        if disappeared:
            print("\nDisappeared:")
            for s in disappeared:
                print(
                    f"  [GONE]   {s['representative_title'][:60]}  "
                    f"(peak {s['peak_source_count']} sources, last {s['last_seen']})"
                )
        conn.close()
        return

    # --dashboard-only: regenerate without fetching
    if getattr(args, "dashboard_only", False):
        dashboard.generate(conn, output_dir, config=config)
        print(f"Dashboard written to: {output_dir / 'index.html'}")
        conn.close()
        return

    # Default: fetch → track → dashboard → alerts → digest
    feed_health = []
    if not getattr(args, "fetch_only", False):
        # Validate feeds before fetching
        validation = fetcher.validate_feeds(config)
        errors = [v for v in validation if not v.ok]
        warnings = [v for v in validation if v.warnings]
        if errors:
            print(f"Feed validation: {len(errors)} error(s)")
            for v in errors:
                for e in v.errors:
                    print(f"  ERROR  {v.name}: {e}")
        if warnings:
            for v in warnings:
                for w in v.warnings:
                    print(f"  WARN   {v.name}: {w}")

        first_run = db_mod.headline_count(conn) == 0
        print("Fetching feeds...")
        count, feed_health = fetcher.fetch_all_feeds(conn, config, max_items=max_items)
        print(f"  {count} headlines processed")

        # Report feed health
        failed = [fh for fh in feed_health if not fh.ok]
        if failed:
            print(f"  {len(failed)}/{len(feed_health)} feeds failed:")
            for fh in failed:
                print(f"    ✗ {fh.name}: {fh.error}")

        if first_run:
            print("First run detected — backpopulating historical data...")
            bp_count = backpopulate_daily(
                config,
                output_dir,
                max_items,
                int(settings.get("summary_max_sentences", 6)),
                int(settings.get("summary_max_words", 300)),
            )
            print(f"  Backpopulated {bp_count} daily file(s)")

    if not getattr(args, "fetch_only", False):
        print("Tracking stories...")
        story_events = tracker.track_stories(conn)
        print(f"  {db_mod.story_count(conn)} stories in database")
        if story_events:
            print(f"  {len(story_events)} story merge/split event(s):")
            for ev in story_events:
                if ev.event_type == "merge":
                    print(
                        f'    ⊕ Merged {len(ev.absorbed_ids)} stories into "{ev.survivor_title[:50]}"'
                    )
                else:
                    print(f'    ⊖ Split from "{ev.survivor_title[:50]}"')

        print("Generating dashboard...")
        dashboard.generate(conn, output_dir, config=config, feed_health=feed_health)
        print(f"Dashboard written to: {output_dir / 'index.html'}")

        # Write JSON export alongside dashboard
        _write_json_export(conn, output_dir)

        # Disappearance alerts
        alert_count = alerts.run_alerts(conn, config)
        if alert_count:
            print(f"  {alert_count} disappearance alert(s) sent")

        # Daily digest
        if digest.run_digest(conn, config, output_dir=str(output_dir)):
            print("  Digest generated")
    else:
        print("Fetch complete (--fetch-only, skipping tracking/dashboard)")

    conn.close()


def _write_json_export(
    conn: Any, output_dir: Path, config: dict[str, Any] | None = None
) -> None:
    """Write JSON files for external consumption (API-like export)."""
    del config  # compatibility arg retained for legacy callers/tests
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    top = db_mod.get_top_stories(conn, limit=50)
    disappeared = db_mod.get_disappeared_stories(conn)

    stories_out = []
    for s in top:
        sources = db_mod.get_story_source_names(conn, s["id"])
        stories_out.append(
            {
                "id": s["id"],
                "title": s["representative_title"],
                "status": s["status"],
                "importance_score": round(float(s["importance_score"]), 2),
                "source_count": s["source_count"],
                "mention_count": s["mention_count"],
                "peak_source_count": s["peak_source_count"],
                "first_seen": s["first_seen"],
                "last_seen": s["last_seen"],
                "sources": sources,
            }
        )

    disappeared_out = []
    for s in disappeared:
        disappeared_out.append(
            {
                "id": s["id"],
                "title": s["representative_title"],
                "peak_source_count": s["peak_source_count"],
                "first_seen": s["first_seen"],
                "last_seen": s["last_seen"],
            }
        )

    export = {
        "generated_at": today,
        "headline_count": db_mod.headline_count(conn),
        "story_count": db_mod.story_count(conn),
        "top_stories": stories_out,
        "disappeared_stories": disappeared_out,
    }

    (output_dir / "api.json").write_text(
        json.dumps(export, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _legacy_detect_from_markdown(
    settings: dict[str, Any],
    output_dir: Path,
    args: argparse.Namespace,
) -> None:
    """Legacy code path: --detect-important --from-markdown."""
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    daily_title_template = str(settings.get("daily_title", "Daily RSS Digest - {date}"))
    daily_title = daily_title_template.format(date=today)

    detection_entries = load_entries_from_markdown(output_dir)
    clusters = detect_important_clusters(detection_entries)
    if getattr(args, "show_only_multi", False):
        clusters = [c for c in clusters if len(c.get("sources", [])) > 1]

    cal_out_dir = output_dir
    write_calendar_html(clusters, cal_out_dir, title=daily_title)
    write_multi_source_md(clusters, cal_out_dir)
    write_review_ui(clusters, cal_out_dir)
    print(f"Calendar written to: {cal_out_dir / 'calendar.html'}")
    print(f"Markdown files generated in: {output_dir.resolve()}")


if __name__ == "__main__":
    main()
