import json
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from mcp_server_appwrite.docs_index import build_index, content_hash, diff_pages
from mcp_server_appwrite.docs_source import Page

MODEL = "test-model"
DIMENSION = 3


class CountingEmbedder:
    """Deterministic per-call but different across calls, like the real API."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, texts: list[str]) -> np.ndarray:
        self.calls.append(list(texts))
        salt = len(self.calls)
        return np.asarray(
            [[salt, len(text), index] for index, text in enumerate(texts)],
            dtype=np.float32,
        )


def page(path: str, body: str, title: str | None = None) -> Page:
    return Page(path=path, title=title or path, description="", content=body)


def checksums(data_dir: Path) -> dict[str, bytes]:
    return {file.name: file.read_bytes() for file in sorted(data_dir.iterdir())}


class BuildIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.embedder = CountingEmbedder()

    def build(self, pages: list[Page]):
        return build_index(
            pages,
            data_dir=self.data_dir,
            model=MODEL,
            dimension=DIMENSION,
            embed=self.embedder,
        )

    def test_first_build_embeds_everything_and_reports_all_pages_added(self):
        report = self.build([page("docs/a", "# A\nbody"), page("docs/b", "# B\nbody")])

        self.assertEqual(report.pages, 2)
        self.assertEqual(report.chunks_embedded, 2)
        self.assertEqual(report.chunks_reused, 0)
        self.assertEqual(
            [item.path for item in report.changes.added], ["docs/a", "docs/b"]
        )
        self.assertFalse(report.changes.removed or report.changes.changed)

        meta = json.loads((self.data_dir / "docs_index_meta.json").read_text())
        self.assertEqual(meta["pages"][0]["hash"], content_hash("# A\nbody"))
        with np.load(self.data_dir / "docs_index.npz") as data:
            self.assertEqual(
                list(data["chunk_hash"]),
                [content_hash("# A\nbody"), content_hash("# B\nbody")],
            )

    def test_unchanged_docs_reuse_vectors_and_write_identical_bytes(self):
        pages = [page("docs/a", "# A\nbody"), page("docs/b", "# B\nbody")]
        self.build(pages)
        first = checksums(self.data_dir)

        # Cross a wall-clock second so timestamp leakage into the archive shows up.
        time.sleep(1.1)
        report = self.build(pages)

        self.assertEqual(report.chunks_embedded, 0)
        self.assertEqual(report.chunks_reused, 2)
        self.assertTrue(report.changes.empty)
        self.assertEqual(
            len(self.embedder.calls), 1, "embedder called again for cached chunks"
        )
        self.assertEqual(checksums(self.data_dir), first)

    def test_only_changed_chunks_are_embedded(self):
        self.build([page("docs/a", "# A\nbody"), page("docs/b", "# B\nbody")])

        report = self.build(
            [
                page("docs/a", "# A\nbody"),
                page("docs/b", "# B\nnew body"),
                page("docs/c", "# C\nbody"),
            ]
        )

        self.assertEqual(report.chunks_embedded, 2)
        self.assertEqual(report.chunks_reused, 1)
        self.assertEqual(self.embedder.calls[-1], ["# B\nnew body", "# C\nbody"])
        self.assertEqual([item.path for item in report.changes.changed], ["docs/b"])
        self.assertEqual([item.path for item in report.changes.added], ["docs/c"])

        with np.load(self.data_dir / "docs_index.npz") as data:
            # Cached vector for docs/a comes from the first call (salt 1).
            self.assertEqual(data["vectors"][0][0], 1.0)
            self.assertEqual(data["vectors"][1][0], 2.0)

    def test_removed_pages_are_reported(self):
        self.build(
            [page("docs/a", "# A\nbody"), page("docs/b", "# B\nbody", title="Bee")]
        )

        report = self.build([page("docs/a", "# A\nbody")])

        self.assertEqual(
            [(item.path, item.title) for item in report.changes.removed],
            [("docs/b", "Bee")],
        )

    def test_model_change_invalidates_cache(self):
        self.build([page("docs/a", "# A\nbody")])

        report = build_index(
            [page("docs/a", "# A\nbody")],
            data_dir=self.data_dir,
            model="other-model",
            dimension=DIMENSION,
            embed=self.embedder,
        )

        self.assertEqual(report.chunks_embedded, 1)
        self.assertTrue(report.changes.empty)


class DiffPagesTests(unittest.TestCase):
    def test_previous_pages_without_hash_are_hashed_from_content(self):
        previous = [{"path": "docs/a", "title": "A", "content": "same"}]

        self.assertTrue(diff_pages(previous, [page("docs/a", "same")]).empty)
        self.assertEqual(
            [
                item.path
                for item in diff_pages(previous, [page("docs/a", "different")]).changed
            ],
            ["docs/a"],
        )


if __name__ == "__main__":
    unittest.main()
