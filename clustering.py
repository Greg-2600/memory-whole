"""Hybrid story clustering helpers.

The pipeline is intentionally layered:
- lexical normalization and exact-title bucketing
- semantic candidate search (Faiss when available, otherwise nearest neighbors)
- RapidFuzz scoring for near-duplicate headlines
- HDBSCAN refinement when available, with DBSCAN fallback

This keeps the clustering deterministic and cheap for the common case while
allowing semantic matching when better text models are installed.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Mapping, Sequence

import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import normalize

from utils import first_non_empty, item_datetime, strip_html

try:
    from rapidfuzz import fuzz as rapidfuzz_fuzz
    from rapidfuzz import utils as rapidfuzz_utils

    _HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover - optional dependency
    rapidfuzz_fuzz = None
    rapidfuzz_utils = None
    _HAS_RAPIDFUZZ = False

try:
    from sentence_transformers import SentenceTransformer

    _HAS_SENTENCE_TRANSFORMERS = True
except ImportError:  # pragma: no cover - optional dependency
    SentenceTransformer = None
    _HAS_SENTENCE_TRANSFORMERS = False

try:
    import hdbscan as _hdbscan

    _HAS_HDBSCAN = True
except ImportError:  # pragma: no cover - optional dependency
    _hdbscan = None
    _HAS_HDBSCAN = False


def _safe_import_faiss() -> tuple[Any | None, bool]:
    spec = importlib.util.find_spec("faiss")
    if spec is None:
        return None, False

    try:
        faiss_module = importlib.import_module("faiss")
        return faiss_module, True
    except ImportError:
        return None, False


faiss, _HAS_FAISS = _safe_import_faiss()


@dataclass(frozen=True)
class ClusterSettings:
    """Tunable thresholds for the hybrid clustering pipeline."""

    min_cluster_size: int = 2
    lexical_threshold: float = 92.0
    semantic_threshold_embeddings: float = 0.78
    semantic_threshold_tfidf: float = 0.55
    semantic_lexical_threshold: float = 30.0
    neighbor_k: int = 6
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"


@dataclass(frozen=True)
class _ClusterRecord:
    title: str
    text: str
    source: str
    link: str
    date: str | None
    normalized_title: str


def cluster_headlines(
    headlines: Sequence[Mapping[str, Any]],
    min_cluster_size: int = 2,
    settings: ClusterSettings | None = None,
) -> list[list[int]]:
    """Cluster SQLite headline rows by similarity and return index groups."""
    records = [_record_from_headline(row) for row in headlines]
    return _cluster_records(
        records, min_cluster_size=min_cluster_size, settings=settings
    )


def cluster_entries(
    entries: Sequence[Mapping[str, Any]],
    min_cluster_size: int = 2,
    settings: ClusterSettings | None = None,
) -> list[list[int]]:
    """Cluster feed entry dictionaries and return index groups."""
    records = [_record_from_entry(entry) for entry in entries]
    return _cluster_records(
        records, min_cluster_size=min_cluster_size, settings=settings
    )


def _record_from_headline(row: Mapping[str, Any]) -> _ClusterRecord:
    title = first_non_empty(_mapping_value(row, "title"), "Untitled")
    summary = first_non_empty(_mapping_value(row, "summary"), "")
    text = _combine_text(title, summary)
    source = first_non_empty(
        _mapping_value(row, "source"),
        _mapping_value(row, "feed"),
        "",
    )
    link = first_non_empty(_mapping_value(row, "url"), "")
    date = first_non_empty(
        _mapping_value(row, "published_at"),
        _mapping_value(row, "first_seen"),
        _mapping_value(row, "last_seen"),
        "",
    )
    return _ClusterRecord(
        title=title,
        text=text,
        source=source,
        link=link,
        date=date or None,
        normalized_title=_normalize_title(title),
    )


def _record_from_entry(entry: Mapping[str, Any]) -> _ClusterRecord:
    title = first_non_empty(_mapping_value(entry, "title"), "Untitled")
    content_value = ""
    content_items = _mapping_value(entry, "content")
    if isinstance(content_items, list) and content_items:
        first_content = content_items[0]
        if isinstance(first_content, dict):
            content_value = first_non_empty(first_content.get("value"), "")
    summary = first_non_empty(
        content_value,
        _mapping_value(entry, "summary"),
        _mapping_value(entry, "description"),
        "",
    )
    text = _combine_text(title, summary)
    source = first_non_empty(
        _mapping_value(entry, "_feed_name"),
        _mapping_value(entry, "_feed_url"),
        _mapping_value(entry, "source"),
        "",
    )
    link = first_non_empty(_mapping_value(entry, "link"), "")
    dt_item = item_datetime(dict(entry))
    date = (
        dt_item.date().isoformat()
        if dt_item
        else first_non_empty(_mapping_value(entry, "date"), "")
    )
    return _ClusterRecord(
        title=title,
        text=text,
        source=source,
        link=link,
        date=date or None,
        normalized_title=_normalize_title(title),
    )


def _cluster_records(
    records: Sequence[_ClusterRecord],
    min_cluster_size: int = 2,
    settings: ClusterSettings | None = None,
) -> list[list[int]]:
    if not records:
        return []

    cfg = settings or ClusterSettings(min_cluster_size=min_cluster_size)
    texts = [record.text for record in records]
    vectors, vector_mode = _vectorize_texts(texts, cfg)

    components = _candidate_components(records, vectors, vector_mode, cfg)
    clusters: list[list[int]] = []
    for component in components:
        clusters.extend(_split_component(component, vectors, vector_mode, cfg))

    clusters = [sorted(group) for group in clusters if group]
    clusters.sort(key=lambda group: (group[0], len(group)))
    return clusters


def _vectorize_texts(
    texts: Sequence[str],
    settings: ClusterSettings,
) -> tuple[np.ndarray, str]:
    dense_embeddings = _sentence_transformer_embeddings(texts, settings)
    if dense_embeddings is not None:
        dense_embeddings = np.asarray(dense_embeddings, dtype=np.float32)
        if dense_embeddings.ndim == 1:
            dense_embeddings = dense_embeddings.reshape(1, -1)
        dense_embeddings = normalize(dense_embeddings, norm="l2")
        return dense_embeddings, "embeddings"

    vectorizer = TfidfVectorizer(
        max_features=5000, ngram_range=(1, 2), stop_words="english"
    )
    sparse = vectorizer.fit_transform(texts)

    if sparse.shape[0] < 2 or sparse.shape[1] < 2:
        dense = sparse.toarray()
    else:
        n_components = min(
            100,
            max(1, sparse.shape[1] - 1),
            max(1, sparse.shape[0] - 1),
        )
        if n_components > 1:
            try:
                dense = TruncatedSVD(n_components=n_components).fit_transform(sparse)
            except (RuntimeError, TypeError, ValueError):
                dense = sparse.toarray()
        else:
            dense = sparse.toarray()

    dense = np.asarray(dense, dtype=np.float32)
    if dense.ndim == 1:
        dense = dense.reshape(-1, 1)
    dense = normalize(dense, norm="l2")
    return dense, "tfidf"


@lru_cache(maxsize=1)
def _load_sentence_transformer(model_name: str) -> Any | None:
    if not _HAS_SENTENCE_TRANSFORMERS:
        return None
    try:
        return SentenceTransformer(model_name)
    except Exception:  # pragma: no cover - model download/runtime dependent
        return None


def _sentence_transformer_embeddings(
    texts: Sequence[str],
    settings: ClusterSettings,
) -> np.ndarray | None:
    model_name = os.getenv("MW_CLUSTER_MODEL", settings.embedding_model).strip()
    if not model_name:
        return None
    model = _load_sentence_transformer(model_name)
    if model is None:
        return None
    try:
        embeddings = model.encode(
            list(texts),
            show_progress_bar=False,
            normalize_embeddings=True,
        )
    except Exception:  # pragma: no cover - model download/runtime dependent
        return None
    return np.asarray(embeddings)


def _candidate_components(
    records: Sequence[_ClusterRecord],
    vectors: np.ndarray,
    vector_mode: str,
    settings: ClusterSettings,
) -> list[list[int]]:
    union_find = _UnionFind(len(records))

    # Exact/normalized-title duplicates should always stay together.
    title_buckets: dict[str, list[int]] = {}
    for idx, record in enumerate(records):
        if record.normalized_title:
            title_buckets.setdefault(record.normalized_title, []).append(idx)
    for bucket in title_buckets.values():
        if len(bucket) > 1:
            first = bucket[0]
            for idx in bucket[1:]:
                union_find.union(first, idx)

    semantic_threshold = (
        settings.semantic_threshold_embeddings
        if vector_mode == "embeddings"
        else settings.semantic_threshold_tfidf
    )

    for left, right, score in _nearest_neighbor_pairs(vectors, settings.neighbor_k):
        lexical = _lexical_similarity(records[left].title, records[right].title)
        if (
            score >= semantic_threshold
            and lexical >= settings.semantic_lexical_threshold
        ) or lexical >= settings.lexical_threshold:
            union_find.union(left, right)

    grouped: dict[int, list[int]] = {}
    for idx in range(len(records)):
        grouped.setdefault(union_find.find(idx), []).append(idx)
    return list(grouped.values())


def _split_component(
    component: Sequence[int],
    vectors: np.ndarray,
    vector_mode: str,
    settings: ClusterSettings,
) -> list[list[int]]:
    if len(component) <= 1:
        return [list(component)]

    # HDBSCAN handles variable-density groups better than DBSCAN and is the
    # preferred refinement step when available.
    if _HAS_HDBSCAN and len(component) >= max(settings.min_cluster_size + 1, 3):
        try:
            labels = _hdbscan.HDBSCAN(
                min_cluster_size=max(2, settings.min_cluster_size),
                metric="euclidean",
            ).fit_predict(vectors[list(component)])
        except Exception:  # pragma: no cover - optional dependency/runtime specific
            labels = np.full(len(component), -1, dtype=int)

        return _labels_to_groups(component, labels)

    if len(component) >= max(settings.min_cluster_size + 1, 3):
        try:
            labels = DBSCAN(
                eps=0.32 if vector_mode == "embeddings" else 0.45,
                min_samples=max(2, settings.min_cluster_size),
                metric="cosine",
            ).fit_predict(vectors[list(component)])
        except Exception:
            labels = np.full(len(component), -1, dtype=int)

        return _labels_to_groups(component, labels)

    return [list(component)]


def _labels_to_groups(
    component: Sequence[int], labels: Sequence[int]
) -> list[list[int]]:
    clusters: dict[int, list[int]] = {}
    noise: list[list[int]] = []

    for idx, lbl in zip(component, labels):
        if int(lbl) == -1:
            noise.append([int(idx)])
        else:
            clusters.setdefault(int(lbl), []).append(int(idx))

    groups = list(clusters.values())
    if not groups:
        return [list(component)]

    groups.extend(noise)
    return groups


def _nearest_neighbor_pairs(
    vectors: np.ndarray,
    neighbor_k: int,
) -> list[tuple[int, int, float]]:
    if len(vectors) < 2:
        return []

    k = min(max(2, neighbor_k + 1), len(vectors))
    matrix = np.asarray(vectors, dtype=np.float32)

    if _HAS_FAISS and matrix.ndim == 2 and matrix.shape[1] > 0:
        contiguous = np.ascontiguousarray(matrix)
        index = faiss.IndexFlatIP(contiguous.shape[1])
        index.add(contiguous)
        scores, indices = index.search(contiguous, k)
        pairs: list[tuple[int, int, float]] = []
        for i, row in enumerate(indices):
            for score, j in zip(scores[i], row):
                if j < 0 or int(j) == i:
                    continue
                pairs.append((i, int(j), float(score)))
        return pairs

    nn = NearestNeighbors(n_neighbors=k, metric="cosine")
    nn.fit(matrix)
    distances, indices = nn.kneighbors(matrix)
    pairs = []
    for i, row in enumerate(indices):
        for distance, j in zip(distances[i], row):
            if j < 0 or int(j) == i:
                continue
            pairs.append((i, int(j), 1.0 - float(distance)))
    return pairs


def _lexical_similarity(left: str, right: str) -> float:
    if _HAS_RAPIDFUZZ and rapidfuzz_fuzz is not None and rapidfuzz_utils is not None:
        processor = rapidfuzz_utils.default_process
        return float(
            max(
                rapidfuzz_fuzz.WRatio(left, right, processor=processor),
                rapidfuzz_fuzz.token_set_ratio(left, right, processor=processor),
            )
        )
    return SequenceMatcher(None, left.lower(), right.lower()).ratio() * 100.0


def _combine_text(title: str, body: str) -> str:
    parts = [strip_html(title), strip_html(body)]
    return " ".join(part for part in parts if part).strip()


def _normalize_title(value: str) -> str:
    value = strip_html(value).lower()
    value = re.sub(
        r"^(breaking|update|exclusive|analysis|opinion|report)\s*[:\-–—]?\s+",
        "",
        value,
    )
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _mapping_value(record: Mapping[str, Any], key: str) -> Any:
    if hasattr(record, "get"):
        try:
            return record.get(key)
        except Exception:  # pragma: no cover - very defensive
            pass
    try:
        return record[key]
    except Exception:  # pragma: no cover - very defensive
        return None


class _UnionFind:
    def __init__(self, size: int) -> None:
        self._parent = list(range(size))
        self._rank = [0] * size

    def find(self, item: int) -> int:
        parent = self._parent[item]
        if parent != item:
            self._parent[item] = self.find(parent)
        return self._parent[item]

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if self._rank[left_root] < self._rank[right_root]:
            self._parent[left_root] = right_root
        elif self._rank[left_root] > self._rank[right_root]:
            self._parent[right_root] = left_root
        else:
            self._parent[right_root] = left_root
            self._rank[left_root] += 1
