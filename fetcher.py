"""Fetch RSS feeds and store headlines in the database."""

from __future__ import annotations

import datetime as dt
import html as html_mod
from typing import Any

import feedparser

import db
from utils import (
    DEFAULT_USER_AGENT,
    first_non_empty,
    item_datetime,
    summarize_text,
)


def fetch_all_feeds(
    conn: Any,
    config: dict[str, Any],
    max_items: int = 250,
) -> int:
    """Fetch every configured feed and upsert headlines into the DB.

    Returns the number of headline rows touched (inserted or updated).
    """
    today = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    settings = config.get("settings", {})
    ua = settings.get("user_agent", DEFAULT_USER_AGENT)
    max_sentences = int(settings.get("summary_max_sentences", 6))
    max_words = int(settings.get("summary_max_words", 300))
    count = 0

    for feed_cfg in config.get("feeds", []):
        name = str(feed_cfg.get("name", "Unnamed Feed"))
        url = str(feed_cfg.get("url", "")).strip()
        if not url:
            continue

        parsed = feedparser.parse(url, request_headers={"User-Agent": ua})

        for entry in parsed.entries[:max_items]:
            link = first_non_empty(entry.get("link"), "").strip()
            if not link:
                continue

            title = html_mod.unescape(first_non_empty(entry.get("title"), "Untitled"))

            pub_dt = item_datetime(entry)
            published_at = pub_dt.strftime("%Y-%m-%d") if pub_dt else None

            # Extract body text for summary
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
                first_seen=today,
                last_seen=today,
                summary=summary,
            )
            count += 1

    conn.commit()
    return count
