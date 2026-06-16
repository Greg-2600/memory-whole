"""Tests for the clustering pipeline."""

import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np

import clustering


class TestHybridClustering(unittest.TestCase):
    """Verify hybrid clustering groups headlines correctly."""
    def test_semantic_vectors_group_related_headlines(self) -> None:
        entries = [
            {
                "title": "Massive earthquake hits California coast",
                "summary": "",
                "_feed_name": "A",
            },
            {
                "title": "California coast rattled by quake",
                "summary": "",
                "_feed_name": "B",
            },
            {
                "title": "Stock market rallies on tech earnings",
                "summary": "",
                "_feed_name": "C",
            },
        ]

        vectors = np.asarray(
            [
                [1.0, 0.0],
                [0.96, 0.04],
                [0.0, 1.0],
            ],
            dtype=np.float32,
        )

        with (
            mock.patch.object(
                clustering, "_vectorize_texts", return_value=(vectors, "embeddings")
            ),
            mock.patch.object(clustering, "_HAS_FAISS", False),
            mock.patch.object(clustering, "_HAS_HDBSCAN", False),
            mock.patch.object(clustering, "_HAS_RAPIDFUZZ", False),
        ):
            groups = clustering.cluster_entries(entries)

        self.assertEqual(groups, [[0, 1], [2]])

    def test_lexical_bucketing_keeps_duplicate_titles_together(self) -> None:
        entries = [
            {
                "title": "Breaking: Storm hits coast",
                "summary": "",
                "_feed_name": "A",
            },
            {
                "title": "Breaking storm hits coast!",
                "summary": "",
                "_feed_name": "B",
            },
            {
                "title": "Markets rally after earnings",
                "summary": "",
                "_feed_name": "C",
            },
        ]

        vectors = np.asarray(
            [
                [1.0, 0.0, 0.0],
                [0.0, 1.0, 0.0],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float32,
        )

        with (
            mock.patch.object(
                clustering, "_vectorize_texts", return_value=(vectors, "embeddings")
            ),
            mock.patch.object(clustering, "_HAS_FAISS", False),
            mock.patch.object(clustering, "_HAS_HDBSCAN", False),
            mock.patch.object(clustering, "_HAS_RAPIDFUZZ", False),
        ):
            groups = clustering.cluster_entries(entries)

        self.assertEqual(groups[0], [0, 1])
        self.assertEqual(groups[1], [2])

    def test_semantic_similarity_requires_minimum_lexical_overlap(self) -> None:
        entries = [
            {
                "title": "Judge rejects legal effort to cancel White House's UFC event",
                "summary": "",
                "_feed_name": "A",
            },
            {
                "title": "Could Trump’s last-minute endorsement prevail in Georgia’s high-stakes Senate race?",
                "summary": "",
                "_feed_name": "B",
            },
        ]

        vectors = np.asarray(
            [
                [1.0, 0.0],
                [0.998, 0.002],
            ],
            dtype=np.float32,
        )

        with (
            mock.patch.object(
                clustering, "_vectorize_texts", return_value=(vectors, "embeddings")
            ),
            mock.patch.object(clustering, "_HAS_FAISS", False),
            mock.patch.object(clustering, "_HAS_HDBSCAN", False),
            mock.patch.object(clustering, "_HAS_RAPIDFUZZ", False),
        ):
            groups = clustering.cluster_entries(entries)

        self.assertEqual(groups, [[0], [1]])

    def test_hdbscan_refinement_splits_component_noise(self) -> None:
        entries = [
            {"title": "Story A one", "summary": "", "_feed_name": "A"},
            {"title": "Story A two", "summary": "", "_feed_name": "B"},
            {"title": "Story A three", "summary": "", "_feed_name": "C"},
        ]

        vectors = np.asarray(
            [
                [1.0, 0.0],
                [0.98, 0.02],
                [0.0, 1.0],
            ],
            dtype=np.float32,
        )

        class DummyHDBSCAN:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def fit_predict(self, _data):
                return np.asarray([0, 0, -1], dtype=int)

        with (
            mock.patch.object(
                clustering, "_vectorize_texts", return_value=(vectors, "embeddings")
            ),
            mock.patch.object(clustering, "_HAS_FAISS", False),
            mock.patch.object(clustering, "_HAS_HDBSCAN", True),
            mock.patch.object(
                clustering, "_hdbscan", SimpleNamespace(HDBSCAN=DummyHDBSCAN)
            ),
            mock.patch.object(clustering, "_HAS_RAPIDFUZZ", False),
        ):
            groups = clustering.cluster_entries(entries)

        self.assertEqual(groups, [[0, 1], [2]])


if __name__ == "__main__":
    unittest.main()
