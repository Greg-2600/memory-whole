<p align="center">
  <strong>⛰️ Memory Mountain</strong><br>
  <em>Watch the news rise. Watch it disappear.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/docker-ready-2496ed?logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License">
  <img src="https://img.shields.io/badge/feeds-9%20sources-orange" alt="Feeds">
</p>

---

Memory Mountain is a self-hosted news intelligence tool. It fetches headlines from across the political spectrum, clusters them into stories using machine learning, and tracks how those stories rise, persist, and vanish from the news cycle — all served as a single-page dashboard you can run with one command.

## Why?

Every news outlet decides what to amplify and what to bury. Memory Mountain doesn't editorialize — it **remembers**. It watches 9 RSS feeds spanning left, right, and center, then answers questions like:

- Which stories are **every** outlet covering right now?
- What was huge yesterday but **disappeared** today?
- Which outlets are covering something that **nobody else** is?
- How long did a story stay in the news before it was gone?

## How It Works

```
   RSS Feeds (9 sources)
         │
         ▼
   ┌───────────┐     ┌──────────┐     ┌──────────────┐
   │  Fetcher   │────▶│ SQLite   │────▶│   Tracker    │
   │ feedparser │     │ every    │     │ TF-IDF +     │
   │            │     │ headline │     │ DBSCAN       │
   └───────────┘     └──────────┘     └──────┬───────┘
                                              │
                       story lifecycle:       │
                       active → fading → gone │
                                              ▼
                                     ┌────────────────┐
                                     │   Dashboard    │
                                     │ 4-tab HTML UI  │
                                     └────────────────┘
```

**Pipeline stages:**

| Module | Role |
|---|---|
| `fetcher.py` | Pulls RSS/Atom feeds, normalizes entries, upserts into SQLite |
| `db.py` | Schema, queries, daily snapshots — the permanent memory |
| `tracker.py` | Clusters headlines via TF-IDF + DBSCAN, manages story lifecycle |
| `dashboard.py` | Generates the single-page HTML dashboard with 4 views |
| `utils.py` | Slugify, summarize, favicon lookups, color mappings |
| `rss_reader.py` | CLI entry point that orchestrates the full pipeline |

**Dashboard tabs:**

| Tab | What it shows |
|---|---|
| **Top Stories** | Highest-importance active stories, ranked by cross-source coverage |
| **Disappeared** | Stories that were big but dropped off — the memory mountain in action |
| **Timeline** | Heatmap of story coverage over time |
| **Sources** | Matrix showing which outlets covered which stories |

## Quick Start

### Docker (recommended)

```bash
git clone https://github.com/you/memory_mountain.git
cd memory_mountain
cp feeds.example.yaml feeds.yaml   # edit to customize sources
./scripts/docker_up.sh
```

Open **http://localhost:4747** — that's it. The container fetches feeds, clusters stories, builds the dashboard, and serves it. A cron job inside the container re-runs the pipeline every 6 hours.

### Local

```bash
python -m pip install -r requirements.txt
python rss_reader.py --max-items 250     # fetch + cluster + dashboard
python -m http.server 4747 --directory output
```

## Usage

```bash
# Full pipeline: fetch → cluster → dashboard
python rss_reader.py --max-items 250

# Dashboard only (skip fetching, use existing DB)
python rss_reader.py --dashboard-only

# Import existing markdown digests into the DB
python rss_reader.py --import-markdown

# Check current status
python rss_reader.py --status

# Backpopulate historical data from feeds
python rss_reader.py --backpopulate --max-items 500
```

### Legacy calendar mode

The original clustering workflow is still supported:

```bash
# Cluster from markdown files and generate calendar artifacts
python rss_reader.py --detect-important --from-markdown

# Only show multi-source stories
python rss_reader.py --detect-important --from-markdown --show-only-multi
```

### CLI Reference

| Flag | Description |
|---|---|
| `--config` | Path to YAML config (default: `feeds.yaml`) |
| `--output-dir` | Override output directory |
| `--max-items` | Max items fetched per feed |
| `--fetch-only` | Fetch and store headlines, skip tracking/dashboard |
| `--dashboard-only` | Regenerate dashboard from existing DB |
| `--import-markdown` | Import markdown digests into the database |
| `--status` | Print current DB stats and exit |
| `--backpopulate` | Generate historical daily files from feed archives |
| `--detect-important` | Run multi-source detection (legacy calendar mode) |
| `--from-markdown` | Load entries from markdown instead of live feeds |
| `--publish` | Write artifacts to the output directory |
| `--watch` | Watch for markdown changes and auto-regenerate |
| `--show-only-multi` | Show only multi-source stories in calendar output |

## Configuration

Edit `feeds.yaml` to add or remove sources (see `feeds.example.yaml` for a starting template):

```yaml
feeds:
  - name: CNN Top Stories
    url: https://rss.cnn.com/rss/edition.rss
  - name: FOX News Latest
    url: https://moxie.foxnews.com/google-publisher/latest.xml
  - name: NPR News
    url: https://feeds.npr.org/1001/rss.xml
  # ... add any RSS/Atom feed

settings:
  output_dir: output
  max_items_per_feed: 250
  merge_all_sources: true
  merged_filename: "daily-news-{date}.md"
  daily_title: "Daily RSS Digest - {date}"
  write_individual_feeds: false
  summary_max_sentences: 6
  summary_max_words: 300
```

## Docker

### Compose (recommended)

```bash
./scripts/docker_up.sh          # build + start on port 4747
./scripts/docker_down.sh        # stop (output volume preserved)
```

The named volume `mm_output` persists all generated data across container rebuilds.

### Force regeneration

```bash
FORCE_REGEN=1 ./scripts/docker_up.sh
```

When `FORCE_REGEN=1`, the container re-runs the full pipeline on startup regardless of existing data.

### Healthcheck

The container includes a healthcheck against `http://localhost:4747/calendar.html`:

```bash
docker compose ps                    # shows health status
```

### Run without Compose

```bash
# Dev mode — mount local output/ for live changes
docker run --rm -p 4747:4747 -v "$(pwd)/output:/app/output" memory_mountain:latest

# Force regen
docker run --rm -e FORCE_REGEN=1 -p 4747:4747 -v "$(pwd)/output:/app/output" memory_mountain:latest
```

## Output

All artifacts are written to `output/`:

| File | Description |
|---|---|
| `memory_mountain.db` | SQLite database — every headline ever fetched  |
| `index.html` | Main dashboard (4-tab UI) |
| `story_*.html` | Per-story detail pages with sparkline charts |
| `daily-news-YYYY-MM-DD.md` | Daily merged digest (Markdown) |
| `calendar.html` | Legacy calendar view |
| `calendar_event_*.html` | Per-event detail pages (legacy mode) |

## Project Structure

```
memory_mountain/
├── rss_reader.py           # CLI entry point
├── fetcher.py              # RSS fetch + normalize + store
├── db.py                   # SQLite schema, queries, snapshots
├── tracker.py              # TF-IDF + DBSCAN clustering, lifecycle
├── dashboard.py            # HTML dashboard generator (4 tabs)
├── utils.py                # Shared helpers
├── feeds.yaml              # Feed configuration
├── feeds.example.yaml      # Example config for new users
├── Dockerfile              # Container image
├── docker-compose.yml      # One-command deployment
├── scripts/
│   ├── docker_up.sh        # Start via Compose
│   ├── docker_down.sh      # Stop via Compose
│   ├── docker_entrypoint.sh
│   ├── container_cron      # Cron schedule for in-container runs
│   ├── run.sh              # Helper script for common operations
│   └── ...
├── tests/                  # Test suite (pytest)
├── output/                 # Generated artifacts (gitignored)
├── requirements.txt        # Runtime dependencies
└── requirements-dev.txt    # Dev/lint dependencies
```

## Development

```bash
python -m pip install -r requirements-dev.txt

# Format
python -m black .
python -m ruff format .

# Lint
python -m ruff check .

# Test
python -m pytest tests/ -v
```

## Requirements

- **Python 3.10+**
- **Runtime:** `feedparser`, `PyYAML`
- **Clustering:** `scikit-learn`, `numpy` (installed automatically in Docker)
- **Docker** (optional, recommended for production)

## Troubleshooting

| Problem | Solution |
|---|---|
| Empty/sparse output | Feeds may not retain history — increase `--max-items` or check the feed URL directly |
| Config errors | Ensure `feeds.yaml` exists with a non-empty `feeds` list |
| Clustering fails | Install `scikit-learn` and `numpy` (`pip install scikit-learn numpy`) |
| Container unhealthy | Check logs: `docker compose logs memory_mountain` |
| Port conflict | Change the port mapping in `docker-compose.yml` or pass `-p XXXX:4747` |

---

<p align="center">
  <em>What the news doesn't want you to remember, Memory Mountain never forgets.</em>
</p>
