"""Generate the Memory Mountain HTML dashboard.

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
from silence import detect_silence
from utils import color_for_source, lean_for_source, slugify


def generate(
    conn: sqlite3.Connection,
    output_dir: Path,
    window_days: int = 30,
    config: dict | None = None,
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
        min_src = int(silence_cfg.get("min_sources_covering", 2))
        lookback = int(silence_cfg.get("lookback_days", 7))
        silence_gaps = detect_silence(
            conn, min_sources_covering=min_src, lookback_days=lookback
        )
    else:
        silence_gaps = []

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

    # Write per-story detail pages
    for s in all_stories:
        _write_story_page(conn, s, output_dir, sparklines.get(s["id"], []))

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


def _story_card(story: sqlite3.Row, spark_data: list[int], ghost: bool = False) -> str:
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
    slug = slugify(str(sid))

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

    return f"""\
<div class="story-card" style="border-left-color:{border_color};{opacity}" data-sources="{sources}" data-score="{score:.2f}">
  <div class="card-top">
    <div class="sparkline-box">{sparkline}</div>
    <div class="card-body">
      <h3><a href="story_{sid}.html">{title}</a></h3>
      <div class="meta">
        {badge}
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
<title>{title_esc} — Memory Mountain</title>
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
) -> str:
    # Top Stories cards
    top_cards = (
        "\n".join(_story_card(s, sparklines.get(s["id"], [])) for s in top)
        or '<p class="empty">No active stories yet. Run a fetch first.</p>'
    )

    # Disappeared cards
    dis_cards = (
        "\n".join(
            _story_card(s, sparklines.get(s["id"], []), ghost=True) for s in disappeared
        )
        or '<p class="empty">No disappeared stories detected yet.</p>'
    )

    # Timeline heatmap
    heatmap = _render_heatmap(all_stories, sparklines, today, window_days)

    # Source matrix
    matrix = _render_source_matrix(all_sources, source_matrix, matrix_stories)

    # Silence gaps
    silence_html = _render_silence_gaps(silence_gaps or [])

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
<title>Memory Mountain</title>
<style>
{_main_css()}
</style>
</head>
<body>

<header>
  <h1>Memory Mountain</h1>
  <p class="subtitle">
    {html_mod.escape(now_str)} &middot;
    {total_hl} headlines &middot; {total_st} stories &middot;
    {active_ct} active &middot; {gone_ct} fading/gone
    {f' &middot; {silence_ct} silence gap{"s" if silence_ct != 1 else ""}' if silence_ct else ''}
  </p>
</header>

<nav>
  <button class="tab active" data-tab="top">Top Stories</button>
  <button class="tab" data-tab="disappeared">Disappeared</button>
  <button class="tab" data-tab="silence">Silence{f' ({silence_ct})' if silence_ct else ''}</button>
  <button class="tab" data-tab="timeline">Timeline</button>
  <button class="tab" data-tab="sources">Sources</button>
</nav>

<div class="controls">
  <input type="text" id="search" placeholder="Search stories\u2026">
  <label><input type="checkbox" id="multiOnly"> Multi-source only</label>
</div>

<section id="tab-top" class="tab-content active">
  <h2>Top Stories</h2>
  <p class="section-desc">Ranked by importance: cross-source coverage \u00d7 persistence \u00d7 velocity.</p>
  <div class="card-list">{top_cards}</div>
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

    lines = [
        '<div class="heatmap-wrap"><table class="heatmap"><thead><tr><th class="story-col"></th>'
    ]
    for d in days:
        label = d.strftime("%d")
        lines.append(f'<th class="day-hdr" title="{d.isoformat()}">{label}</th>')
    lines.append("</tr></thead><tbody>")

    for s in shown:
        data = sparklines.get(s["id"], [0] * window_days)
        title_esc = html_mod.escape((s["representative_title"] or "")[:50])
        sid = s["id"]
        lines.append(
            f'<tr><td class="story-col"><a href="story_{sid}.html" title="{html_mod.escape(s["representative_title"] or "")}">{title_esc}</a></td>'
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
# CSS
# ------------------------------------------------------------------


def _main_css() -> str:
    return """\
*{box-sizing:border-box}
body{
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif;
  max-width:1200px;margin:0 auto;padding:20px 24px;
  color:#1a1a2e;background:#fafbfc;line-height:1.5;
}
a{color:#2563eb;text-decoration:none} a:hover{text-decoration:underline}
header{margin-bottom:20px}
header h1{
  font-family:Georgia,'Times New Roman',serif;
  font-size:2em;margin:0 0 4px;letter-spacing:-0.5px;
}
.subtitle{color:#6b7280;font-size:13px;margin:0}

/* Tabs */
nav{display:flex;gap:4px;border-bottom:2px solid #e5e7eb;margin-bottom:16px;padding-bottom:0}
.tab{
  background:none;border:none;padding:8px 16px;cursor:pointer;
  font-size:14px;font-weight:600;color:#6b7280;border-bottom:2px solid transparent;
  margin-bottom:-2px;transition:color .15s,border-color .15s;
}
.tab:hover{color:#1a1a2e}
.tab.active{color:#2563eb;border-bottom-color:#2563eb}
.tab-content{display:none} .tab-content.active{display:block}

/* Controls */
.controls{display:flex;gap:12px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.controls input[type=text]{
  padding:6px 12px;border:1px solid #d1d5db;border-radius:6px;
  font-size:13px;width:240px;
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

/* Silence gaps */
.silence-card{
  background:#fff;border:1px solid #e5e7eb;border-left:4px solid #9333ea;
  border-radius:8px;padding:14px 18px;margin-bottom:10px;
}
.silence-card.silence-right{border-left-color:#dc2626}
.silence-card.silence-left{border-left-color:#2563eb}
.silence-top{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.silence-top h3{margin:0;font-size:1.05em}
.silence-top h3 a{color:inherit}
.silence-badge{padding:2px 10px;border-radius:10px;font-size:11px;font-weight:700;color:#fff}
.silence-badge.silence-right{background:#dc2626}
.silence-badge.silence-left{background:#2563eb}
.silence-meta{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:12px;color:#6b7280;margin-bottom:4px}
.covering-label{font-weight:600}
.src-pill{color:#fff;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:500}
.silence-score{font-size:12px;color:#9ca3af}

/* Heatmap */
.heatmap-wrap{overflow-x:auto;margin-top:8px}
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
.matrix-wrap{overflow-x:auto;margin-top:8px}
.matrix{border-collapse:collapse;font-size:12px}
.matrix th,.matrix td{padding:4px 8px;text-align:center;border:1px solid #e5e7eb}
.matrix .story-col{text-align:left;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.src-hdr{font-weight:700;writing-mode:vertical-rl;transform:rotate(180deg);font-size:11px}
.mx-cell{min-width:32px}
.mx-cell.filled{background:#dbeafe;font-weight:600}

/* Dark mode */
@media (prefers-color-scheme:dark){
  body{background:#0f0f1a;color:#e5e7eb}
  nav{border-bottom-color:#2a2a3e}
  .tab{color:#8888aa} .tab:hover{color:#e5e7eb} .tab.active{color:#60a5fa;border-bottom-color:#60a5fa}
  .controls input[type=text]{background:#1a1a2e;border-color:#2a2a3e;color:#e5e7eb}
  .controls label{color:#8888aa}
  .subtitle,.section-desc,.meta{color:#8888aa}
  .stat{background:#1e1e2e;color:#a5b4fc}
  .story-card{background:#1a1a2e;border-color:#2a2a3e}
  .story-card:hover{box-shadow:0 2px 12px rgba(0,0,0,0.3)}
  .card-body h3 a{color:#e5e7eb}
  a{color:#60a5fa}
  .c0{background:#1e1e2e;border-color:#2a2a3e}
  .c1{background:#1e3a5f} .c2{background:#1d4ed8} .c3{background:#2563eb} .c4{background:#3b82f6}
  .matrix th,.matrix td{border-color:#2a2a3e}
  .mx-cell.filled{background:#1e3a5f}
  .heatmap .story-col a,.matrix .story-col a{color:#e5e7eb}
  .silence-card{background:#1a1a2e;border-color:#2a2a3e}
  .silence-top h3 a{color:#e5e7eb}
  .silence-meta{color:#8888aa}
  .silence-score{color:#6b7280}
}
"""


# ------------------------------------------------------------------
# JavaScript
# ------------------------------------------------------------------


def _main_js() -> str:
    return """\
(function(){
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
})();
"""
