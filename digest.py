"""Daily digest: generate and deliver a summary of today's intelligence.

Produces a concise report of:
  - New / rising stories
  - Disappeared stories
  - Silence gaps (stories one side ignores)
  - Key stats

Delivery channels:
  - ntfy.sh push notification
  - SMTP email
  - Plain text written to output/digest-{date}.txt
"""

from __future__ import annotations

import email.mime.multipart
import email.mime.text
import logging
import smtplib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import db
from alerts import send_ntfy
from silence import detect_silence

log = logging.getLogger(__name__)


def _build_digest_text(conn: sqlite3.Connection, config: dict[str, Any]) -> str:
    """Build a plain-text digest of the current state."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines: list[str] = []

    lines.append(f"=== Memory Whole Daily Digest — {today} ===")
    lines.append("")

    # Stats
    hl_count = db.headline_count(conn)
    st_count = db.story_count(conn)
    lines.append(f"Headlines: {hl_count}  |  Stories: {st_count}")
    lines.append("")

    # Top active stories
    top = db.get_top_stories(conn, limit=10)
    if top:
        lines.append("── TOP STORIES ──")
        for i, s in enumerate(top, 1):
            src_ct = s["source_count"]
            status = s["status"].upper()
            lines.append(
                f"  {i}. [{status}] {s['representative_title'][:80]} "
                f"({src_ct} source{'s' if src_ct != 1 else ''})"
            )
        lines.append("")

    # Disappeared
    disappeared = db.get_disappeared_stories(conn, min_peak_sources=2)
    if disappeared:
        lines.append("── DISAPPEARED ──")
        for s in disappeared[:10]:
            lines.append(
                f"  • {s['representative_title'][:80]} "
                f"(peak {s['peak_source_count']} sources, last seen {s['last_seen']})"
            )
        lines.append("")

    # Silence gaps
    silence_cfg = config.get("silence", {})
    if silence_cfg.get("enabled", True):
        min_src = int(silence_cfg.get("min_sources_covering", 2))
        lookback = int(silence_cfg.get("lookback_days", 7))
        gaps = detect_silence(
            conn, min_sources_covering=min_src, lookback_days=lookback
        )
        if gaps:
            lines.append("── SILENCE GAPS ──")
            for g in gaps[:10]:
                side = g.silent_side.upper()
                covering = (
                    g.left_sources if g.silent_side == "right" else g.right_sources
                )
                lines.append(
                    f"  • {side} SILENT: {g.title[:70]} "
                    f"(covered by {', '.join(covering[:3])})"
                )
            lines.append("")

    if len(lines) <= 4:
        lines.append("No significant activity to report.")

    lines.append("— Memory Whole")
    return "\n".join(lines)


def _send_email(
    subject: str,
    body: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_pass: str,
    smtp_from: str,
    smtp_to: str,
) -> bool:
    """Send a plain-text email via SMTP. Returns True on success."""
    if not all([smtp_host, smtp_user, smtp_pass, smtp_from, smtp_to]):
        return False

    msg = email.mime.multipart.MIMEMultipart()
    msg["From"] = smtp_from
    msg["To"] = smtp_to
    msg["Subject"] = subject
    msg.attach(email.mime.text.MIMEText(body, "plain", "utf-8"))

    try:
        with smtplib.SMTP(smtp_host, int(smtp_port), timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, [smtp_to], msg.as_string())
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("Email send failed: %s", exc)
        return False


def run_digest(
    conn: sqlite3.Connection,
    config: dict[str, Any],
    output_dir: str | None = None,
) -> bool:
    """Build and deliver the daily digest.

    Always writes to output/digest-{date}.txt.
    Optionally sends via ntfy and/or email based on config.
    Returns True if the digest was generated.
    """
    digest_cfg = config.get("digest", {})
    if not digest_cfg.get("enabled", False):
        return False

    text = _build_digest_text(conn, config)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Always write to file
    if output_dir:
        out = Path(output_dir) / f"digest-{today}.txt"
        out.write_text(text, encoding="utf-8")
        log.info("Digest written to %s", out)

    # ntfy delivery
    ntfy_topic = str(digest_cfg.get("ntfy_topic", "")).strip()
    if ntfy_topic:
        # ntfy has a 4096-byte limit; truncate if needed
        truncated = text[:3900] + ("\n[truncated]" if len(text) > 3900 else "")
        send_ntfy(
            ntfy_topic,
            f"Memory Whole Digest — {today}",
            truncated,
            priority="default",
        )

    # Email delivery
    smtp = {
        k: str(digest_cfg.get(k, ""))
        for k in ("smtp_host", "smtp_user", "smtp_pass", "smtp_from", "smtp_to")
    }
    smtp_port = int(digest_cfg.get("smtp_port", 587))
    if smtp["smtp_host"]:
        _send_email(
            subject=f"Memory Whole Digest — {today}",
            body=text,
            smtp_host=smtp["smtp_host"],
            smtp_port=smtp_port,
            smtp_user=smtp["smtp_user"],
            smtp_pass=smtp["smtp_pass"],
            smtp_from=smtp["smtp_from"],
            smtp_to=smtp["smtp_to"],
        )

    return True
