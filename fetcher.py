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
# Fetch tuning defaults (overridable via config `settings`)
# ------------------------------------------------------------------

# Total parse attempts per feed (1 initial + retries). Retries cover
# transient network blips — a feed that fails once usually succeeds
# on the next attempt (e.g. WSJ intermittently returning nothing).
DEFAULT_FETCH_MAX_ATTEMPTS = 3
# Linear backoff between attempts: base_seconds * attempt_number.
DEFAULT_FETCH_BACKOFF_SECONDS = 1.0
# Items with a publish date older than this are quarantined, not stored.
# Guards against stale feeds served as current (e.g. CNN's edition.rss
# serving 2023 content) being stamped with today's date.
DEFAULT_MAX_ITEM_AGE_DAYS = 90


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
    stale_items: int = 0


def _parse_with_retries(
    url: str, user_agent: str, max_attempts: int, backoff_base: float
) -> Any:
    """Parse a feed URL, retrying transient failures with linear backoff.

    Retries on transport exceptions and on empty (bozo) parses, which are
    often transient. A clean but empty response is returned as-is without
    retrying. Raises the last exception if all attempts fail.
    """
    last_exc: Exception | None = None
    parsed: Any = None
    for attempt in range(1, max_attempts + 1):
        try:
            parsed = feedparser.parse(url, request_headers={"User-Agent": user_agent})
        except Exception as exc:  # pylint: disable=broad-exception-caught
            last_exc = exc
            parsed = None
        empty_parse = (
            parsed is not None
            and getattr(parsed, "bozo", False)
            and not getattr(parsed, "entries", None)
        )
        if parsed is not None and not empty_parse:
            return parsed
        if attempt < max_attempts:
            delay = backoff_base * attempt
            log.warning(
                "Feed %s: attempt %d/%d failed (%s), retrying in %.1fs",
                url,
                attempt,
                max_attempts,
                last_exc or getattr(parsed, "get", lambda *a: "?")("bozo_exception", "?"),
                delay,
            )
            if delay > 0:
                time.sleep(delay)
    if parsed is not None:
        return parsed
    raise last_exc or RuntimeError(f"Failed to parse feed: {url}")


def fetch_all_feeds(
    conn: Any,
    config: dict[str, Any],
    max_items: int | None = None,
) -> tuple[int, list[FeedHealth]]:
    """Fetch every configured feed and upsert headlines into the DB.

    Returns a tuple of (headline_count, list_of_FeedHealth).
    """
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    settings = config.get("settings", {})
    ua = settings.get("user_agent", DEFAULT_USER_AGENT)
    max_sentences = int(settings.get("summary_max_sentences", 6))
    max_words = int(settings.get("summary_max_words", 300))
    max_attempts = max(1, int(settings.get("fetch_max_attempts", DEFAULT_FETCH_MAX_ATTEMPTS)))
    backoff_base = max(
        0.0, float(settings.get("fetch_backoff_seconds", DEFAULT_FETCH_BACKOFF_SECONDS))
    )
    max_item_age_days = int(
        settings.get("max_item_age_days", DEFAULT_MAX_ITEM_AGE_DAYS)
    )
    today_date = dt.datetime.now(dt.timezone.utc).date()
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
            parsed = _parse_with_retries(
                url, ua, max_attempts=max_attempts, backoff_base=backoff_base
            )
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

                # Quarantine items dated far in the past: some feeds serve
                # years-old content as current, and without this check those
                # items would be stamped with today's date as fresh news.
                if (
                    max_item_age_days > 0
                    and pub_dt is not None
                    and (today_date - pub_dt.date()).days > max_item_age_days
                ):
                    fh.stale_items += 1
                    continue

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
        except (AttributeError, OSError, TypeError, ValueError) as exc:
            fh.error = str(exc)[:200]
            log.warning("Feed %s fetch failed: %s", name, fh.error)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            fh.error = str(exc)[:200]
            log.warning("Feed %s fetch failed: %s", name, fh.error)
        finally:
            fh.fetch_time_ms = int((time.monotonic() - t0) * 1000)
            health.append(fh)

    conn.commit()
    return count, health
