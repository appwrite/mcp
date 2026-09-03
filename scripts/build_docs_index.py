"""Build the committed semantic-search index for the Appwrite documentation.

This fetches the published docs from appwrite.io as Markdown (see
``mcp_server_appwrite/docs_source.py``), chunks each page, embeds the chunks with
OpenAI ``text-embedding-3-small``, and writes a small artifact that the running
server loads at startup (see ``mcp_server_appwrite/docs_search.py``).

Run this when the docs change and commit the refreshed artifact:

    OPENAI_API_KEY=sk-... uv run python scripts/build_docs_index.py

Outputs (committed into the repo, shipped in the image / wheel):
    src/mcp_server_appwrite/data/docs_index.npz       float32 vectors + chunk->page map
    src/mcp_server_appwrite/data/docs_index_meta.json page metadata (path/title/desc/content)

Env vars:
    OPENAI_API_KEY        required.
    DOCS_ORIGIN           site to fetch docs from (default "https://appwrite.io").
    DOCS_EMBED_BATCH      embedding batch size (default 100).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import numpy as np
from openai import OpenAI

from mcp_server_appwrite.docs_source import DEFAULT_ORIGIN, chunk_markdown, fetch_pages

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMENSION = 1536

DATA_DIR = (
    Path(__file__).resolve().parent.parent / "src" / "mcp_server_appwrite" / "data"
)


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

    origin = os.getenv("DOCS_ORIGIN", DEFAULT_ORIGIN).rstrip("/")
    batch_size = int(os.getenv("DOCS_EMBED_BATCH", "100"))
    client = OpenAI()

    print(f"Fetching documentation from {origin} ...")
    fetched, skipped = asyncio.run(fetch_pages(origin))
    if not fetched:
        print("No documentation pages fetched; aborting", file=sys.stderr)
        return 1
    print(f"Fetched {len(fetched)} pages, skipped {len(skipped)} unpublished")
    for path in skipped:
        print(f"  skipped {path}")

    pages: list[dict[str, str]] = []
    chunk_texts: list[str] = []
    chunk_page: list[int] = []

    for page in sorted(fetched, key=lambda page: page.path):
        chunks = chunk_markdown(page.content)
        if not chunks:
            continue
        page_index = len(pages)
        pages.append(
            {
                "path": page.path,
                "title": page.title,
                "description": page.description,
                "content": page.content,
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
