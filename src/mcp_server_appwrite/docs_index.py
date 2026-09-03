"""Assemble the committed docs index artifact deterministically.

``scripts/build_docs_index.py`` fetches and chunks the docs, then hands the chunks
here. This module owns everything that makes consecutive builds comparable:

* Every page carries a ``hash`` of its content in ``docs_index_meta.json`` and
  every chunk vector carries the hash of its text in ``docs_index.npz``.
* Chunks whose hash already exists in the previous artifact reuse the stored
  vector instead of being embedded again. OpenAI embeddings are not bit-for-bit
  reproducible, so without this every rebuild produced a different binary and a
  spurious release even when no documentation changed.
* Comparing the previous and new page hashes yields a change report (added,
  removed, changed pages) that the refresh workflow turns into release notes.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .constants import META_FILE, VECTORS_FILE
from .docs_source import Page, chunk_markdown

Embedder = Callable[[list[str]], np.ndarray]
"""Embeds a batch of texts into an ``(n, dimension)`` float32 matrix."""


# ``np.savez_compressed`` stamps every zip entry with the current time, which alone
# makes two otherwise identical builds differ. Entries are written with a fixed
# timestamp instead (the earliest one the zip format can express).
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def save_arrays(path: Path, **arrays: np.ndarray) -> None:
    """Write arrays in ``.npz`` layout with deterministic zip metadata."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, array in arrays.items():
            buffer = io.BytesIO()
            np.lib.format.write_array(buffer, np.ascontiguousarray(array))
            info = zipfile.ZipInfo(f"{name}.npy", date_time=_ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, buffer.getvalue())


@dataclass(frozen=True)
class PageSummary:
    path: str
    title: str


@dataclass
class Changes:
    """Pages that differ between the previous artifact and the new build."""

    added: list[PageSummary] = field(default_factory=list)
    removed: list[PageSummary] = field(default_factory=list)
    changed: list[PageSummary] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed)


@dataclass
class BuildReport:
    pages: int
    chunks: int
    chunks_embedded: int
    chunks_reused: int
    changes: Changes

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)


def load_previous(
    data_dir: Path, model: str
) -> tuple[list[dict[str, Any]], dict[str, np.ndarray]]:
    """Return the previous artifact's pages and its chunk-hash -> vector cache.

    Both are empty when no artifact exists. The vector cache is empty when the
    previous artifact was built with a different model or predates chunk hashes.
    """
    meta_path = data_dir / META_FILE
    vectors_path = data_dir / VECTORS_FILE
    if not meta_path.exists() or not vectors_path.exists():
        return [], {}

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    pages = list(meta.get("pages", []))

    if meta.get("model") != model:
        return pages, {}
    with np.load(vectors_path) as data:
        if "chunk_hash" not in data:
            return pages, {}
        hashes = data["chunk_hash"]
        vectors = data["vectors"]
    cache = {str(chunk_hash): vectors[index] for index, chunk_hash in enumerate(hashes)}
    return pages, cache


def diff_pages(previous: list[dict[str, Any]], pages: list[Page]) -> Changes:
    """Compare page content hashes between the previous artifact and the new pages.

    Previous pages without a stored hash are hashed from their content, so the
    first build after introducing hashes still reports accurately.
    """
    before = {
        page["path"]: (page.get("hash") or content_hash(page.get("content", "")), page)
        for page in previous
    }
    after = {page.path: page for page in pages}

    changes = Changes()
    for path in sorted(after.keys() - before.keys()):
        changes.added.append(PageSummary(path, after[path].title))
    for path in sorted(before.keys() - after.keys()):
        changes.removed.append(PageSummary(path, str(before[path][1].get("title", ""))))
    for path in sorted(after.keys() & before.keys()):
        if before[path][0] != content_hash(after[path].content):
            changes.changed.append(PageSummary(path, after[path].title))
    return changes


def build_index(
    pages: list[Page],
    *,
    data_dir: Path,
    model: str,
    dimension: int,
    embed: Embedder,
) -> BuildReport:
    """Chunk, embed (reusing cached vectors), and write the artifact.

    Pages are processed in path order and chunks in document order, so the
    written files are byte-identical when the documentation is unchanged.
    """
    previous_pages, cache = load_previous(data_dir, model)

    page_records: list[dict[str, str]] = []
    chunk_texts: list[str] = []
    chunk_hashes: list[str] = []
    chunk_page: list[int] = []
    for page in sorted(pages, key=lambda page: page.path):
        chunks = chunk_markdown(page.content)
        if not chunks:
            continue
        page_index = len(page_records)
        page_records.append(
            {
                "path": page.path,
                "title": page.title,
                "description": page.description,
                "content": page.content,
                "hash": content_hash(page.content),
            }
        )
        for chunk in chunks:
            chunk_texts.append(chunk)
            chunk_hashes.append(content_hash(chunk))
            chunk_page.append(page_index)

    if not chunk_texts:
        raise ValueError("No chunks produced from the fetched pages")

    missing = [
        index
        for index, chunk_hash in enumerate(chunk_hashes)
        if chunk_hash not in cache
    ]
    vectors = np.zeros((len(chunk_texts), dimension), dtype=np.float32)
    for index, chunk_hash in enumerate(chunk_hashes):
        if chunk_hash in cache:
            vectors[index] = cache[chunk_hash]
    if missing:
        embedded = embed([chunk_texts[index] for index in missing])
        if embedded.shape != (len(missing), dimension):
            raise ValueError(
                f"Embedder returned shape {embedded.shape}, expected ({len(missing)}, {dimension})"
            )
        for row, index in enumerate(missing):
            vectors[index] = embedded[row]

    data_dir.mkdir(parents=True, exist_ok=True)
    save_arrays(
        data_dir / VECTORS_FILE,
        vectors=vectors,
        chunk_page=np.asarray(chunk_page, dtype=np.int32),
        chunk_hash=np.asarray(chunk_hashes, dtype="U64"),
    )
    (data_dir / META_FILE).write_text(
        json.dumps(
            {"model": model, "dimension": dimension, "pages": page_records},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    kept = [page for page in pages if chunk_markdown(page.content)]
    return BuildReport(
        pages=len(page_records),
        chunks=len(chunk_texts),
        chunks_embedded=len(missing),
        chunks_reused=len(chunk_texts) - len(missing),
        changes=diff_pages(previous_pages, kept),
    )
