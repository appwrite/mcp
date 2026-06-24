"""Build the committed semantic-search index for the Appwrite documentation.

This is the Python port of ``mcp-for-docs``'s download + init-vector-store pipeline.
It downloads the Appwrite docs from GitHub, chunks each page's markdown, embeds the
chunks with OpenAI ``text-embedding-3-small``, and writes a small artifact that the
running server loads at startup (see ``mcp_server_appwrite/docs_search.py``).

Run this when the docs change and commit the refreshed artifact:

    OPENAI_API_KEY=sk-... uv run python scripts/build_docs_index.py

Outputs (committed into the repo, shipped in the image / wheel):
    src/mcp_server_appwrite/data/docs_index.npz       float32 vectors + chunk->page map
    src/mcp_server_appwrite/data/docs_index_meta.json page metadata (path/title/desc/content)

Env vars:
    OPENAI_API_KEY        required.
    DOCS_WEBSITE_REF      git ref of appwrite/website to index (default "main").
    DOCS_EMBED_BATCH      embedding batch size (default 100).
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tarfile
from pathlib import Path

import httpx
import numpy as np
import yaml
from openai import OpenAI

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMENSION = 1536
GITHUB_OWNER = "appwrite"
GITHUB_REPO = "website"
DOCS_SUBDIR = "src/routes/docs"

# Approximate Mastra's markdown chunking: header-aware sections packed to ~1500
# chars with ~200 chars of overlap. Exact sizing is not load-bearing — retrieval
# quality is dominated by the (identical) embedding model.
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200

DATA_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "mcp_server_appwrite" / "data"
)


def download_docs(ref: str) -> dict[str, str]:
    """Download appwrite/website and return {webPath: raw .markdoc text}."""
    url = f"https://codeload.github.com/{GITHUB_OWNER}/{GITHUB_REPO}/tar.gz/{ref}"
    print(f"Downloading {GITHUB_OWNER}/{GITHUB_REPO}@{ref} ...")
    response = httpx.get(url, follow_redirects=True, timeout=120.0)
    response.raise_for_status()

    pages: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.endswith(".markdoc"):
                continue
            # member.name == "website-<ref>/src/routes/docs/.../+page.markdoc"
            parts = member.name.split("/", 1)
            if len(parts) != 2:
                continue
            repo_relative = parts[1]
            if not repo_relative.startswith(DOCS_SUBDIR + "/"):
                continue
            fileobj = tar.extractfile(member)
            if fileobj is None:
                continue
            text = fileobj.read().decode("utf-8", errors="replace")
            # webPath mirrors mcp-for-docs: "docs/<...>" with "/+page.markdoc" stripped.
            inner = repo_relative[len(DOCS_SUBDIR) + 1 :]  # strip "src/routes/docs/"
            web_path = ("docs/" + inner).replace("/+page.markdoc", "")
            pages[web_path] = text

    print(f"Found {len(pages)} .markdoc pages")
    return pages


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split YAML front-matter from the markdown body."""
    if text.startswith("---"):
        match = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
        if match:
            try:
                attributes = yaml.safe_load(match.group(1)) or {}
            except yaml.YAMLError:
                attributes = {}
            if not isinstance(attributes, dict):
                attributes = {}
            return attributes, match.group(2)
    return {}, text


def chunk_markdown(text: str) -> list[str]:
    """Header-aware markdown chunking approximating Mastra's markdown strategy."""
    text = text.strip()
    if not text:
        return []

    # Split into header-delimited sections, keeping the header with its body.
    sections: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if re.match(r"^#{1,6}\s", line) and current:
            sections.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current).strip())

    chunks: list[str] = []
    for section in sections:
        if not section:
            continue
        if len(section) <= CHUNK_SIZE:
            chunks.append(section)
            continue
        # Hard-split oversized sections with overlap.
        start = 0
        while start < len(section):
            end = start + CHUNK_SIZE
            chunks.append(section[start:end].strip())
            if end >= len(section):
                break
            start = end - CHUNK_OVERLAP
    return [chunk for chunk in chunks if chunk]


def embed_texts(client: OpenAI, texts: list[str], batch_size: int) -> np.ndarray:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        print(f"Embedding {start + 1}-{start + len(batch)} of {len(texts)} ...")
        response = client.embeddings.create(model=EMBED_MODEL, input=batch)
        vectors.extend(item.embedding for item in response.data)
    matrix = np.asarray(vectors, dtype=np.float32)
    # L2-normalize so cosine similarity is a dot product at query time.
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def main() -> int:
    # Load OPENAI_API_KEY (and friends) from a local .env, like the server does.
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set", file=sys.stderr)
        return 1

    ref = os.getenv("DOCS_WEBSITE_REF", "main")
    batch_size = int(os.getenv("DOCS_EMBED_BATCH", "100"))
    client = OpenAI()

    raw_pages = download_docs(ref)

    pages: list[dict[str, str]] = []
    chunk_texts: list[str] = []
    chunk_page: list[int] = []

    for web_path, raw in sorted(raw_pages.items()):
        attributes, body = parse_front_matter(raw)
        chunks = chunk_markdown(body)
        if not chunks:
            continue
        page_index = len(pages)
        pages.append(
            {
                "path": web_path,
                "title": str(attributes.get("title", "")),
                "description": str(attributes.get("description", "")),
                "content": body.strip(),
            }
        )
        for chunk in chunks:
            chunk_texts.append(chunk)
            chunk_page.append(page_index)

    if not chunk_texts:
        print("No chunks produced; aborting", file=sys.stderr)
        return 1

    print(f"Indexing {len(chunk_texts)} chunks across {len(pages)} pages")
    vectors = embed_texts(client, chunk_texts, batch_size)
    if vectors.shape[1] != EMBED_DIMENSION:
        print(f"Unexpected embedding dimension {vectors.shape[1]}", file=sys.stderr)
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        DATA_DIR / "docs_index.npz",
        vectors=vectors,
        chunk_page=np.asarray(chunk_page, dtype=np.int32),
    )
    (DATA_DIR / "docs_index_meta.json").write_text(
        json.dumps(
            {"model": EMBED_MODEL, "dimension": EMBED_DIMENSION, "pages": pages},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(f"Wrote {vectors.shape[0]} vectors and {len(pages)} pages to {DATA_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
