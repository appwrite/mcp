# Documentation search

`appwrite_search_docs` runs semantic search over the Appwrite documentation
entirely in-process (replacing the standalone docs MCP server). It needs no
`project_id`. Each query is embedded with OpenAI's `text-embedding-3-small` model,
matched against a prebuilt index by cosine similarity, and the top-ranked pages
are returned with their full content.

The index is a single committed artifact, `src/mcp_server_appwrite/data/docs_index.npz`,
shipped in the image. It holds the chunk vectors, the chunk to page map, the
chunk hashes, and the page metadata as an embedded `meta.json` member. The tool
registers **only** when both the artifact and `OPENAI_API_KEY` are available;
otherwise the server boots without it.

## Runtime configuration

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `OPENAI_API_KEY` | Yes | — | Embeds each incoming query (one OpenAI call per search). |
| `DOCS_SEARCH_MIN_SCORE` | No | `0.25` | Minimum cosine score for a match. |
| `DOCS_SEARCH_LIMIT` | No | `5` (max `10`) | Default max pages returned. |

## Rebuilding the index

Re-run when the docs change, then commit the refreshed artifact:

```bash
OPENAI_API_KEY=sk-... uv run python scripts/build_docs_index.py
```

The script fetches the docs as published on appwrite.io: it reads the page
index at `/docs/llms.txt`, downloads each page's Markdown export
(`/docs/<slug>.md`), chunks the pages, embeds the chunks, and writes the
artifact to `data/`. Pages listed in the index but not published (feature-gated
content answering 404) are skipped.

Builds are deterministic: each page stores a hash of its title, description,
and body, and each chunk vector stores the hash of its text. Chunks already
present in the committed artifact reuse their vectors, so an unchanged
documentation set produces a byte-identical file and no spurious commit or
release. The artifact is written to the side and renamed into place, so an
interrupted build cannot leave a partial index behind. Only new or
edited chunks are sent to OpenAI. The script prints the added, changed, and
removed pages, and can write the same report as JSON. Optional build env vars:

| Variable | Default | Purpose |
| --- | --- | --- |
| `DOCS_ORIGIN` | `https://appwrite.io` | Site to fetch the docs from (for example a staging deployment). |
| `DOCS_EMBED_BATCH` | `100` | Embedding batch size. |
| `DOCS_REPORT_FILE` | — | Write the JSON build report (page changes, chunks embedded vs reused) here. |
