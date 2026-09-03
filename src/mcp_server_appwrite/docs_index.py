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
import os
import tempfile
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

from .constants import META_FILE, VECTORS_FILE
from .docs_source import Page, chunk_markdown

Embedder = Callable[[list[str]], np.ndarray]
"""Embeds a batch of texts into an ``(n, dimension)`` float32 matrix."""


# ``np.savez_compressed`` stamps every zip entry with the current time, which alone
# makes two otherwise identical builds differ. Entries are written with a fixed
# timestamp instead (the earliest one the zip format can express).
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

ARTIFACT_MODE = 0o644


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def page_hash(title: str, description: str, content: str) -> str:
    """Hash everything about a page that reaches search results.

    Title and description are returned to clients alongside the body, so a
    metadata-only edit must count as a change even though no chunk is re-embedded.
    """
    digest = hashlib.sha256()
    for part in (title, description, content):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def build_fingerprint(model: str, dimension: int, page_hashes: Iterable[str]) -> str:
    """Identify one build by its embedding configuration and ordered page hashes.

    Model and dimension are part of the stamp so vectors from one embedding
    setup can never be served with metadata that describes another.
    """
    digest = hashlib.sha256()
    digest.update(f"{model}\0{dimension}\0".encode("utf-8"))
    for page_hash_value in page_hashes:
        digest.update(page_hash_value.encode("ascii"))
    return digest.hexdigest()


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
    data_dir: Path, model: str, dimension: int
) -> tuple[list[dict[str, Any]], dict[str, np.ndarray]]:
    """Return the previous artifact's pages and its chunk-hash -> vector cache.

    Both are empty when no artifact exists. The vector cache is empty when the
    previous artifact was built with a different model or dimension, or predates
    chunk hashes.
    """
    meta_path = data_dir / META_FILE
    vectors_path = data_dir / VECTORS_FILE
    if not meta_path.exists() or not vectors_path.exists():
        return [], {}

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    pages = list(meta.get("pages", []))

    if meta.get("model") != model or meta.get("dimension") != dimension:
        return pages, {}
    with np.load(vectors_path) as data:
        if "chunk_hash" not in data:
            return pages, {}
        hashes = data["chunk_hash"]
        vectors = data["vectors"]
    cache = {str(chunk_hash): vectors[index] for index, chunk_hash in enumerate(hashes)}
    return pages, cache


def diff_pages(previous: list[dict[str, Any]], pages: list[Page]) -> Changes:
    """Compare page hashes between the previous artifact and the new pages.

    Previous pages without a stored hash are hashed from their fields, so the
    first build after introducing hashes still reports accurately.
    """
    before = {
        page["path"]: (page.get("hash") or _hash_record(page), page)
        for page in previous
    }
    after = {page.path: page for page in pages}

    changes = Changes()
    for path in sorted(after.keys() - before.keys()):
        changes.added.append(PageSummary(path, after[path].title))
    for path in sorted(before.keys() - after.keys()):
        changes.removed.append(PageSummary(path, str(before[path][1].get("title", ""))))
    for path in sorted(after.keys() & before.keys()):
        if before[path][0] != _hash_page(after[path]):
            changes.changed.append(PageSummary(path, after[path].title))
    return changes


def _temporary_path(data_dir: Path, name: str) -> Path:
    """Unique sibling path so overlapping builds never write the same temp file."""
    handle, path = tempfile.mkstemp(prefix=f"{name}.", suffix=".tmp", dir=data_dir)
    os.close(handle)
    # mkstemp creates private files; the artifact is committed and shipped, so
    # give it ordinary world-readable permissions.
    os.chmod(path, ARTIFACT_MODE)
    return Path(path)


def _hash_page(page: Page) -> str:
    return page_hash(page.title, page.description, page.content)


def _hash_record(record: dict[str, Any]) -> str:
    return page_hash(
        str(record.get("title", "")),
        str(record.get("description", "")),
        str(record.get("content", "")),
    )


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
    previous_pages, cache = load_previous(data_dir, model, dimension)

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
                "hash": _hash_page(page),
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

    # The two files cannot be replaced in one atomic step, so each carries the
    # same build fingerprint and the loader refuses a pair whose stamps differ.
    # Writing to the side and swapping keeps the mismatch window to two renames.
    build = build_fingerprint(
        model, dimension, (record["hash"] for record in page_records)
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    vectors_tmp = _temporary_path(data_dir, VECTORS_FILE)
    meta_tmp = _temporary_path(data_dir, META_FILE)
    save_arrays(
        vectors_tmp,
        vectors=vectors,
        chunk_page=np.asarray(chunk_page, dtype=np.int32),
        chunk_hash=np.asarray(chunk_hashes, dtype="U64"),
        build=np.asarray([build], dtype="U64"),
    )
    meta_tmp.write_text(
        json.dumps(
            {
                "model": model,
                "dimension": dimension,
                "build": build,
                "pages": page_records,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(vectors_tmp, data_dir / VECTORS_FILE)
    os.replace(meta_tmp, data_dir / META_FILE)

    kept = [page for page in pages if chunk_markdown(page.content)]
    return BuildReport(
        pages=len(page_records),
        chunks=len(chunk_texts),
        chunks_embedded=len(missing),
        chunks_reused=len(chunk_texts) - len(missing),
        changes=diff_pages(previous_pages, kept),
    )
