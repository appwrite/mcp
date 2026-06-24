import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from mcp_server_appwrite.docs_search import MAX_LIMIT, DocsSearch, _clamp_limit


def write_index(data_dir: Path) -> None:
    # Three pages; page 0 has two chunks. Dimension 3 keeps the test tiny.
    vectors = np.array(
        [
            [1.0, 0.0, 0.0],  # chunk 0 -> page 0 (databases)
            [0.96, 0.28, 0.0],  # chunk 1 -> page 0 (databases)
            [0.0, 1.0, 0.0],  # chunk 2 -> page 1 (storage)
            [0.0, 0.0, 1.0],  # chunk 3 -> page 2 (auth)
        ],
        dtype=np.float32,
    )
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors = vectors / norms
    np.savez_compressed(
        data_dir / "docs_index.npz",
        vectors=vectors,
        chunk_page=np.array([0, 0, 1, 2], dtype=np.int32),
    )
    pages = [
        {
            "path": "docs/products/databases",
            "title": "Databases",
            "description": "Work with databases.",
            "content": "# Databases\nFull databases page content.",
        },
        {
            "path": "docs/products/storage",
            "title": "Storage",
            "description": "Store files.",
            "content": "# Storage\nFull storage page content.",
        },
        {
            "path": "docs/products/auth",
            "title": "Authentication",
            "description": "Authenticate users.",
            "content": "# Auth\nFull auth page content.",
        },
    ]
    (data_dir / "docs_index_meta.json").write_text(
        json.dumps({"model": "test", "dimension": 3, "pages": pages})
    )


QUERY_VECTORS = {
    "databases query": [1.0, 0.0, 0.0],
    "auth query": [0.0, 0.0, 1.0],
    "mixed query": [1.0, 1.0, 1.0],
}


def fake_embedder(query: str) -> list[float]:
    return QUERY_VECTORS[query]


class DocsSearchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name)
        write_index(self.data_dir)

    def tearDown(self):
        self._tmp.cleanup()

    def make_search(self, **kwargs) -> DocsSearch:
        return DocsSearch(
            data_dir=self.data_dir,
            embedder=fake_embedder,
            **kwargs,
        )

    def test_available_when_index_and_embedder_present(self):
        self.assertTrue(self.make_search().available)

    def test_unavailable_without_embedder(self):
        search = DocsSearch(data_dir=self.data_dir, embedder=None)
        self.assertFalse(search.available)
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            search.search({"query": "databases query"})

    def test_short_query_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least 3"):
            self.make_search().search({"query": "ab"})

    def test_ranks_and_dedupes_to_page(self):
        result = self.make_search().search({"query": "databases query"})
        payload = json.loads(result[0].text)
        # Both top chunks belong to page 0 -> a single deduped result.
        self.assertEqual(len(payload["results"]), 1)
        top = payload["results"][0]
        self.assertEqual(top["path"], "docs/products/databases")
        self.assertEqual(top["title"], "Databases")
        self.assertIn("Full databases page content", top["content"])
        self.assertAlmostEqual(top["score"], 1.0, places=2)

    def test_min_score_filters_unrelated_pages(self):
        # Query aligns only with the auth chunk; orthogonal chunks score 0.
        result = self.make_search(min_score=0.25).search({"query": "auth query"})
        payload = json.loads(result[0].text)
        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["path"], "docs/products/auth")

    def test_no_match_returns_message(self):
        # The mixed query scores ~0.577 against every chunk; a high threshold
        # rejects them all.
        result = self.make_search(min_score=0.99).search({"query": "mixed query"})
        self.assertIn("No documentation matched", result[0].text)

    def test_clamp_limit(self):
        self.assertEqual(_clamp_limit(None, 5), 5)
        self.assertEqual(_clamp_limit(50, 5), MAX_LIMIT)
        with self.assertRaises(ValueError):
            _clamp_limit(0, 5)


if __name__ == "__main__":
    unittest.main()
