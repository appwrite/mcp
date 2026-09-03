"""Build the committed semantic-search index for the Appwrite documentation.

This fetches the published docs from appwrite.io as Markdown (see
``mcp_server_appwrite/docs_source.py``), chunks each page, embeds the chunks with
OpenAI ``text-embedding-3-small``, and writes a small artifact that the running
server loads at startup (see ``mcp_server_appwrite/docs_search.py``). Chunks that
already exist in the committed artifact reuse their stored vectors, so the output
is byte-identical when the documentation has not changed (see
``mcp_server_appwrite/docs_index.py``).

Run this when the docs change and commit the refreshed artifact:

    OPENAI_API_KEY=sk-... uv run python scripts/build_docs_index.py

Outputs (committed into the repo, shipped in the image / wheel):
    src/mcp_server_appwrite/data/docs_index.npz       float32 vectors + chunk->page map + chunk hashes
    src/mcp_server_appwrite/data/docs_index_meta.json page metadata (path/title/desc/content/hash)

Env vars:
    OPENAI_API_KEY        required.
    DOCS_ORIGIN           site to fetch docs from (default "https://appwrite.io").
    DOCS_EMBED_BATCH      embedding batch size (default 100).
    DOCS_REPORT_FILE      optional path to write the JSON build report (page changes,
                          chunks embedded vs reused) for the refresh workflow.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import numpy as np
from openai import OpenAI

from mcp_server_appwrite.constants import DATA_DIR, EMBED_MODEL
from mcp_server_appwrite.docs_index import build_index
from mcp_server_appwrite.docs_source import DEFAULT_ORIGIN, fetch_pages

EMBED_DIMENSION = 1536


def embed_texts(client: OpenAI, texts: list[str], batch_size: int) -> np.ndarray:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        print(f"Embedding {start + 1}-{start + len(batch)} of {len(texts)} ...")
        response = client.embeddings.create(model=EMBED_MODEL, input=batch)
        vectors.extend(item.embedding for item in response.data)
    matrix = np.asarray(vectors, dtype=np.float32).reshape(len(texts), -1)
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
    report_file = os.getenv("DOCS_REPORT_FILE")
    client = OpenAI()

    print(f"Fetching documentation from {origin} ...")
    pages, skipped = asyncio.run(fetch_pages(origin))
    if not pages:
        print("No documentation pages fetched; aborting", file=sys.stderr)
        return 1
    print(f"Fetched {len(pages)} pages, skipped {len(skipped)} unpublished")
    for path in skipped:
        print(f"  skipped {path}")

    report = build_index(
        pages,
        data_dir=DATA_DIR,
        model=EMBED_MODEL,
        dimension=EMBED_DIMENSION,
        embed=lambda texts: embed_texts(client, texts, batch_size),
    )

    print(
        f"Wrote {report.chunks} vectors ({report.chunks_embedded} embedded, "
        f"{report.chunks_reused} reused) across {report.pages} pages to {DATA_DIR}"
    )
    changes = report.changes
    print(
        f"Changes: {len(changes.added)} added, {len(changes.changed)} changed, "
        f"{len(changes.removed)} removed"
    )
    for label, items in (
        ("+", changes.added),
        ("~", changes.changed),
        ("-", changes.removed),
    ):
        for item in items:
            print(f"  {label} {item.path}")

    if report_file:
        Path(report_file).write_text(report.to_json(), encoding="utf-8")
        print(f"Wrote build report to {report_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
