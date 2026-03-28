"""Generate the Memory Whole HTML dashboard.

Produces a single-page dashboard with five views:
  1. Top Stories   — highest-importance active stories
  2. Disappeared   — stories that were big but dropped off
  3. Silence       — stories one political side ignores
  4. Timeline      — heatmap of coverage over days
  5. Sources       — which outlets covered which stories
"""

from __future__ import annotations

import html as html_mod
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import db
from framing import analyze_framing
from silence import detect_silence
from utils import color_for_source, lean_for_source


def generate(
    conn: sqlite3.Connection,
    output_dir: Path,
    window_days: int = 30,
    config: dict | None = None,
    feed_health: list | None = None,
) -> None:
    """Write ``index.html`` and per-story detail pages into *output_dir*."""
    output_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).date()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    top = db.get_top_stories(conn)
    disappeared = db.get_disappeared_stories(conn)
    all_stories = db.get_all_stories(conn)
    matrix_rows = db.get_source_story_matrix(conn, days=window_days)

    # Silence detection
    cfg = config or {}
    silence_cfg = cfg.get("silence", {})
    if silence_cfg.get("enabled", True):
        min_src = int(silence_cfg.get("min_sources_covering", 1))
        lookback = int(silence_cfg.get("lookback_days", 7))
        silence_gaps = detect_silence(
            conn, min_sources_covering=min_src, lookback_days=lookback
        )
    else:
        silence_gaps = []

    # Framing analysis
    framing_divergences = analyze_framing(
        conn,
        min_sources=2,
        min_divergence=0.1,
        lookback_days=window_days,
    )

    # Preload sparkline data per story
    sparklines: dict[int, list[int]] = {}
    for s in all_stories:
        snaps = db.get_story_snapshots(conn, s["id"], days=window_days)
        # Build a day→source_count map
        snap_map = {r["date"]: r["source_count"] for r in snaps}
        data = []
        for i in range(window_days):
            d = (today - timedelta(days=window_days - 1 - i)).isoformat()
            data.append(snap_map.get(d, 0))
        sparklines[s["id"]] = data

    # Build source matrix: {source: {story_id: count}}
    source_matrix: dict[str, dict[int, int]] = {}
    for r in matrix_rows:
        source_matrix.setdefault(r["source"], {})[r["story_id"]] = r["cnt"]
    all_sources = sorted(source_matrix.keys())

    # Top stories for matrix (limit to top 20)
    matrix_stories = [s for s in all_stories if s["source_count"] > 1][:20]

    # Preload per-story source names for top stories (used by hero/grid cards)
    story_sources: dict[int, list[str]] = {}
    for s in top[:12]:
        story_sources[s["id"]] = db.get_story_source_names(conn, s["id"])

    # Write per-story detail pages
    for s in all_stories:
        _write_story_page(conn, s, output_dir, sparklines.get(s["id"], []))

    # Compute velocity (trend) for each story: compare last 3 days vs prior 3 days
    velocity_map: dict[int, str] = {}
    for s in all_stories:
        data = sparklines.get(s["id"], [])
        if len(data) >= 6:
            recent = sum(data[-3:])
            prior = sum(data[-6:-3])
            if recent > prior and prior > 0:
                velocity_map[s["id"]] = "rising"
            elif recent < prior and recent == 0:
                velocity_map[s["id"]] = "falling"
            elif recent < prior:
                velocity_map[s["id"]] = "cooling"
            else:
                velocity_map[s["id"]] = "steady"
        elif len(data) >= 3 and sum(data[-3:]) > 0:
            velocity_map[s["id"]] = "new"
        else:
            velocity_map[s["id"]] = "steady"

    # Write main dashboard
    page = _render_page(
        now_str,
        today,
        top,
        disappeared,
        all_stories,
        sparklines,
        all_sources,
        source_matrix,
        matrix_stories,
        window_days,
        silence_gaps,
        story_sources,
        framing_divergences,
        feed_health=feed_health or [],
        velocity_map=velocity_map,
    )
    (output_dir / "index.html").write_text(page, encoding="utf-8")

    # Keep calendar.html as alias for backward compat (Docker healthcheck)
    redirect = (
        "<!doctype html><html><head>"
        '<meta http-equiv="refresh" content="0;url=index.html">'
        '</head><body><a href="index.html">Dashboard</a></body></html>\n'
    )
    (output_dir / "calendar.html").write_text(redirect, encoding="utf-8")


# ------------------------------------------------------------------
# Sparkline SVG
# ------------------------------------------------------------------


def _sparkline(
    data: list[int], w: int = 120, h: int = 28, color: str = "#3b82f6"
) -> str:
    if not data or max(data) == 0:
        return f'<svg width="{w}" height="{h}"></svg>'
    mx = max(data) or 1
    step = w / max(1, len(data) - 1)
    pts = []
    for i, v in enumerate(data):
        x = i * step
        y = h - (v / mx * (h - 4)) - 2
        pts.append(f"{x:.1f},{y:.1f}")
    poly = " ".join(pts)
    fill_pts = f"0,{h} {poly} {w:.1f},{h}"
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" style="vertical-align:middle">'
        f'<polyline points="{fill_pts}" fill="{color}18" stroke="none"/>'
        f'<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="1.5" stroke-linecap="round"/>'
        f"</svg>"
    )


# ------------------------------------------------------------------
# Story card HTML
# ------------------------------------------------------------------


def _story_card(
    story: sqlite3.Row, spark_data: list[int], ghost: bool = False, velocity: str = ""
) -> str:
    sid = story["id"]
    title = html_mod.escape(story["representative_title"] or "Story")
    status = story["status"]
    score = float(story["importance_score"])
    sources = int(story["source_count"])
    mentions = int(story["mention_count"])
    peak = int(story["peak_source_count"])
    first = story["first_seen"] or ""
    last = story["last_seen"] or ""

    if status == "active":
        border_color = "#ef4444"
        badge = '<span class="status-pill active">ACTIVE</span>'
    elif status == "fading":
        border_color = "#f59e0b"
        badge = '<span class="status-pill fading">FADING</span>'
    else:
        border_color = "#9ca3af"
        badge = '<span class="status-pill gone">GONE</span>'

    spark_color = "#9ca3af" if ghost else "#3b82f6"
    sparkline = _sparkline(spark_data, color=spark_color)

    opacity = "opacity:0.7;" if ghost else ""

    days_active = ""
    if first and last:
        try:
            d = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
            days_active = f"{d}d active"
        except ValueError:
            pass

    # Days since last seen
    days_gone = ""
    if ghost and last:
        try:
            d = (date.today() - date.fromisoformat(last)).days
            days_gone = f'<span class="days-gone">Last seen {d} day{"s" if d != 1 else ""} ago</span>'
        except ValueError:
            pass

    vel_badge = _velocity_badge(velocity)

    return f"""\
<div class="story-card" style="border-left-color:{border_color};{opacity}" data-sources="{sources}" data-score="{score:.2f}">
  <div class="card-top">
    <div class="sparkline-box">{sparkline}</div>
    <div class="card-body">
      <h3><a href="story_{sid}.html">{title}</a></h3>
      <div class="meta">
        {badge}
        {vel_badge}
        <span class="stat">{sources} source{"s" if sources != 1 else ""}</span>
        <span class="stat">{mentions} mention{"s" if mentions != 1 else ""}</span>
        <span class="stat">peak {peak}</span>
        {f'<span class="stat">{days_active}</span>' if days_active else ''}
        {days_gone}
      </div>
    </div>
  </div>
</div>"""


# ------------------------------------------------------------------
# Per-story detail page
# ------------------------------------------------------------------


def _write_story_page(
    conn: sqlite3.Connection,
    story: sqlite3.Row,
    output_dir: Path,
    spark_data: list[int],
) -> None:
    sid = story["id"]
    title_esc = html_mod.escape(story["representative_title"] or "Story")
    headlines = db.get_story_headlines(conn, sid)
    snaps = db.get_story_snapshots(conn, sid, days=60)
    spark_large = _sparkline(spark_data, w=400, h=60, color="#3b82f6")

    items_html = []
    for h in headlines:
        h_title = html_mod.escape(h["title"] or "")
        h_url = html_mod.escape(h["url"] or "#")
        h_source = html_mod.escape(h["source"] or "")
        h_date = html_mod.escape(h["last_seen"] or "")
        src_color = color_for_source(h["source"])
        items_html.append(
            f'<div class="hl-row">'
            f'<span class="hl-badge" style="background:{src_color}">{h_source}</span>'
            f'<a href="{h_url}" target="_blank" rel="noopener">{h_title}</a>'
            f'<span class="hl-date">{h_date}</span>'
            f"</div>"
        )

    snap_html = ""
    if snaps:
        snap_html = "<h3>Daily coverage</h3><table class='snap-tbl'><tr><th>Date</th><th>Sources</th><th>Headlines</th></tr>"
        for sn in reversed(list(snaps)):
            snap_html += f"<tr><td>{sn['date']}</td><td>{sn['source_count']}</td><td>{sn['headline_count']}</td></tr>"
        snap_html += "</table>"

    page = f"""\
<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title_esc} — Memory Whole</title>
<style>
{_detail_css()}
</style></head><body>
<a href="index.html" class="back">&larr; Dashboard</a>
<h1>{title_esc}</h1>
<div class="stats">
  Status: <strong>{story['status']}</strong> &middot;
  Score: {float(story['importance_score']):.2f} &middot;
  Peak sources: {story['peak_source_count']} &middot;
  {story['first_seen']} &rarr; {story['last_seen']}
</div>
<div style="margin:16px 0">{spark_large}</div>
<h3>{len(headlines)} headline{"s" if len(headlines) != 1 else ""}</h3>
<div class="headlines">{"".join(items_html)}</div>
{snap_html}
</body></html>"""

    (output_dir / f"story_{sid}.html").write_text(page, encoding="utf-8")


def _detail_css() -> str:
    return """\
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
     max-width:900px;margin:0 auto;padding:20px;color:#1a1a2e;background:#fafbfc;line-height:1.6}
a{color:#2563eb;text-decoration:none} a:hover{text-decoration:underline}
.back{display:inline-block;margin-bottom:12px;font-size:14px;color:#6b7280}
h1{font-family:Georgia,'Times New Roman',serif;font-size:1.8em;margin:0 0 8px}
.stats{color:#6b7280;font-size:14px;margin-bottom:12px}
.headlines{display:flex;flex-direction:column;gap:6px}
.hl-row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #f0f0f0;font-size:14px}
.hl-badge{color:#fff;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;white-space:nowrap}
.hl-date{color:#9ca3af;font-size:12px;margin-left:auto;white-space:nowrap}
.snap-tbl{border-collapse:collapse;margin-top:8px;font-size:13px}
.snap-tbl th,.snap-tbl td{border:1px solid #e5e7eb;padding:4px 10px;text-align:center}
.snap-tbl th{background:#f3f4f6;font-weight:600}
@media (prefers-color-scheme:dark){
  body{background:#0f0f1a;color:#e5e7eb}
  .back{color:#9ca3af} a{color:#60a5fa}
  .stats{color:#9ca3af}
  .hl-row{border-bottom-color:#1e1e2e}
  .snap-tbl th{background:#1e1e2e} .snap-tbl th,.snap-tbl td{border-color:#2a2a3e}
}"""


# ------------------------------------------------------------------
# Tiered Top Stories rendering
# ------------------------------------------------------------------


def _source_pills(sources: list[str], limit: int = 6) -> str:
    """Render color-coded source pills with overflow indicator."""
    if not sources:
        return ""
    shown = sources[:limit]
    pills = []
    for s in shown:
        c = color_for_source(s)
        lbl = html_mod.escape(s.split("(")[0].strip())
        lean = lean_for_source(s)
        lean_cls = f" lean-{lean}" if lean else ""
        pills.append(
            f'<span class="src-pill{lean_cls}" style="background:{c}">{lbl}</span>'
        )
    overflow = len(sources) - limit
    if overflow > 0:
        pills.append(f'<span class="src-overflow">+{overflow} more</span>')
    return " ".join(pills)


def _render_top_stories(
    top: list[sqlite3.Row],
    sparklines: dict[int, list[int]],
    src_map: dict[int, list[str]],
    velocity_map: dict[int, str] | None = None,
) -> str:
    """Build the three-tier Top Stories section: hero, grid, compact."""
    vel = velocity_map or {}
    if not top:
        return '<p class="empty">No active stories yet. Run a fetch first.</p>'

    parts: list[str] = []

    # Tier 1: Hero card (top story)
    hero = top[0]
    parts.append(
        _hero_card(
            hero,
            sparklines.get(hero["id"], []),
            src_map.get(hero["id"], []),
            velocity=vel.get(hero["id"], ""),
        )
    )

    # Tier 2: Grid cards (stories 2-6)
    grid_stories = top[1:6]
    if grid_stories:
        grid_items = "\n".join(
            _grid_card(
                s,
                sparklines.get(s["id"], []),
                src_map.get(s["id"], []),
                rank=i + 2,
                velocity=vel.get(s["id"], ""),
            )
            for i, s in enumerate(grid_stories)
        )
        parts.append(f'<div class="tier-grid">{grid_items}</div>')

    # Tier 3: Compact list (stories 7+)
    compact_stories = top[6:]
    if compact_stories:
        rows = "\n".join(
            _compact_row(s, rank=i + 7, velocity=vel.get(s["id"], ""))
            for i, s in enumerate(compact_stories)
        )
        parts.append(f'<div class="tier-compact">{rows}</div>')

    return "\n".join(parts)


def _hero_card(
    story: sqlite3.Row,
    spark_data: list[int],
    sources: list[str],
    velocity: str = "",
) -> str:
    """Large featured card for the #1 story."""
    sid = story["id"]
    title = html_mod.escape(story["representative_title"] or "Story")
    status = story["status"]
    score = float(story["importance_score"])
    src_ct = int(story["source_count"])
    mentions = int(story["mention_count"])
    peak = int(story["peak_source_count"])

    badge = _status_badge(status)
    vel = _velocity_badge(velocity)
    spark = _sparkline(spark_data, w=320, h=50, color="#3b82f6")
    pills = _source_pills(sources, limit=8)

    days_active = _days_active(story)

    return f"""\
<div class="hero-card story-card" data-sources="{src_ct}" data-score="{score:.2f}">
  <div class="hero-rank">#1</div>
  <div class="hero-main">
    <h3><a href="story_{sid}.html">{title}</a></h3>
    <div class="hero-stats">
      {badge}
      {vel}
      <span class="stat-big">{src_ct} source{"s" if src_ct != 1 else ""}</span>
      <span class="stat-big">{mentions} mention{"s" if mentions != 1 else ""}</span>
      <span class="stat-big">peak {peak}</span>
      {f'<span class="stat-big">{days_active}</span>' if days_active else ''}
    </div>
    <div class="hero-sources">{pills}</div>
  </div>
  <div class="hero-spark">{spark}</div>
</div>"""


def _grid_card(
    story: sqlite3.Row,
    spark_data: list[int],
    sources: list[str],
    rank: int,
    velocity: str = "",
) -> str:
    """Medium card for stories #2-6 in a 2-column grid."""
    sid = story["id"]
    title = html_mod.escape(story["representative_title"] or "Story")
    status = story["status"]
    score = float(story["importance_score"])
    src_ct = int(story["source_count"])
    mentions = int(story["mention_count"])

    badge = _status_badge(status)
    vel = _velocity_badge(velocity)
    spark = _sparkline(spark_data, w=180, h=36, color="#3b82f6")
    pills = _source_pills(sources, limit=4)

    return f"""\
<div class="grid-card story-card" data-sources="{src_ct}" data-score="{score:.2f}">
  <div class="grid-top">
    <span class="grid-rank">#{rank}</span>
    {badge}
    {vel}
    <span class="stat">{src_ct} src</span>
    <span class="stat">{mentions} mentions</span>
  </div>
  <h3><a href="story_{sid}.html">{title}</a></h3>
  <div class="grid-spark">{spark}</div>
  <div class="grid-sources">{pills}</div>
</div>"""


def _compact_row(story: sqlite3.Row, rank: int, velocity: str = "") -> str:
    """Dense single-line row for stories #7+."""
    sid = story["id"]
    title_raw = story["representative_title"] or "Story"
    title = html_mod.escape(title_raw[:80] + ("\u2026" if len(title_raw) > 80 else ""))
    status = story["status"]
    score = float(story["importance_score"])
    src_ct = int(story["source_count"])
    mentions = int(story["mention_count"])

    status_cls = status  # active/fading/gone
    border = {"active": "#ef4444", "fading": "#f59e0b"}.get(status, "#9ca3af")
    vel = _velocity_badge(velocity)

    return f"""\
<div class="compact-row story-card" style="border-left-color:{border}" data-sources="{src_ct}" data-score="{score:.2f}">
  <span class="compact-rank">#{rank}</span>
  <span class="compact-status {status_cls}"></span>
  <a href="story_{sid}.html" class="compact-title">{title}</a>
  {vel}
  <span class="compact-meta">{src_ct} src &middot; {mentions} mentions</span>
</div>"""


def _status_badge(status: str) -> str:
    if status == "active":
        return '<span class="status-pill active">ACTIVE</span>'
    elif status == "fading":
        return '<span class="status-pill fading">FADING</span>'
    return '<span class="status-pill gone">GONE</span>'


def _days_active(story: sqlite3.Row) -> str:
    first = story["first_seen"] or ""
    last = story["last_seen"] or ""
    if first and last:
        try:
            d = (date.fromisoformat(last) - date.fromisoformat(first)).days + 1
            return f"{d}d active"
        except ValueError:
            pass
    return ""


def _velocity_badge(velocity: str) -> str:
    """Return a small trend indicator badge."""
    if velocity == "rising":
        return (
            '<span class="vel-badge rising" title="Trending up">&#9650; rising</span>'
        )
    elif velocity == "cooling":
        return (
            '<span class="vel-badge cooling" title="Cooling off">&#9660; cooling</span>'
        )
    elif velocity == "falling":
        return '<span class="vel-badge falling" title="Disappearing">&#9660; falling</span>'
    elif velocity == "new":
        return '<span class="vel-badge new" title="New story">&#9733; new</span>'
    return ""


# ------------------------------------------------------------------
# Main page
# ------------------------------------------------------------------


def _render_page(
    now_str: str,
    today: date,
    top: list[sqlite3.Row],
    disappeared: list[sqlite3.Row],
    all_stories: list[sqlite3.Row],
    sparklines: dict[int, list[int]],
    all_sources: list[str],
    source_matrix: dict[str, dict[int, int]],
    matrix_stories: list[sqlite3.Row],
    window_days: int,
    silence_gaps: list | None = None,
    story_sources: dict[int, list[str]] | None = None,
    framing_divergences: list | None = None,
    feed_health: list | None = None,
    velocity_map: dict[int, str] | None = None,
) -> str:
    src_map = story_sources or {}
    vel = velocity_map or {}

    # Top Stories: tiered layout (hero, grid, compact)
    top_section = _render_top_stories(top, sparklines, src_map, velocity_map=vel)

    # Disappeared cards
    dis_cards = (
        "\n".join(
            _story_card(
                s,
                sparklines.get(s["id"], []),
                ghost=True,
                velocity=vel.get(s["id"], ""),
            )
            for s in disappeared
        )
        or '<p class="empty">No disappeared stories detected yet.</p>'
    )

    # Timeline heatmap
    heatmap = _render_heatmap(all_stories, sparklines, today, window_days)

    # Source matrix
    matrix = _render_source_matrix(all_sources, source_matrix, matrix_stories)

    # Silence gaps
    silence_html = _render_silence_gaps(silence_gaps or [])

    # Framing divergence
    framing_html = _render_framing(framing_divergences or [])
    framing_ct = len(framing_divergences or [])

    # Feed health
    health_html = _render_feed_health(feed_health or [])
    health_ok = sum(1 for fh in (feed_health or []) if fh.ok)
    health_total = len(feed_health or [])

    # Stats
    total_hl = sum(s["mention_count"] for s in all_stories)
    total_st = len(all_stories)
    active_ct = sum(1 for s in all_stories if s["status"] == "active")
    gone_ct = sum(1 for s in all_stories if s["status"] in ("fading", "gone"))
    silence_ct = len(silence_gaps or [])

    return f"""\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Memory Whole</title>
<style>
{_main_css()}
</style>
</head>
<body>

<header>
  <div class="header-row">
    <div>
      <h1>Memory Whole</h1>
      <p class="subtitle">
        {html_mod.escape(now_str)} &middot;
        {total_hl} headlines &middot; {total_st} stories &middot;
        {active_ct} active &middot; {gone_ct} fading/gone
        {f' &middot; {silence_ct} silence gap{"s" if silence_ct != 1 else ""}' if silence_ct else ''}
      </p>
    </div>
    <button class="theme-toggle" id="themeToggle" title="Toggle dark mode">&#9789;</button>
  </div>
</header>

<nav>
  <button class="tab active" data-tab="top">Top Stories</button>
  <button class="tab" data-tab="disappeared">Disappeared</button>
  <button class="tab" data-tab="silence">Silence{f' ({silence_ct})' if silence_ct else ''}</button>
  <button class="tab" data-tab="framing">Framing{f' ({framing_ct})' if framing_ct else ''}</button>
  <button class="tab" data-tab="timeline">Timeline</button>
  <button class="tab" data-tab="sources">Sources</button>
  <button class="tab" data-tab="health">Feeds{f' ({health_ok}/{health_total})' if health_total else ''}</button>
</nav>

<div class="controls">
  <input type="text" id="search" placeholder="Search stories\u2026">
  <label><input type="checkbox" id="multiOnly"> Multi-source only</label>
</div>

<section id="tab-top" class="tab-content active">
  <h2>Top Stories</h2>
  <p class="section-desc">Ranked by importance: cross-source coverage \u00d7 persistence \u00d7 velocity.</p>
  {top_section}
</section>

<section id="tab-disappeared" class="tab-content">
  <h2>Disappeared</h2>
  <p class="section-desc">Stories that had significant coverage but stopped being reported.</p>
  <div class="card-list">{dis_cards}</div>
</section>

<section id="tab-silence" class="tab-content">
  <h2>Silence Gaps</h2>
  <p class="section-desc">Stories covered by one political side but ignored by the other. The core of what we track.</p>
  {silence_html}
</section>

<section id="tab-framing" class="tab-content">
  <h2>Framing</h2>
  <p class="section-desc">Stories where left-leaning and right-leaning outlets frame the same event with different tone. Blue dots = left sources, red dots = right sources, diamonds = group averages.</p>
  {framing_html}
</section>

<section id="tab-timeline" class="tab-content">
  <h2>Timeline</h2>
  <p class="section-desc">Source coverage per day over the last {window_days} days. Darker = more sources.</p>
  {heatmap}
</section>

<section id="tab-sources" class="tab-content">
  <h2>Source Coverage</h2>
  <p class="section-desc">Which outlets covered which stories. Gaps reveal editorial blind spots.</p>
  {matrix}
</section>

<section id="tab-health" class="tab-content">
  <h2>Feed Health</h2>
  <p class="section-desc">Status of each configured RSS feed from the last fetch cycle.</p>
  {health_html}
</section>

<script>
{_main_js()}
</script>

</body>
</html>"""


# ------------------------------------------------------------------
# Silence gaps
# ------------------------------------------------------------------


def _render_silence_gaps(gaps: list) -> str:
    """Render silence gap cards showing left/right asymmetry."""
    if not gaps:
        return '<p class="empty">No silence gaps detected. This is good — or there isn\'t enough data yet.</p>'

    lines: list[str] = []
    for g in gaps:
        title_esc = html_mod.escape(g.title or "Story")
        silent = g.silent_side
        if silent == "right":
            badge_class = "silence-right"
            badge_text = "RIGHT SILENT"
            covering_label = "Left/Center covering"
            covering = g.left_sources + g.center_sources
        else:
            badge_class = "silence-left"
            badge_text = "LEFT SILENT"
            covering_label = "Right/Center covering"
            covering = g.right_sources + g.center_sources

        source_pills = " ".join(
            f'<span class="src-pill" style="background:{color_for_source(s)}">'
            f"{html_mod.escape(s)}</span>"
            for s in covering[:8]
        )

        lines.append(f"""\
<div class="silence-card {badge_class}">
  <div class="silence-top">
    <span class="silence-badge {badge_class}">{badge_text}</span>
    <h3><a href="story_{g.story_id}.html">{title_esc}</a></h3>
  </div>
  <div class="silence-meta">
    <span class="covering-label">{covering_label}:</span>
    {source_pills}
  </div>
  <div class="silence-score">Score: {g.importance_score:.1f}</div>
</div>""")

    return "\n".join(lines)


# ------------------------------------------------------------------
# Framing divergence
# ------------------------------------------------------------------

_MAX_SPECTRUM_DOTS = 8  # per side
_MAX_HEADLINES = 6  # per side


def _sample_extreme(
    headlines: list[tuple[str, str, float]], n: int
) -> list[tuple[str, str, float]]:
    """Pick up to *n* headlines sampling from sentiment extremes."""
    if len(headlines) <= n:
        return list(headlines)
    by_score = sorted(headlines, key=lambda x: x[2])
    half = n // 2
    return by_score[:half] + by_score[-half:]


def _spectrum_svg(
    left_headlines: list[tuple[str, str, float]],
    right_headlines: list[tuple[str, str, float]],
    left_avg: float,
    right_avg: float,
    card_idx: int,
) -> str:
    """SVG showing average markers with a divergence band + sampled dots."""
    w, h = 480, 64
    pad = 24
    pw = w - 2 * pad

    def xf(score: float) -> float:
        return pad + (score + 1) / 2 * pw

    gid = f"sg{card_idx}"
    s: list[str] = [
        f'<svg width="100%" height="{h}" viewBox="0 0 {w} {h}" '
        f'preserveAspectRatio="xMidYMid meet" class="framing-spectrum">'
    ]

    # Gradient background track
    s.append(
        f'<defs><linearGradient id="{gid}" x1="0%" x2="100%">'
        '<stop offset="0%" stop-color="#fca5a5" stop-opacity=".18"/>'
        '<stop offset="50%" stop-color="#d1d5db" stop-opacity=".08"/>'
        '<stop offset="100%" stop-color="#86efac" stop-opacity=".18"/>'
        "</linearGradient></defs>"
    )
    cy = 24
    s.append(
        f'<rect x="{pad}" y="{cy - 14}" width="{pw}" height="28" '
        f'rx="14" fill="url(#{gid})"/>'
    )

    # Center (neutral) dashed line
    cx = xf(0)
    s.append(
        f'<line x1="{cx}" y1="{cy - 16}" x2="{cx}" y2="{cy + 16}" '
        f'stroke="#9ca3af" stroke-width="1" stroke-dasharray="3,3"/>'
    )

    # Divergence band between the two averages
    lx, rx = xf(left_avg), xf(right_avg)
    bx = min(lx, rx)
    bw = abs(rx - lx)
    if bw > 1:
        s.append(
            f'<rect x="{bx:.1f}" y="{cy - 10}" width="{bw:.1f}" height="20" '
            f'rx="4" fill="#8b5cf6" opacity=".15"/>'
        )

    # Sample dots (limited to avoid clutter)
    left_sample = _sample_extreme(left_headlines, _MAX_SPECTRUM_DOTS)
    right_sample = _sample_extreme(right_headlines, _MAX_SPECTRUM_DOTS)
    for i, (_, _, sc) in enumerate(left_sample):
        y = cy + (i % 3 - 1) * 6
        s.append(
            f'<circle cx="{xf(sc):.1f}" cy="{y}" r="4" '
            f'fill="#3b82f6" opacity=".7" stroke="#fff" stroke-width=".8"/>'
        )
    for i, (_, _, sc) in enumerate(right_sample):
        y = cy + (i % 3 - 1) * 6
        s.append(
            f'<circle cx="{xf(sc):.1f}" cy="{y}" r="4" '
            f'fill="#ef4444" opacity=".7" stroke="#fff" stroke-width=".8"/>'
        )

    # Mean markers (diamonds) — drawn last so they're on top
    for avg, color in [(left_avg, "#2563eb"), (right_avg, "#dc2626")]:
        mx = xf(avg)
        s.append(
            f'<polygon points="{mx},{cy - 8} {mx + 8},{cy} '
            f'{mx},{cy + 8} {mx - 8},{cy}" '
            f'fill="{color}" stroke="#fff" stroke-width="1.5"/>'
        )

    # Axis labels
    s.append(
        f'<text x="{pad}" y="{h - 2}" font-size="9" fill="#9ca3af" '
        f'font-family="system-ui">Negative</text>'
    )
    s.append(
        f'<text x="{cx}" y="{h - 2}" font-size="9" fill="#9ca3af" '
        f'text-anchor="middle" font-family="system-ui">Neutral</text>'
    )
    s.append(
        f'<text x="{w - pad}" y="{h - 2}" font-size="9" fill="#9ca3af" '
        f'text-anchor="end" font-family="system-ui">Positive</text>'
    )

    s.append("</svg>")
    return "\n".join(s)


def _sentiment_bg(score: float) -> str:
    """Subtle background tint for a sentiment score."""
    if score >= 0.3:
        return "rgba(34,197,94,0.12)"
    if score >= 0.05:
        return "rgba(34,197,94,0.06)"
    if score <= -0.3:
        return "rgba(239,68,68,0.12)"
    if score <= -0.05:
        return "rgba(239,68,68,0.06)"
    return "transparent"


def _framing_hl_list(
    headlines: list[tuple[str, str, float]], limit: int = _MAX_HEADLINES
) -> str:
    """Render a capped list of headline rows, sorted by sentiment."""
    by_score = sorted(headlines, key=lambda x: x[2])
    shown = _sample_extreme(by_score, limit)
    remainder = len(headlines) - len(shown)

    parts: list[str] = []
    for src, title, score in sorted(shown, key=lambda x: x[2]):
        src_esc = html_mod.escape(src)
        t_esc = html_mod.escape(title[:120])
        bg = _sentiment_bg(score)
        src_color = color_for_source(src)
        parts.append(
            f'<div class="frame-hl" style="background:{bg}">'
            f'<span class="src-pill" style="background:{src_color}">{src_esc}</span>'
            f'<span class="frame-hl-title">{t_esc}</span>'
            f'<span class="frame-hl-score">{score:+.2f}</span>'
            f"</div>"
        )
    if remainder > 0:
        parts.append(
            f'<div class="frame-hl-more">… and {remainder} more headline'
            f'{"s" if remainder != 1 else ""}</div>'
        )
    return "\n".join(parts) if parts else '<span class="empty">None</span>'


def _render_framing(divergences: list) -> str:
    """Render framing divergence cards with spectrum viz and headlines."""
    if not divergences:
        return (
            '<p class="empty">No framing divergences detected yet — '
            "need stories covered by both sides with different tone.</p>"
        )

    lines: list[str] = []
    for i, d in enumerate(divergences):
        title_esc = html_mod.escape(d.title)
        div_pct = min(100, d.divergence * 100)

        spectrum = _spectrum_svg(
            d.left_headlines,
            d.right_headlines,
            d.left_sentiment,
            d.right_sentiment,
            i,
        )

        left_items = _framing_hl_list(d.left_headlines)
        right_items = _framing_hl_list(d.right_headlines)

        lines.append(f"""\
<div class="framing-card">
  <div class="framing-header">
    <h3><a href="story_{d.story_id}.html">{title_esc}</a></h3>
    <span class="framing-divergence" style="background:hsl({max(0, 60 - div_pct * 0.6):.0f}, 80%, 48%)">
      {d.divergence:.2f} divergence
    </span>
  </div>
  <div class="framing-spectrum-wrap">
    <div class="framing-legend">
      <span class="legend-item"><span class="legend-dot legend-left"></span> Left avg: {d.left_sentiment:+.2f} ({len(d.left_headlines)})</span>
      <span class="legend-item"><span class="legend-dot legend-right"></span> Right avg: {d.right_sentiment:+.2f} ({len(d.right_headlines)})</span>
    </div>
    {spectrum}
  </div>
  <div class="framing-columns">
    <div class="framing-col">
      <h4><span class="legend-dot legend-left"></span> Left / Center-Left</h4>
      {left_items}
    </div>
    <div class="framing-col">
      <h4><span class="legend-dot legend-right"></span> Right / Center-Right</h4>
      {right_items}
    </div>
  </div>
</div>""")

    return "\n".join(lines)


# ------------------------------------------------------------------
# Timeline heatmap
# ------------------------------------------------------------------


def _render_heatmap(
    stories: list[sqlite3.Row],
    sparklines: dict[int, list[int]],
    today: date,
    window_days: int,
) -> str:
    # Show top 40 stories (by score) in the heatmap
    shown = [s for s in stories if s["source_count"] > 0][:40]
    if not shown:
        return '<p class="empty">No data yet.</p>'

    days = []
    for i in range(window_days):
        days.append(today - timedelta(days=window_days - 1 - i))

    # Sort by most recent activity first, then by total coverage
    def _heatmap_sort_key(s):
        data = sparklines.get(s["id"], [0] * window_days)
        # Last day with any coverage (higher = more recent)
        last_active = -1
        for i in range(len(data) - 1, -1, -1):
            if data[i] > 0:
                last_active = i
                break
        total = sum(data)
        return (-last_active, -total, -(s["importance_score"] or 0))

    shown.sort(key=_heatmap_sort_key)

    lines = [
        '<div class="heatmap-controls">'
        '<span class="hm-label">Sort:</span>'
        '<button class="hm-sort active" data-sort="recent">Recent first</button>'
        '<button class="hm-sort" data-sort="coverage">Most covered</button>'
        '<button class="hm-sort" data-sort="score">Highest score</button>'
        "</div>",
        '<div class="heatmap-wrap"><table class="heatmap"><thead><tr><th class="story-col"></th>',
    ]
    for d in days:
        label = d.strftime("%d")
        lines.append(f'<th class="day-hdr" title="{d.isoformat()}">{label}</th>')
    lines.append("</tr></thead><tbody>")

    for s in shown:
        data = sparklines.get(s["id"], [0] * window_days)
        title_esc = html_mod.escape((s["representative_title"] or "")[:50])
        sid = s["id"]
        total_cov = sum(data)
        last_active = -1
        for i in range(len(data) - 1, -1, -1):
            if data[i] > 0:
                last_active = i
                break
        score = s["importance_score"] or 0
        lines.append(
            f'<tr data-recent="{last_active}" data-coverage="{total_cov}" data-score="{score:.1f}">'
            f'<td class="story-col"><a href="story_{sid}.html" title="{html_mod.escape(s["representative_title"] or "")}">{title_esc}</a></td>'
        )
        mx = max(data) if data else 1
        for v in data:
            if v == 0:
                lines.append('<td class="cell c0"></td>')
            else:
                intensity = min(4, max(1, round(v / max(1, mx) * 4)))
                lines.append(f'<td class="cell c{intensity}"></td>')
        lines.append("</tr>")

    lines.append("</tbody></table></div>")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Source matrix
# ------------------------------------------------------------------


def _render_source_matrix(
    all_sources: list[str],
    source_matrix: dict[str, dict[int, int]],
    matrix_stories: list[sqlite3.Row],
) -> str:
    if not matrix_stories or not all_sources:
        return '<p class="empty">Not enough data for source matrix.</p>'

    lines = ['<div class="matrix-wrap"><table class="matrix"><thead><tr><th></th>']
    for src in all_sources:
        color = color_for_source(src)
        short = html_mod.escape(src.split("(")[0].strip()[:12])
        lines.append(
            f'<th class="src-hdr" style="color:{color}" title="{html_mod.escape(src)}">{short}</th>'
        )
    lines.append("</tr></thead><tbody>")

    for s in matrix_stories:
        sid = s["id"]
        title_esc = html_mod.escape((s["representative_title"] or "")[:45])
        lines.append(
            f'<tr><td class="story-col"><a href="story_{sid}.html">{title_esc}</a></td>'
        )
        for src in all_sources:
            cnt = source_matrix.get(src, {}).get(sid, 0)
            if cnt:
                lines.append(
                    f'<td class="mx-cell filled" title="{cnt} headlines">{cnt}</td>'
                )
            else:
                lines.append('<td class="mx-cell"></td>')
        lines.append("</tr>")

    lines.append("</tbody></table></div>")
    return "\n".join(lines)


# ------------------------------------------------------------------
# Feed health
# ------------------------------------------------------------------


def _render_feed_health(health: list) -> str:
    """Render feed health status table."""
    if not health:
        return '<p class="empty">No feed health data — run a fetch first.</p>'

    ok_ct = sum(1 for fh in health if fh.ok)
    fail_ct = len(health) - ok_ct
    avg_ms = int(sum(fh.fetch_time_ms for fh in health) / max(1, len(health)))
    total_items = sum(fh.items_fetched for fh in health)

    lines = [
        '<div class="health-summary">',
        f'<span class="health-stat ok">{ok_ct} OK</span>',
        f'<span class="health-stat fail">{fail_ct} Failed</span>' if fail_ct else "",
        f'<span class="health-stat">{total_items} items</span>',
        f'<span class="health-stat">avg {avg_ms}ms</span>',
        "</div>",
        '<table class="health-tbl"><thead><tr>',
        "<th>Status</th><th>Feed</th><th>Items</th><th>Time</th><th>Error</th>",
        "</tr></thead><tbody>",
    ]

    for fh in sorted(health, key=lambda h: (h.ok, -h.fetch_time_ms)):
        icon = "&#10003;" if fh.ok else "&#10007;"
        cls = "ok" if fh.ok else "fail"
        err = html_mod.escape(fh.error[:80]) if fh.error else ""
        name_esc = html_mod.escape(fh.name)
        lines.append(
            f'<tr class="health-{cls}">'
            f'<td class="health-icon {cls}">{icon}</td>'
            f"<td>{name_esc}</td>"
            f"<td>{fh.items_fetched}</td>"
            f"<td>{fh.fetch_time_ms}ms</td>"
            f"<td>{err}</td></tr>"
        )

    lines.append("</tbody></table>")
    return "\n".join(lines)


# ------------------------------------------------------------------
# CSS
# ------------------------------------------------------------------


def _main_css() -> str:
    return """\
*{box-sizing:border-box}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  max-width:1200px;margin:0 auto;padding:20px 24px;
  color:#1a1a2e;background:#fafbfc;line-height:1.5;
  transition:background .2s,color .2s;
}
a{color:#2563eb;text-decoration:none} a:hover{text-decoration:underline}
header{margin-bottom:20px}
.header-row{display:flex;justify-content:space-between;align-items:flex-start}
header h1{
  font-family:Georgia,'Times New Roman',serif;
  font-size:2em;margin:0 0 4px;letter-spacing:-0.5px;
}
.subtitle{color:#6b7280;font-size:13px;margin:0}

/* Dark mode toggle */
.theme-toggle{
  background:none;border:1px solid #d1d5db;border-radius:50%;
  width:36px;height:36px;font-size:20px;cursor:pointer;
  display:flex;align-items:center;justify-content:center;
  color:#6b7280;transition:all .2s;flex-shrink:0;margin-top:4px;
}
.theme-toggle:hover{border-color:#3b82f6;color:#3b82f6}

/* Tabs */
nav{display:flex;gap:4px;border-bottom:2px solid #e5e7eb;margin-bottom:16px;padding-bottom:0;overflow-x:auto;-webkit-overflow-scrolling:touch}
.tab{
  background:none;border:none;padding:8px 16px;cursor:pointer;
  font-size:14px;font-weight:600;color:#6b7280;border-bottom:2px solid transparent;
  margin-bottom:-2px;transition:color .15s,border-color .15s;white-space:nowrap;
}
.tab:hover{color:#1a1a2e}
.tab.active{color:#2563eb;border-bottom-color:#2563eb}
.tab-content{display:none}
.tab-content.active{display:block}

/* Controls */
.controls{display:flex;gap:12px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.controls input[type=text]{
  padding:6px 12px;border:1px solid #d1d5db;border-radius:6px;
  font-size:13px;width:240px;max-width:100%;
}
.controls label{font-size:13px;color:#6b7280;cursor:pointer}

/* Section */
h2{font-family:Georgia,'Times New Roman',serif;font-size:1.4em;margin:0 0 4px}
.section-desc{color:#6b7280;font-size:13px;margin:0 0 14px}
.empty{color:#9ca3af;font-style:italic}

/* Story cards */
.card-list{display:flex;flex-direction:column;gap:10px}
.story-card{
  background:#fff;border:1px solid #e5e7eb;border-left:4px solid #ef4444;
  border-radius:8px;padding:14px 18px;transition:box-shadow .15s;
}
.story-card:hover{box-shadow:0 2px 8px rgba(0,0,0,0.06)}
.card-top{display:flex;align-items:flex-start;gap:14px}
.sparkline-box{flex-shrink:0}
.card-body{flex:1;min-width:0}
.card-body h3{margin:0 0 4px;font-size:1.05em}
.card-body h3 a{color:inherit}
.meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:12px;color:#6b7280}
.stat{background:#f3f4f6;padding:2px 8px;border-radius:10px}
.status-pill{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700;color:#fff}
.status-pill.active{background:#ef4444}
.status-pill.fading{background:#f59e0b}
.status-pill.gone{background:#9ca3af}
.days-gone{color:#9ca3af;font-style:italic;font-size:12px}

/* Velocity badges */
.vel-badge{padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700;white-space:nowrap}
.vel-badge.rising{background:#dcfce7;color:#166534}
.vel-badge.cooling{background:#fef3c7;color:#92400e}
.vel-badge.falling{background:#fee2e2;color:#991b1b}
.vel-badge.new{background:#ede9fe;color:#5b21b6}

/* Hero card (top story) */
.hero-card{
  background:#fff;border:1px solid #e5e7eb;border-left:5px solid #ef4444;
  border-radius:10px;padding:20px 24px;margin-bottom:16px;
  display:flex;align-items:flex-start;gap:18px;
}
.hero-card:hover{box-shadow:0 4px 16px rgba(0,0,0,0.08)}
.hero-rank{
  font-size:28px;font-weight:800;color:#e5e7eb;line-height:1;
  font-family:Georgia,'Times New Roman',serif;flex-shrink:0;min-width:40px;
}
.hero-main{flex:1;min-width:0}
.hero-main h3{margin:0 0 8px;font-size:1.25em;line-height:1.3}
.hero-main h3 a{color:inherit}
.hero-stats{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-bottom:8px}
.stat-big{background:#f3f4f6;padding:3px 10px;border-radius:10px;font-size:12px;font-weight:600;color:#4b5563}
.hero-sources{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px}
.hero-spark{flex-shrink:0;align-self:center}

/* Grid cards (stories 2-6) */
.tier-grid{
  display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px;
}
.grid-card{
  background:#fff;border:1px solid #e5e7eb;border-left:4px solid #ef4444;
  border-radius:8px;padding:14px 16px;
}
.grid-card:hover{box-shadow:0 2px 10px rgba(0,0,0,0.06)}
.grid-top{display:flex;align-items:center;gap:6px;margin-bottom:6px;font-size:12px;flex-wrap:wrap}
.grid-rank{font-weight:800;color:#9ca3af;font-size:14px}
.grid-card h3{margin:0 0 8px;font-size:0.95em;line-height:1.3}
.grid-card h3 a{color:inherit}
.grid-spark{margin-bottom:6px}
.grid-sources{display:flex;flex-wrap:wrap;gap:3px}

/* Compact rows (stories 7+) */
.tier-compact{display:flex;flex-direction:column;gap:2px}
.compact-row{
  background:#fff;border:1px solid #e5e7eb;border-left:3px solid #9ca3af;
  border-radius:4px;padding:8px 12px;
  display:flex;align-items:center;gap:8px;font-size:13px;
}
.compact-row:hover{background:#f9fafb}
.compact-rank{font-weight:700;color:#9ca3af;min-width:28px}
.compact-status{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.compact-status.active{background:#ef4444}
.compact-status.fading{background:#f59e0b}
.compact-status.gone{background:#9ca3af}
.compact-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:inherit}
.compact-meta{color:#9ca3af;font-size:12px;white-space:nowrap;margin-left:auto}
.src-overflow{color:#9ca3af;font-size:11px;font-style:italic}

/* Silence gaps */
.silence-card{
  background:#fff;border:1px solid #e5e7eb;border-left:4px solid #9333ea;
  border-radius:8px;padding:14px 18px;margin-bottom:10px;
}
.silence-card.silence-right{border-left-color:#dc2626}
.silence-card.silence-left{border-left-color:#2563eb}
.silence-top{display:flex;align-items:center;gap:10px;margin-bottom:6px;flex-wrap:wrap}
.silence-top h3{margin:0;font-size:1.05em}
.silence-top h3 a{color:inherit}
.silence-badge{padding:2px 10px;border-radius:10px;font-size:11px;font-weight:700;color:#fff}
.silence-badge.silence-right{background:#dc2626}
.silence-badge.silence-left{background:#2563eb}
.silence-meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:12px;color:#6b7280;margin-bottom:4px}
.covering-label{font-weight:600}
.src-pill{color:#fff;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:500}
.silence-score{font-size:12px;color:#9ca3af}

/* Framing divergence */
.framing-card{
  background:#fff;border:1px solid #e5e7eb;border-radius:10px;
  padding:18px 22px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,0.04);
}
.framing-header{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.framing-header h3{margin:0;font-size:1.1em;flex:1;min-width:0}
.framing-header h3 a{color:inherit;text-decoration:none}
.framing-header h3 a:hover{text-decoration:underline}
.framing-divergence{
  padding:3px 12px;border-radius:12px;font-size:11px;font-weight:700;color:#fff;white-space:nowrap;flex-shrink:0;
}
.framing-spectrum-wrap{margin-bottom:16px}
.framing-legend{display:flex;gap:16px;margin-bottom:6px;font-size:12px;color:#6b7280;font-weight:600}
.legend-item{display:flex;align-items:center;gap:5px}
.legend-dot{display:inline-block;width:10px;height:10px;border-radius:50%;flex-shrink:0}
.legend-left{background:#3b82f6}
.legend-right{background:#ef4444}
.framing-spectrum{width:100%;max-width:100%}
.framing-columns{display:grid;grid-template-columns:1fr 1fr;gap:16px;overflow:hidden}
.framing-col{min-width:0;overflow:hidden}
.framing-col h4{font-size:12px;font-weight:600;color:#6b7280;margin:0 0 8px;display:flex;align-items:center;gap:6px}
.frame-hl{display:flex;align-items:center;gap:6px;font-size:12px;padding:5px 6px;border-radius:4px;margin-bottom:2px;overflow:hidden}
.frame-hl-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.frame-hl-score{color:#6b7280;font-size:11px;font-weight:700;white-space:nowrap;font-variant-numeric:tabular-nums}
.frame-hl-more{font-size:11px;color:#9ca3af;padding:4px 6px;font-style:italic}

/* Heatmap */
.heatmap-controls{display:flex;align-items:center;gap:6px;margin-bottom:8px;flex-wrap:wrap}
.hm-label{font-size:12px;color:#6b7280;font-weight:600}
.hm-sort{font-size:12px;padding:3px 10px;border:1px solid #d1d5db;border-radius:12px;background:#fff;color:#374151;cursor:pointer;transition:all .15s}
.hm-sort:hover{border-color:#3b82f6;color:#3b82f6}
.hm-sort.active{background:#3b82f6;color:#fff;border-color:#3b82f6}
.heatmap-wrap{overflow-x:auto;margin-top:8px;-webkit-overflow-scrolling:touch}
.heatmap{border-collapse:collapse;font-size:11px;width:100%}
.heatmap th,.heatmap td{padding:3px;text-align:center}
.heatmap .story-col{text-align:left;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-right:8px;font-weight:500}
.day-hdr{writing-mode:vertical-rl;transform:rotate(180deg);font-weight:400;color:#9ca3af;font-size:10px}
.cell{width:18px;height:18px;border-radius:3px;border:1px solid #f0f0f0}
.c0{background:#f3f4f6}
.c1{background:#bfdbfe}
.c2{background:#60a5fa}
.c3{background:#2563eb}
.c4{background:#1e40af}

/* Source matrix */
.matrix-wrap{overflow-x:auto;margin-top:8px;-webkit-overflow-scrolling:touch}
.matrix{border-collapse:collapse;font-size:12px}
.matrix th,.matrix td{padding:4px 8px;text-align:center;border:1px solid #e5e7eb}
.matrix .story-col{text-align:left;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.src-hdr{font-weight:700;writing-mode:vertical-rl;transform:rotate(180deg);font-size:11px}
.mx-cell{min-width:32px}
.mx-cell.filled{background:#dbeafe;font-weight:600}

/* Feed health */
.health-summary{display:flex;gap:10px;margin-bottom:12px;flex-wrap:wrap}
.health-stat{background:#f3f4f6;padding:4px 12px;border-radius:10px;font-size:13px;font-weight:600}
.health-stat.ok{background:#dcfce7;color:#166534}
.health-stat.fail{background:#fee2e2;color:#991b1b}
.health-tbl{border-collapse:collapse;width:100%;font-size:13px}
.health-tbl th,.health-tbl td{border:1px solid #e5e7eb;padding:6px 10px;text-align:left}
.health-tbl th{background:#f3f4f6;font-weight:600}
.health-icon{font-size:16px;text-align:center;width:40px}
.health-icon.ok{color:#16a34a}
.health-icon.fail{color:#dc2626}
.health-fail td{background:#fef2f2}

/* Mobile responsiveness */
@media (max-width:768px){
  body{padding:12px 10px}
  header h1{font-size:1.5em}
  .controls input[type=text]{width:100%}
  .tier-grid{grid-template-columns:1fr}
  .hero-card{flex-direction:column;padding:14px 16px}
  .hero-spark{align-self:stretch}
  .hero-rank{font-size:22px}
  .hero-main h3{font-size:1.1em}
  .framing-columns{grid-template-columns:1fr}
  .compact-row{flex-wrap:wrap;gap:4px}
  .compact-meta{margin-left:0}
  nav{gap:2px}
  .tab{padding:6px 10px;font-size:12px}
  .heatmap .story-col{max-width:120px;font-size:10px}
  .cell{width:14px;height:14px}
  .silence-top{flex-direction:column;align-items:flex-start}
  .framing-top{flex-direction:column;align-items:flex-start}
  .health-tbl{font-size:11px}
  .health-tbl th,.health-tbl td{padding:4px 6px}
}
@media (max-width:480px){
  .tier-grid{gap:8px}
  .grid-card{padding:10px 12px}
  .hero-card{padding:12px}
  .stat-big,.stat{font-size:10px;padding:1px 6px}
}

/* Dark mode — auto (media query) */
@media (prefers-color-scheme:dark){
  body:not(.light-mode){background:#0f0f1a;color:#e5e7eb}
  body:not(.light-mode) nav{border-bottom-color:#2a2a3e}
  body:not(.light-mode) .tab{color:#8888aa} body:not(.light-mode) .tab:hover{color:#e5e7eb} body:not(.light-mode) .tab.active{color:#60a5fa;border-bottom-color:#60a5fa}
  body:not(.light-mode) .controls input[type=text]{background:#1a1a2e;border-color:#2a2a3e;color:#e5e7eb}
  body:not(.light-mode) .controls label{color:#8888aa}
  body:not(.light-mode) .subtitle,body:not(.light-mode) .section-desc,body:not(.light-mode) .meta{color:#8888aa}
  body:not(.light-mode) .stat{background:#1e1e2e;color:#a5b4fc}
  body:not(.light-mode) .story-card{background:#1a1a2e;border-color:#2a2a3e}
  body:not(.light-mode) .story-card:hover{box-shadow:0 2px 12px rgba(0,0,0,0.3)}
  body:not(.light-mode) .card-body h3 a{color:#e5e7eb}
  body:not(.light-mode) a{color:#60a5fa}
  body:not(.light-mode) .c0{background:#1e1e2e;border-color:#2a2a3e}
  body:not(.light-mode) .c1{background:#1e3a5f} body:not(.light-mode) .c2{background:#1d4ed8} body:not(.light-mode) .c3{background:#2563eb} body:not(.light-mode) .c4{background:#3b82f6}
  body:not(.light-mode) .matrix th,body:not(.light-mode) .matrix td{border-color:#2a2a3e}
  body:not(.light-mode) .mx-cell.filled{background:#1e3a5f}
  body:not(.light-mode) .heatmap .story-col a,body:not(.light-mode) .matrix .story-col a{color:#e5e7eb}
  body:not(.light-mode) .silence-card{background:#1a1a2e;border-color:#2a2a3e}
  body:not(.light-mode) .silence-top h3 a{color:#e5e7eb}
  body:not(.light-mode) .silence-meta{color:#8888aa}
  body:not(.light-mode) .silence-score{color:#6b7280}
  body:not(.light-mode) .hero-card{background:#1a1a2e;border-color:#2a2a3e}
  body:not(.light-mode) .hero-card:hover{box-shadow:0 4px 20px rgba(0,0,0,0.4)}
  body:not(.light-mode) .hero-rank{color:#2a2a3e}
  body:not(.light-mode) .hero-main h3 a{color:#e5e7eb}
  body:not(.light-mode) .stat-big{background:#1e1e2e;color:#a5b4fc}
  body:not(.light-mode) .grid-card{background:#1a1a2e;border-color:#2a2a3e}
  body:not(.light-mode) .grid-card:hover{box-shadow:0 2px 12px rgba(0,0,0,0.3)}
  body:not(.light-mode) .grid-card h3 a{color:#e5e7eb}
  body:not(.light-mode) .grid-rank{color:#6b7280}
  body:not(.light-mode) .compact-row{background:#1a1a2e;border-color:#2a2a3e}
  body:not(.light-mode) .compact-row:hover{background:#1e1e2e}
  body:not(.light-mode) .compact-title{color:#e5e7eb}
  body:not(.light-mode) .compact-meta{color:#6b7280}
  body:not(.light-mode) .src-overflow{color:#6b7280}
  body:not(.light-mode) .framing-card{background:#1a1a2e;border-color:#2a2a3e;box-shadow:none}
  body:not(.light-mode) .framing-header h3 a{color:#e5e7eb}
  body:not(.light-mode) .framing-legend{color:#8888aa}
  body:not(.light-mode) .framing-col h4{color:#8888aa}
  body:not(.light-mode) .frame-hl-score{color:#6b7280}
  body:not(.light-mode) .theme-toggle{border-color:#2a2a3e;color:#8888aa}
  body:not(.light-mode) .hm-sort{background:#1a1a2e;border-color:#2a2a3e;color:#8888aa}
  body:not(.light-mode) .hm-sort.active{background:#3b82f6;color:#fff;border-color:#3b82f6}
  body:not(.light-mode) .health-tbl th{background:#1e1e2e}
  body:not(.light-mode) .health-tbl th,body:not(.light-mode) .health-tbl td{border-color:#2a2a3e}
  body:not(.light-mode) .health-fail td{background:#2a1a1a}
  body:not(.light-mode) .health-stat{background:#1e1e2e}
  body:not(.light-mode) .health-stat.ok{background:#052e16;color:#86efac}
  body:not(.light-mode) .health-stat.fail{background:#450a0a;color:#fca5a5}
  body:not(.light-mode) .vel-badge.rising{background:#052e16;color:#86efac}
  body:not(.light-mode) .vel-badge.cooling{background:#451a03;color:#fcd34d}
  body:not(.light-mode) .vel-badge.falling{background:#450a0a;color:#fca5a5}
  body:not(.light-mode) .vel-badge.new{background:#2e1065;color:#c4b5fd}
}

/* Dark mode — manual toggle (class-based) */
body.dark-mode{background:#0f0f1a;color:#e5e7eb}
body.dark-mode nav{border-bottom-color:#2a2a3e}
body.dark-mode .tab{color:#8888aa} body.dark-mode .tab:hover{color:#e5e7eb} body.dark-mode .tab.active{color:#60a5fa;border-bottom-color:#60a5fa}
body.dark-mode .controls input[type=text]{background:#1a1a2e;border-color:#2a2a3e;color:#e5e7eb}
body.dark-mode .controls label{color:#8888aa}
body.dark-mode .subtitle,body.dark-mode .section-desc,body.dark-mode .meta{color:#8888aa}
body.dark-mode .stat{background:#1e1e2e;color:#a5b4fc}
body.dark-mode .story-card{background:#1a1a2e;border-color:#2a2a3e}
body.dark-mode .story-card:hover{box-shadow:0 2px 12px rgba(0,0,0,0.3)}
body.dark-mode .card-body h3 a{color:#e5e7eb}
body.dark-mode a{color:#60a5fa}
body.dark-mode .c0{background:#1e1e2e;border-color:#2a2a3e}
body.dark-mode .c1{background:#1e3a5f} body.dark-mode .c2{background:#1d4ed8} body.dark-mode .c3{background:#2563eb} body.dark-mode .c4{background:#3b82f6}
body.dark-mode .matrix th,body.dark-mode .matrix td{border-color:#2a2a3e}
body.dark-mode .mx-cell.filled{background:#1e3a5f}
body.dark-mode .heatmap .story-col a,body.dark-mode .matrix .story-col a{color:#e5e7eb}
body.dark-mode .silence-card{background:#1a1a2e;border-color:#2a2a3e}
body.dark-mode .silence-top h3 a{color:#e5e7eb}
body.dark-mode .silence-meta{color:#8888aa}
body.dark-mode .silence-score{color:#6b7280}
body.dark-mode .hero-card{background:#1a1a2e;border-color:#2a2a3e}
body.dark-mode .hero-card:hover{box-shadow:0 4px 20px rgba(0,0,0,0.4)}
body.dark-mode .hero-rank{color:#2a2a3e}
body.dark-mode .hero-main h3 a{color:#e5e7eb}
body.dark-mode .stat-big{background:#1e1e2e;color:#a5b4fc}
body.dark-mode .grid-card{background:#1a1a2e;border-color:#2a2a3e}
body.dark-mode .grid-card:hover{box-shadow:0 2px 12px rgba(0,0,0,0.3)}
body.dark-mode .grid-card h3 a{color:#e5e7eb}
body.dark-mode .grid-rank{color:#6b7280}
body.dark-mode .compact-row{background:#1a1a2e;border-color:#2a2a3e}
body.dark-mode .compact-row:hover{background:#1e1e2e}
body.dark-mode .compact-title{color:#e5e7eb}
body.dark-mode .compact-meta{color:#6b7280}
body.dark-mode .src-overflow{color:#6b7280}
body.dark-mode .framing-card{background:#1a1a2e;border-color:#2a2a3e;box-shadow:none}
body.dark-mode .framing-header h3 a{color:#e5e7eb}
body.dark-mode .framing-legend{color:#8888aa}
body.dark-mode .framing-col h4{color:#8888aa}
body.dark-mode .frame-hl-score{color:#6b7280}
body.dark-mode .theme-toggle{border-color:#2a2a3e;color:#8888aa}
body.dark-mode .hm-sort{background:#1a1a2e;border-color:#2a2a3e;color:#8888aa}
body.dark-mode .hm-sort.active{background:#3b82f6;color:#fff;border-color:#3b82f6}
body.dark-mode .health-tbl th{background:#1e1e2e}
body.dark-mode .health-tbl th,body.dark-mode .health-tbl td{border-color:#2a2a3e}
body.dark-mode .health-fail td{background:#2a1a1a}
body.dark-mode .health-stat{background:#1e1e2e}
body.dark-mode .health-stat.ok{background:#052e16;color:#86efac}
body.dark-mode .health-stat.fail{background:#450a0a;color:#fca5a5}
body.dark-mode .vel-badge.rising{background:#052e16;color:#86efac}
body.dark-mode .vel-badge.cooling{background:#451a03;color:#fcd34d}
body.dark-mode .vel-badge.falling{background:#450a0a;color:#fca5a5}
body.dark-mode .vel-badge.new{background:#2e1065;color:#c4b5fd}
"""


# ------------------------------------------------------------------
# JavaScript
# ------------------------------------------------------------------


def _main_js() -> str:
    return """\
(function(){
  // Dark mode toggle
  var toggle=document.getElementById('themeToggle');
  var stored=localStorage.getItem('mw-theme');
  if(stored==='dark'){document.body.classList.add('dark-mode');document.body.classList.remove('light-mode');toggle.innerHTML='&#9788;';}
  else if(stored==='light'){document.body.classList.add('light-mode');document.body.classList.remove('dark-mode');toggle.innerHTML='&#9789;';}
  toggle.addEventListener('click',function(){
    if(document.body.classList.contains('dark-mode')){
      document.body.classList.remove('dark-mode');
      document.body.classList.add('light-mode');
      localStorage.setItem('mw-theme','light');
      toggle.innerHTML='&#9789;';
    }else{
      document.body.classList.remove('light-mode');
      document.body.classList.add('dark-mode');
      localStorage.setItem('mw-theme','dark');
      toggle.innerHTML='&#9788;';
    }
  });

  // Tab switching
  document.querySelectorAll('.tab').forEach(function(btn){
    btn.addEventListener('click',function(){
      document.querySelectorAll('.tab-content').forEach(function(t){t.classList.remove('active')});
      document.querySelectorAll('.tab').forEach(function(b){b.classList.remove('active')});
      document.getElementById('tab-'+btn.dataset.tab).classList.add('active');
      btn.classList.add('active');
    });
  });

  // Search & filter
  var search=document.getElementById('search');
  var multiOnly=document.getElementById('multiOnly');
  function applyFilters(){
    var q=(search.value||'').toLowerCase();
    var mo=multiOnly.checked;
    document.querySelectorAll('.story-card').forEach(function(card){
      var title=card.textContent.toLowerCase();
      var sources=parseInt(card.getAttribute('data-sources')||'0');
      var show=true;
      if(q && title.indexOf(q)===-1) show=false;
      if(mo && sources<2) show=false;
      card.style.display=show?'':'none';
    });
  }
  search.addEventListener('input',applyFilters);
  multiOnly.addEventListener('change',applyFilters);

  // Heatmap sort
  document.querySelectorAll('.hm-sort').forEach(function(btn){
    btn.addEventListener('click',function(){
      document.querySelectorAll('.hm-sort').forEach(function(b){b.classList.remove('active')});
      btn.classList.add('active');
      var tbody=document.querySelector('.heatmap tbody');
      if(!tbody) return;
      var rows=Array.from(tbody.querySelectorAll('tr'));
      var key=btn.dataset.sort;
      rows.sort(function(a,b){
        var av=parseFloat(a.getAttribute('data-'+key)||'0');
        var bv=parseFloat(b.getAttribute('data-'+key)||'0');
        return bv-av;
      });
      rows.forEach(function(r){tbody.appendChild(r)});
    });
  });
})();"""
