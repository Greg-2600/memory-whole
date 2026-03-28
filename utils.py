"""Shared utilities for Memory Mountain."""

from __future__ import annotations

import datetime as dt
import html
import re
from typing import Any

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def slugify(value: str) -> str:
    """Return a URL-safe slug for *value*."""
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value or "item"


def strip_html(value: str) -> str:
    """Remove HTML tags, unescape entities, collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def summarize_text(value: str, max_sentences: int = 6, max_words: int = 300) -> str:
    """Produce a compact summary from *value*."""
    if not value.strip():
        return ""
    cleaned = strip_html(value)
    if not cleaned:
        return ""
    sentence_candidates = re.split(r"(?<=[.!?])\s+", cleaned)
    selected: list[str] = []
    for sentence in sentence_candidates:
        if sentence.strip():
            selected.append(sentence.strip())
        if len(selected) >= max_sentences:
            break
    summary = " ".join(selected).strip() or cleaned
    words = summary.split()
    if len(words) > max_words:
        summary = " ".join(words[:max_words]).rstrip(".,;:!?") + "..."
    return summary


def first_non_empty(*values: Any) -> str:
    """Return the first non-empty string, or ``""``."""
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def item_datetime(entry: dict[str, Any]) -> dt.datetime | None:
    """Extract a UTC datetime from a feed entry, or None."""
    for key in ("published_parsed", "updated_parsed"):
        ts = entry.get(key)
        if ts:
            try:
                return dt.datetime(*ts[:6], tzinfo=dt.timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def favicon_for_source(source_name: str) -> str:
    """Return a favicon URL for a source name via Google S2."""
    if not source_name:
        return ""
    s = source_name.lower()
    mapping = {
        "cnn": "cnn.com",
        "msnbc": "msnbc.com",
        "fox": "foxnews.com",
        "abc": "abcnews.go.com",
        "cbs": "cbsnews.com",
        "pbs": "pbs.org",
        "npr": "npr.org",
        "ap ": "apnews.com",
        "ny times": "nytimes.com",
        "nyt": "nytimes.com",
        "washington post": "washingtonpost.com",
        "ny post": "nypost.com",
        "the hill": "thehill.com",
        "realclear": "realclearpolitics.com",
        "cnbc": "cnbc.com",
        "breitbart": "breitbart.com",
        "daily wire": "dailywire.com",
        "federalist": "thefederalist.com",
        "intercept": "theintercept.com",
        "democracy now": "democracynow.org",
        "guardian": "theguardian.com",
        "al jazeera": "aljazeera.com",
        "patriots.win": "patriots.win",
        "patriots": "patriots.win",
        "google": "news.google.com",
        "bbc": "bbc.co.uk",
        "drudge": "drudgereport.com",
        "rantingly": "rantingly.com",
    }
    for k, dom in mapping.items():
        if k in s:
            return f"https://www.google.com/s2/favicons?sz=64&domain={dom}"
    token = re.sub(r"[^a-z0-9.-]", "", s.split()[0])
    if token and "." in token:
        return f"https://www.google.com/s2/favicons?sz=64&domain={token}"
    return f"https://www.google.com/s2/favicons?sz=64&domain={token or 'example.com'}"


SOURCE_COLORS = {
    "cnn": "#cc0000",
    "msnbc": "#0066b2",
    "fox": "#003366",
    "abc": "#000000",
    "cbs": "#0f59a0",
    "pbs": "#2638c4",
    "npr": "#5a82a8",
    "ap ": "#ef3e42",
    "ny times": "#1a1a1a",
    "washington post": "#231f20",
    "ny post": "#cf1421",
    "the hill": "#28a0cb",
    "realclear": "#003d6b",
    "cnbc": "#005594",
    "breitbart": "#f1592a",
    "daily wire": "#1b3a5c",
    "federalist": "#b8292f",
    "intercept": "#27ae60",
    "democracy now": "#d35400",
    "guardian": "#052962",
    "al jazeera": "#fa9000",
    "bbc": "#bb1919",
    "google": "#4285f4",
    "patriots": "#b91c1c",
    "drudge": "#222222",
    "rantingly": "#7c3aed",
}


def color_for_source(source_name: str) -> str:
    """Return a hex colour for a source, falling back to gray."""
    s = source_name.lower()
    for k, c in SOURCE_COLORS.items():
        if k in s:
            return c
    return "#6b7280"
