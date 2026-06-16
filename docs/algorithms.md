# Memory Whole Clustering and Story State Algorithms

This document explains how Memory Whole clusters headlines into stories and assigns story states. It is written to match the current implementation in `clustering.py`, `tracker.py`, and `db.py`.

## 1. Pipeline overview

Memory Whole uses a multi-stage pipeline:

1. `fetcher.py` reads RSS feeds and writes every headline into SQLite.
2. `tracker.py` reads recent headlines and clusters them into stories.
3. `db.py` refreshes story metadata, scores stories, and updates lifecycle state.
4. `dashboard.py` renders the ranked stories and state-aware dashboard.

The key algorithmic work happens in two areas:

- **Clustering** — grouping headlines into coherent story candidate clusters.
- **Story lifecycle** — matching clusters to persistent stories, scoring them, and assigning `active`, `fading`, or `gone`.

## 2. Clustering algorithm

Clustering is implemented in `clustering.py` and used by `tracker.py`.

### 2.1 Record preparation

Each headline becomes a `_ClusterRecord` containing:

- `title` — headline text
- `summary` — fetched feed summary text
- `text` — combined title and summary
- `source` — feed name
- `link` — headline URL
- `date` — published or seen date
- `normalized_title` — lowercase title with markup removed and prefix noise stripped

Normalization uses `_normalize_title`, which removes leading editorial prefixes like `Breaking:`, `Exclusive —`, `Update:`, `Analysis:`, and strips punctuation.

### 2.2 Exact duplicate bucketing

The first clustering pass groups headlines with identical normalized titles.

- Records with the same `normalized_title` are unioned immediately.
- This is a strict grouping step that ensures exact or near-exact titles stay together even when semantic similarity is not strong.

### 2.3 Vectorization

Headline texts are vectorized using one of two methods:

- `sentence-transformers` embeddings if the package is installed and available.
- Otherwise, TF-IDF with n-grams up to bigrams.

The implementation uses `_vectorize_texts`:

- `TfidfVectorizer(max_features=5000, ngram_range=(1, 2), stop_words="english")`
- optional Truncated SVD if the feature matrix is large enough
- l2 normalization on the resulting vectors

### 2.4 Candidate generation

Once all texts are vectorized, the system finds nearest neighbors and decides whether to link them.

Relevant thresholds in `ClusterSettings`:

- `lexical_threshold = 92.0`
- `semantic_threshold_embeddings = 0.78`
- `semantic_threshold_tfidf = 0.55`
- `semantic_lexical_threshold = 30.0`
- `neighbor_k = 6`

For each headline pair among the nearest neighbors:

- compute semantic similarity score using cosine similarity on the vectorized representations
- compute lexical similarity using `SequenceMatcher` or RapidFuzz if installed

A headline pair is linked if either:

- lexical similarity is above `lexical_threshold` (a near-title-match link), or
- semantic similarity is above the semantic threshold and lexical similarity is above `semantic_lexical_threshold`

This is the conservative gating rule that reduces broad semantic chaining. It means semantic similarity alone is not enough; there must also be a base level of lexical overlap.

### 2.5 Connected components and refinement

Headline similarity links are collected into connected components via union-find. Each connected component is then refined.

- If HDBSCAN is installed, the component is refined with `HDBSCAN(min_cluster_size=..., metric="euclidean")`.
- Otherwise, the component is refined with DBSCAN:
  - `eps = 0.32` for embeddings
  - `eps = 0.45` for TF-IDF
  - `min_samples = max(2, min_cluster_size)`

Refinement splits larger candidate components into smaller clusters and isolates noise.

### 2.6 Why hybrid clustering?

The hybrid pipeline exists to balance three goals:

- **Precision for exact duplicates:** exact title matches should always cluster.
- **Semantic grouping:** similar headlines that do not share exact words should still join when meaningful.
- **Noise control:** avoid transitive grouping of loosely related headlines through weak semantic links.

The lexical gating threshold added in the current implementation is a key safeguard for the last point.

## 3. Tracking headlines into persistent stories

`tracker.py` is responsible for mapping clusters of recent headlines into existing stories.

### 3.1 Input to tracker

`tracker.track_stories(conn, cluster_days=14)` fetches headlines seen in the last `cluster_days`.

### 3.2 Cluster matching behavior

For each cluster returned by `cluster_headlines`:

- if no headline in the cluster has an existing story assignment, a new story row is created
- if exactly one story is already present among the cluster's headlines, the cluster is assigned to that story
- if multiple existing stories are present, those stories are merged

The merge logic uses `db.merge_stories`, which keeps the lowest story id and reassigns all headlines from absorbed stories into the survivor story. It also deletes any orphaned daily snapshots.

### 3.3 Story creation

When a brand-new story is created, `tracker.py` picks a representative headline from the cluster:

- the cluster headline with the longest title is used as the initial story title
- `first_seen` is the earliest headline date in the cluster
- `last_seen` is the latest headline date in the cluster

After all clusters are assigned, `tracker.py` refreshes metadata for every story and updates daily snapshots.

## 4. Story metadata and scoring

`db.refresh_story()` is the core story scoring routine.

### 4.1 Representative title selection

The representative title is selected from the story's headlines as the headline with:

1. newest `last_seen`
2. longest title among equally recent headlines

This both keeps stories up to date and maintains a descriptive title.

### 4.2 Score formula

The story importance score is computed from:

- `source_count` — distinct sources covering the story
- `mentions` — total headline count for the story
- `days_active` — the span from first to last seen
- `velocity` — `mentions / days_active`
- recent coverage in the past 7 days

The score formula is:

```python
effective_source_count = max(recent_source_count, 1) + 0.1 * source_count
effective_mentions = recent_mentions + 0.05 * mentions
base = effective_source_count * (1.0 + math.log1p(effective_mentions))

capped_days = min(days_active, 14)
persistence = 1.0 + capped_days / 7.0

freshness = min(1.0, recent_mentions / max(1, mentions))
recency_factor = 0.5 + 0.5 * freshness

vel_factor = 1.0 + min(3.0, velocity) / 3.0

score = base * persistence * recency_factor * vel_factor
```

This formula prioritizes:

- multi-source coverage
- recent source activity
- mention volume
- persistence up to two weeks
- velocity of headline accumulation

### 4.3 Daily snapshots and peak tracking

`db.update_daily_snapshots()` writes a snapshot for today if the story has headlines with `last_seen == today`.

For each story, the code also recalculates:

- `peak_date` — the date with the highest source coverage
- `peak_source_count` — the maximum number of sources seen on any day

This peak data is used for disappeared-story reporting.

## 5. Story state lifecycle

Story state is derived from `last_seen` relative to `today`.

`db.update_statuses()` implements the lifecycle rules:

- `active`: `last_seen >= date(today, '-1 day')`
- `fading`: `last_seen < date(today, '-1 day')` and `last_seen >= date(today, '-4 days')`
- `gone`: `last_seen < date(today, '-4 days')`

These values are controlled by `gone_days` and can be configured when calling `update_statuses()`.

### 5.1 State meaning

- `active` — story has at least one headline within the last 24 hours
- `fading` — story has not had a headline in the last 24 hours but is still within the short-term window
- `gone` — story has not been covered recently enough to remain in current coverage

The dashboard uses these states to separate top active stories from disappeared stories and to compute velocity badges.

## 6. Querying stories for output

The dashboard relies on a few core query helpers:

- `db.get_top_stories(limit=30)`
  - returns active and fading stories ordered by `importance_score`
- `db.get_disappeared_stories(min_peak_sources=1)`
  - returns fading or gone stories with a non-zero peak source count, ordered by peak and score
- `db.get_all_stories()`
  - returns every story ordered by importance score

These query functions drive the main sections of the output dashboard and JSON export.

## 7. How the dashboard uses the results

### 7.1 Top Stories

`dashboard.py` renders:

- a hero card for story #1
- a grid for stories #2–6
- a compact list for the remainder

Stories are scored by importance and enriched with:

- velocity badges (`new`, `rising`, `steady`, `cooling`, `falling`)
- sparkline charts from daily source counts
- source pills showing which outlets covered the story

### 7.2 Disappeared

The disappeared section is populated from `get_disappeared_stories()` and highlights stories that were large enough to matter but are now fading or gone.

### 7.3 Story detail pages

Story detail pages show:

- full headline list
- source-by-headline badges
- a large sparkline of coverage over 60 days
- daily snapshot table for story coverage

## 8. Current implementation notes

### 8.1 Conservative semantic linking

The clustering pipeline now requires a small lexical overlap for semantic links, so broadly related headlines are less likely to chain into the same giant story.

This is the main adjustment used to reduce overbroad story grouping.

### 8.2 Why stories can still be broad

A story can still grow large when:

- many headlines share a strong semantic relationship
- the story is repeatedly covered by many sources over multiple days
- the story title is updated frequently while the core subject persists

Large, long-running stories are expected, but the current implementation uses a combination of recent coverage and lexical gating to keep the top story set meaningful.

## 9. Summary

Memory Whole clusters headlines with a hybrid approach:

- strict normalized-title bucket for exact duplicates
- semantic nearest-neighbor links for related headlines
- lexical gates to avoid overly broad transitive clustering
- HDBSCAN/DBSCAN refinement within each candidate component

Persistent story metadata is then refreshed and scored by:

- number of sources
- mention volume
- persistence over time
- recent activity
- velocity of coverage

Story lifecycle state is assigned by `last_seen` recency, producing `active`, `fading`, and `gone` stories for the dashboard.
