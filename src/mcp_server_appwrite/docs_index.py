"""Assemble the committed docs index artifact deterministically.

``scripts/build_docs_index.py`` fetches and chunks the docs, then hands the chunks
here. This module owns everything that makes consecutive builds comparable and
publication safe:

* The artifact is a single ``docs_index.npz`` holding the vectors, the chunk to
  page map, the chunk hashes, and the page metadata as an embedded ``meta.json``
  member. One file means one atomic rename to publish; there is no window where
  vectors and metadata can disagree.
* Every page carries a ``hash`` of its title, description, and body, and every
  chunk vector carries the hash of its text. Chunks whose hash already exists in
  the previous artifact reuse the stored vector instead of being embedded again.
  OpenAI embeddings are not bit-for-bit reproducible, so without this every
  rebuild produced a different binary and a spurious release even when no
  documentation changed.
* Zip entries carry a fixed timestamp so an unchanged documentation set yields a
  byte-identical file.
* Builds take an exclusive lock on the data directory so overlapping manual runs
  serialize instead of racing.
* Comparing the previous and new page hashes yields a change report (added,
  removed, changed pages) that the refresh workflow turns into release notes.
"""

from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np

from .constants import INDEX_FILE, META_MEMBER
from .docs_source import Page, chunk_markdown

Embedder = Callable[[list[str]], np.ndarray]
"""Embeds a batch of texts into an ``(n, dimension)`` float32 matrix."""

# ``np.savez_compressed`` stamps every zip entry with the current time, which alone
# makes two otherwise identical builds differ. Entries are written with a fixed
# timestamp instead (the earliest one the zip format can express).
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

ARTIFACT_MODE = 0o644
LOCK_FILE = ".build.lock"


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


def read_artifact(path: Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    """Return the embedded metadata and the arrays of one artifact."""
    with np.load(path) as data:
        raw_meta = data[META_MEMBER] if META_MEMBER in data else None
        arrays = {name: data[name] for name in data.files if name != META_MEMBER}
    meta = json.loads(bytes(raw_meta).decode("utf-8")) if raw_meta is not None else {}
    return meta, arrays


def write_artifact(path: Path, meta: dict[str, Any], **arrays: np.ndarray) -> None:
    """Write arrays plus embedded metadata in ``.npz`` layout with fixed zip metadata."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, array in arrays.items():
            buffer = io.BytesIO()
            np.lib.format.write_array(buffer, np.ascontiguousarray(array))
            archive.writestr(_entry(f"{name}.npy"), buffer.getvalue())
        archive.writestr(
            _entry(META_MEMBER),
            json.dumps(meta, ensure_ascii=False).encode("utf-8"),
        )


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=_ZIP_EPOCH)
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


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
    path = data_dir / INDEX_FILE
    if not path.exists():
        return [], {}

    meta, arrays = read_artifact(path)
    pages = list(meta.get("pages", []))
    if meta.get("model") != model or meta.get("dimension") != dimension:
        return pages, {}
    if "chunk_hash" not in arrays:
        return pages, {}
    vectors = arrays["vectors"]
    cache = {
        str(chunk_hash): vectors[index]
        for index, chunk_hash in enumerate(arrays["chunk_hash"])
    }
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


def _hash_page(page: Page) -> str:
    return page_hash(page.title, page.description, page.content)


def _hash_record(record: dict[str, Any]) -> str:
    return page_hash(
        str(record.get("title", "")),
        str(record.get("description", "")),
        str(record.get("content", "")),
    )


@contextmanager
def build_lock(data_dir: Path) -> Iterator[None]:
    """Serialize builds writing to the same data directory."""
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / LOCK_FILE, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def build_index(
    pages: list[Page],
    *,
    data_dir: Path,
    model: str,
    dimension: int,
    embed: Embedder,
) -> BuildReport:
    """Chunk, embed (reusing cached vectors), and publish the artifact.

    Pages are processed in path order and chunks in document order, so the
    written file is byte-identical when the documentation is unchanged.
    """
    with build_lock(data_dir):
        return _build_locked(
            pages, data_dir=data_dir, model=model, dimension=dimension, embed=embed
        )


def _build_locked(
    pages: list[Page],
    *,
    data_dir: Path,
    model: str,
    dimension: int,
    embed: Embedder,
) -> BuildReport:
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

    # Identical chunk texts (shared boilerplate across pages) embed once and share
    # the vector; embedding them separately would give each a slightly different
    # vector and make a fresh build differ from a cached rebuild.
    missing: dict[str, str] = {}
    for index, chunk_hash in enumerate(chunk_hashes):
        if chunk_hash not in cache and chunk_hash not in missing:
            missing[chunk_hash] = chunk_texts[index]
    if missing:
        embedded = embed(list(missing.values()))
        if embedded.shape != (len(missing), dimension):
            raise ValueError(
                f"Embedder returned shape {embedded.shape}, "
                f"expected ({len(missing)}, {dimension})"
            )
        for row, chunk_hash in enumerate(missing):
            cache[chunk_hash] = embedded[row]

    vectors = np.zeros((len(chunk_texts), dimension), dtype=np.float32)
    for index, chunk_hash in enumerate(chunk_hashes):
        vectors[index] = cache[chunk_hash]

    # Write to a unique sibling file and rename it into place: a single rename
    # publishes vectors and metadata together or not at all.
    handle, tmp_name = tempfile.mkstemp(
        prefix=f"{INDEX_FILE}.", suffix=".tmp", dir=data_dir
    )
    os.close(handle)
    tmp = Path(tmp_name)
    try:
        write_artifact(
            tmp,
            {"model": model, "dimension": dimension, "pages": page_records},
            vectors=vectors,
            chunk_page=np.asarray(chunk_page, dtype=np.int32),
            chunk_hash=np.asarray(chunk_hashes, dtype="U64"),
        )
        # mkstemp creates private files; the artifact is committed and shipped.
        os.chmod(tmp, ARTIFACT_MODE)
        os.replace(tmp, data_dir / INDEX_FILE)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise

    kept = [page for page in pages if chunk_markdown(page.content)]
    return BuildReport(
        pages=len(page_records),
        chunks=len(chunk_texts),
        chunks_embedded=len(missing),
        chunks_reused=len(chunk_texts) - len(missing),
        changes=diff_pages(previous_pages, kept),
    )
