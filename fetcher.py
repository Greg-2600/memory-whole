"""Fetch RSS feeds and store headlines in the database."""

from __future__ import annotations

import datetime as dt
import html as html_mod
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import feedparser

import db
from utils import (
    DEFAULT_USER_AGENT,
    first_non_empty,
    item_datetime,
    summarize_text,
)

log = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Feed validation
# ------------------------------------------------------------------


@dataclass
class FeedValidationResult:
    """Result of validating a single feed configuration entry."""

    name: str
    url: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0


def validate_feeds(config: dict[str, Any]) -> list[FeedValidationResult]:
    """Validate all feed entries in *config* and return results.

    Checks: required fields, URL format, duplicate names/URLs,
    valid lean values.
    """
    feeds = config.get("feeds", [])
    results: list[FeedValidationResult] = []
    seen_names: set[str] = set()
    seen_urls: set[str] = set()
    valid_leans = {
        "left",
        "center-left",
        "center",
        "center-right",
        "right",
        "international",
    }

    for i, entry in enumerate(feeds):
        name = str(entry.get("name", "")).strip()
        url = str(entry.get("url", "")).strip()
        lean = str(entry.get("lean", "")).strip().lower()
        r = FeedValidationResult(name=name or f"feed[{i}]", url=url)

        if not name:
            r.errors.append("Missing 'name'")
        if not url:
            r.errors.append("Missing 'url'")
        else:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                r.errors.append(
                    f"URL scheme must be http or https, got '{parsed.scheme}'"
                )
            if not parsed.netloc:
                r.errors.append("URL has no host")

        if lean and lean not in valid_leans:
            r.warnings.append(
                f"Unknown lean '{lean}' — expected one of {sorted(valid_leans)}"
            )

        if name.lower() in seen_names:
            r.warnings.append(f"Duplicate feed name '{name}'")
        if url and url in seen_urls:
            r.warnings.append("Duplicate feed URL")

        seen_names.add(name.lower())
        if url:
            seen_urls.add(url)
        results.append(r)

    return results


# ------------------------------------------------------------------
# Feed health tracking
# ------------------------------------------------------------------


@dataclass
class FeedHealth:
    """Health stats for a single feed after a fetch attempt."""

    name: str
    url: str
    ok: bool
    status_code: int | None = None
    items_fetched: int = 0
    fetch_time_ms: int = 0
    error: str = ""


def fetch_all_feeds(
    conn: Any,
    config: dict[str, Any],
    max_items: int = 250,
) -> tuple[int, list[FeedHealth]]:
    """Fetch every configured feed and upsert headlines into the DB.

    Returns a tuple of (headline_count, list_of_FeedHealth).
    """
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    settings = config.get("settings", {})
    ua = settings.get("user_agent", DEFAULT_USER_AGENT)
    max_sentences = int(settings.get("summary_max_sentences", 6))
    max_words = int(settings.get("summary_max_words", 300))
    count = 0
    health: list[FeedHealth] = []

    for feed_cfg in config.get("feeds", []):
        name = str(feed_cfg.get("name", "Unnamed Feed"))
        url = str(feed_cfg.get("url", "")).strip()
        if not url:
            continue

        t0 = time.monotonic()
        fh = FeedHealth(name=name, url=url, ok=False)
        try:
            parsed = feedparser.parse(url, request_headers={"User-Agent": ua})
            fh.status_code = getattr(parsed, "status", None)

            if parsed.bozo and not parsed.entries:
                exc = parsed.get("bozo_exception", "")
                fh.error = str(exc)[:200] if exc else "Feed parse error"
                fh.fetch_time_ms = int((time.monotonic() - t0) * 1000)
                health.append(fh)
                log.warning("Feed %s: %s", name, fh.error)
                continue

            fh.ok = True
            items_this_feed = 0

            for entry in parsed.entries[:max_items]:
                link = first_non_empty(entry.get("link"), "").strip()
                if not link:
                    continue

                title = html_mod.unescape(
                    first_non_empty(entry.get("title"), "Untitled")
                )

                pub_dt = item_datetime(entry)
                published_at = pub_dt.strftime("%Y-%m-%d") if pub_dt else None

                # Use the article's publish date as first_seen when available
                # so historical data gets proper backdated timestamps.
                first_seen = published_at or today

                content_value = ""
                if isinstance(entry.get("content"), list) and entry["content"]:
                    first_content = entry["content"][0]
                    if isinstance(first_content, dict):
                        content_value = first_non_empty(first_content.get("value"), "")
                raw = first_non_empty(
                    content_value,
                    entry.get("summary"),
                    entry.get("description"),
                    "",
                )
                summary = summarize_text(
                    raw, max_sentences=max_sentences, max_words=max_words
                )

                db.upsert_headline(
                    conn,
                    url=link,
                    title=title,
                    source=name,
                    published_at=published_at,
                    first_seen=first_seen,
                    last_seen=today,
                    summary=summary,
                )
                count += 1
                items_this_feed += 1

            fh.items_fetched = items_this_feed
        except Exception as exc:  # noqa: BLE001
            fh.error = str(exc)[:200]
            log.warning("Feed %s fetch failed: %s", name, fh.error)
        finally:
            fh.fetch_time_ms = int((time.monotonic() - t0) * 1000)
            health.append(fh)

    conn.commit()
    return count, health
