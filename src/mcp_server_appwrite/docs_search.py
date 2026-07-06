"""In-process semantic search over the Appwrite documentation.

The heavy lifting (downloading docs, chunking, embedding) happens ahead of time
in ``scripts/build_docs_index.py``; the result is a small artifact committed
under ``data/`` and loaded here at startup.

At query time we embed the user's query with the same OpenAI model used to build
the index (``text-embedding-3-small``) and rank the indexed chunks by cosine
similarity. Vectors are L2-normalized at build time, so cosine similarity is a
plain dot product. Matching chunks are deduped to their source page and the full
page content is returned.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

import mcp.types as types

from . import telemetry
from .constants import (
    DATA_DIR,
    DOCS_DEFAULT_LIMIT,
    DOCS_DEFAULT_MIN_SCORE,
    DOCS_MAX_LIMIT,
    DOCS_MIN_QUERY_LENGTH,
    DOCS_TOOL_NAME,
    EMBED_MODEL,
    META_FILE,
    VECTORS_FILE,
)

ToolContent = types.TextContent | types.ImageContent | types.EmbeddedResource

# An embedder maps a query string to its embedding vector.
Embedder = Callable[[str], list[float]]


def _default_embedder() -> Embedder | None:
    """Build an OpenAI-backed embedder, or ``None`` if no API key is configured."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    api_key = "".join(api_key.split())
    if not api_key:
        return None

    from openai import OpenAI

    client = OpenAI(api_key=api_key)

    def embed(text: str) -> list[float]:
        response = client.embeddings.create(model=EMBED_MODEL, input=text)
        return response.data[0].embedding

    return embed


def _clamp_limit(value: Any, default: int) -> int:
    if value is None:
        return default
    limit = int(value)
    if limit < 1:
        raise ValueError("limit must be at least 1.")
    return min(limit, DOCS_MAX_LIMIT)


class DocsSearch:
    """Loads the committed docs index and answers semantic search queries.

    The instance is *available* only when both the index artifact and an embedder
    (OpenAI API key) are present. ``server.build_operator`` omits the tool when the
    instance is unavailable so the server still boots without docs search.
    """

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        embedder: Embedder | None = None,
        min_score: float | None = None,
        default_limit: int = DOCS_DEFAULT_LIMIT,
    ):
        self._data_dir = data_dir or DATA_DIR
        self._embedder = embedder if embedder is not None else _default_embedder()
        self._min_score = (
            min_score
            if min_score is not None
            else float(os.getenv("DOCS_SEARCH_MIN_SCORE", DOCS_DEFAULT_MIN_SCORE))
        )
        self._default_limit = int(os.getenv("DOCS_SEARCH_LIMIT", default_limit))
        self._vectors = None  # np.ndarray [N, D], L2-normalized
        self._chunk_page = None  # np.ndarray [N] int, index into self._pages
        self._pages: list[dict[str, Any]] = []
        self._index_loaded = self._load_index()

    @property
    def available(self) -> bool:
        return self._index_loaded and self._embedder is not None

    def _load_index(self) -> bool:
        vectors_path = self._data_dir / VECTORS_FILE
        meta_path = self._data_dir / META_FILE
        if not vectors_path.exists() or not meta_path.exists():
            return False

        import numpy as np

        # allow_pickle stays False: the artifact holds only numeric arrays.
        with np.load(vectors_path) as data:
            self._vectors = data["vectors"]
            self._chunk_page = data["chunk_page"]

        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        self._pages = meta.get("pages", [])
        return self._vectors is not None and len(self._pages) > 0

    def get_tool(self) -> types.Tool:
        return types.Tool(
            name=DOCS_TOOL_NAME,
            description=(
                "Search the Appwrite documentation with a natural-language query and "
                "return the most relevant documentation pages with their full content. "
                "Use this for questions about Appwrite concepts, products, and guides "
                "(databases, auth, storage, functions, messaging, sites, and more). "
                "This does not require a project_id."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language documentation query, e.g. 'how do relationships work in databases'.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": DOCS_MAX_LIMIT,
                        "description": f"Maximum number of pages to return. Defaults to {self._default_limit}.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    def search(self, arguments: dict[str, Any] | None) -> list[ToolContent]:
        arguments = arguments or {}
        query = str(arguments.get("query", "")).strip()
        if len(query) < DOCS_MIN_QUERY_LENGTH:
            raise ValueError(
                f"query must be at least {DOCS_MIN_QUERY_LENGTH} characters long."
            )
        if not self.available:
            raise RuntimeError(
                "Documentation search is unavailable: the docs index or OPENAI_API_KEY is not configured."
            )

        limit = _clamp_limit(arguments.get("limit"), self._default_limit)
        try:
            results, embedding_duration_s = self._rank(query, limit)
        except Exception as exc:
            telemetry.record_search_docs(outcome="error", match_count=0)
            raise RuntimeError(
                "Documentation search embedding request failed. "
                "Check OPENAI_API_KEY and outbound connectivity to OpenAI."
            ) from exc
        telemetry.record_search_docs(
            outcome="success",
            match_count=len(results),
            embedding_duration_s=embedding_duration_s,
        )

        if not results:
            return [
                types.TextContent(
                    type="text",
                    text=f'No documentation matched "{query}". Try broader terms.',
                )
            ]

        payload = {"query": query, "results": results}
        return [
            types.TextContent(
                type="text",
                text=json.dumps(payload, indent=2, ensure_ascii=False),
            )
        ]

    def _rank(self, query: str, limit: int) -> tuple[list[dict[str, Any]], float]:
        """Return the ranked pages and the embedding call's duration in seconds."""
        import numpy as np

        # _rank is only reachable when the index is loaded and an embedder exists
        # (guarded by `available`); narrow the optionals for the type checker.
        assert self._embedder is not None
        assert self._vectors is not None
        assert self._chunk_page is not None

        embed_start = time.monotonic()
        embedding = np.asarray(self._embedder(query), dtype=np.float32)
        embedding_duration_s = time.monotonic() - embed_start
        norm = float(np.linalg.norm(embedding))
        if norm == 0.0:
            return [], embedding_duration_s
        embedding /= norm

        scores = self._vectors @ embedding  # cosine similarity (both normalized)

        # Take the top `limit` chunks, then dedupe to pages.
        top_indices = np.argsort(-scores)[:limit]

        results: list[dict[str, Any]] = []
        seen_pages: set[int] = set()
        for index in top_indices:
            score = float(scores[index])
            if score < self._min_score:
                continue
            page_index = int(self._chunk_page[index])
            if page_index in seen_pages:
                continue
            seen_pages.add(page_index)
            page = self._pages[page_index]
            results.append(
                {
                    "path": page.get("path", ""),
                    "title": page.get("title", ""),
                    "description": page.get("description", ""),
                    "score": round(score, 3),
                    "content": page.get("content", ""),
                }
            )
        return results, embedding_duration_s
