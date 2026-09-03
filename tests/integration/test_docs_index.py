"""End-to-end build of the docs index from the live site.

Runs the same pipeline as ``scripts/build_docs_index.py`` against a handful of
real pages on appwrite.io: fetch the manifest, download the Markdown exports,
chunk, build the artifact, and load it into ``DocsSearch``. Embedding is the only
step swapped out: a local, deterministic-per-call embedder stands in for OpenAI so
the test needs no credentials while still proving the properties that matter for
the daily refresh:

* a rebuild of unchanged docs embeds nothing and writes byte-identical bytes,
* an edited page is reported as changed and only its chunks are re-embedded,
* the published artifact is readable by the server and answers a search.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx
import numpy as np

from mcp_server_appwrite.docs_index import build_index, read_artifact
from mcp_server_appwrite.docs_search import DocsSearch
from mcp_server_appwrite.docs_source import (
    DEFAULT_ORIGIN,
    Entry,
    Page,
    fetch_page,
)

SAMPLE_PATHS = [
    "docs/products/auth",
    "docs/products/auth/email-password",
    "docs/products/databases",
    "docs/products/storage",
]
MODEL = "local-hash"
DIMENSION = 32


class HashEmbedder:
    """Stands in for OpenAI: content-addressed, unit-length, and it counts calls.

    Each call salts its vectors so two separate embeddings of the same text
    differ slightly, mirroring the real API's non-reproducibility. That is what
    makes the byte-identical assertions meaningful.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, texts: list[str]) -> np.ndarray:
        self.calls.append(list(texts))
        rows = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vector = np.frombuffer(digest, dtype=np.uint8).astype(np.float32)
            vector = vector[:DIMENSION] / 255.0
            vector[0] += len(self.calls) * 1e-3
            rows.append(vector / np.linalg.norm(vector))
        return np.asarray(rows, dtype=np.float32)

    def query(self, text: str) -> list[float]:
        return self([text])[0].tolist()


def fetch_sample() -> list[Page]:
    async def run() -> list[Page]:
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            pages = await asyncio.gather(
                *(
                    fetch_page(client, DEFAULT_ORIGIN, Entry(path, "", ""))
                    for path in SAMPLE_PATHS
                )
            )
        return [page for page in pages if page is not None]

    return asyncio.run(run())


class DocsIndexEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.pages = fetch_sample()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.embedder = HashEmbedder()

    def build(self, pages: list[Page]):
        return build_index(
            pages,
            data_dir=self.data_dir,
            model=MODEL,
            dimension=DIMENSION,
            embed=self.embedder,
        )

    def artifact_bytes(self) -> bytes:
        return (self.data_dir / "docs_index.npz").read_bytes()

    def test_sample_pages_are_live(self):
        self.assertEqual(sorted(p.path for p in self.pages), sorted(SAMPLE_PATHS))
        self.assertTrue(all(page.title for page in self.pages))

    def test_build_then_rebuild_is_byte_identical_and_embeds_nothing(self):
        first = self.build(self.pages)
        self.assertEqual(first.pages, len(self.pages))
        self.assertGreater(first.chunks_embedded, 0)
        self.assertEqual([c.path for c in first.changes.added], sorted(SAMPLE_PATHS))
        first_bytes = self.artifact_bytes()

        second = self.build(self.pages)

        self.assertEqual(second.chunks_embedded, 0)
        self.assertEqual(second.chunks_reused, first.chunks)
        self.assertTrue(second.changes.empty)
        self.assertEqual(len(self.embedder.calls), 1)
        self.assertEqual(self.artifact_bytes(), first_bytes)

    def test_edited_page_is_reported_and_only_its_chunks_re_embed(self):
        self.build(self.pages)
        edited = [
            (
                replace(page, content=page.content + "\n\n# Addendum\n\nNew paragraph.")
                if page.path == "docs/products/storage"
                else page
            )
            for page in self.pages
        ]

        report = self.build(edited)

        self.assertEqual(
            [c.path for c in report.changes.changed], ["docs/products/storage"]
        )
        self.assertFalse(report.changes.added or report.changes.removed)
        self.assertEqual(report.chunks_embedded, 1)
        self.assertIn("# Addendum", self.embedder.calls[-1][0])

    def test_metadata_only_edit_is_reported_without_embedding(self):
        self.build(self.pages)
        renamed = [
            (
                replace(page, title="Renamed")
                if page.path == "docs/products/auth"
                else page
            )
            for page in self.pages
        ]

        report = self.build(renamed)

        self.assertEqual(
            [c.path for c in report.changes.changed], ["docs/products/auth"]
        )
        self.assertEqual(report.chunks_embedded, 0)

    def test_removed_page_is_reported(self):
        self.build(self.pages)

        report = self.build(
            [p for p in self.pages if p.path != "docs/products/databases"]
        )

        self.assertEqual(
            [c.path for c in report.changes.removed], ["docs/products/databases"]
        )

    def test_artifact_layout_and_server_search(self):
        self.build(self.pages)

        meta, arrays = read_artifact(self.data_dir / "docs_index.npz")
        self.assertEqual(meta["model"], MODEL)
        self.assertEqual(meta["dimension"], DIMENSION)
        self.assertEqual(sorted(arrays), ["chunk_hash", "chunk_page", "vectors"])
        self.assertEqual(
            arrays["vectors"].shape, (len(arrays["chunk_hash"]), DIMENSION)
        )
        self.assertEqual(
            {"path", "title", "description", "content", "hash"}, set(meta["pages"][0])
        )

        search = DocsSearch(data_dir=self.data_dir, embedder=self.embedder.query)
        self.assertTrue(search.available)
        # Query with the exact text of a chunk from the auth page: with the hash
        # embedder that is the only way to land near a stored vector, and it
        # proves the chunk -> page mapping round-trips through the artifact.
        auth = next(p for p in self.pages if p.path == "docs/products/auth")
        first_chunk = auth.content.split("\n# ", 1)[0].strip()
        result = json.loads(search.search({"query": first_chunk, "limit": 1})[0].text)
        self.assertEqual(result["results"][0]["path"], "docs/products/auth")
