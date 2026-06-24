# Appwrite MCP server

mcp-name: io.github.appwrite/mcp

## Overview

A Model Context Protocol server for interacting with Appwrite's API. It provides tools to manage databases, users, functions, teams, and more within your Appwrite project.

The server is a hosted [OAuth 2.1 Resource Server](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization) served over the MCP [Streamable HTTP](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) transport. It targets the Appwrite Cloud console project; users authenticate with that project's OAuth 2.1 authorization server, and no API keys are distributed to clients.

## Quick Links
- [Connecting a client](#connecting-a-client)
- [How authentication works](#how-authentication-works)
- [Tool surface](#tool-surface)
- [Local development](#local-development)
- [Debugging](#debugging)

## Connecting a client

Add the server to any MCP client that supports remote (Streamable HTTP) servers by its URL:

```
https://mcp.appwrite.io/mcp
```

For example, in a client that accepts a JSON server config:

```json
{
  "mcpServers": {
    "appwrite": {
      "type": "http",
      "url": "https://mcp.appwrite.io/mcp"
    }
  }
}
```

The first time you connect, the client opens an Appwrite consent screen in your browser. Approve the requested scopes and the client is connected — there are no keys to copy.

## How authentication works

The MCP server validates the bearer access token on every request and forwards it to the Appwrite REST API, which accepts the OAuth2 access token directly. The flow (handled automatically by MCP-aware clients):

1. The client requests `/mcp` without a token and receives `401` with a `WWW-Authenticate` header pointing to the protected-resource metadata.
2. The client fetches `GET /.well-known/oauth-protected-resource/mcp` (RFC 9728), which lists the authorization server (`<APPWRITE_ENDPOINT>/oauth2/console`) and supported scopes.
3. The client discovers the authorization server (RFC 8414 / OIDC) and **self-registers** via RFC 7591 Dynamic Client Registration — the OAuth server exposes an open `registration_endpoint`, so there is no client ID or secret to pre-provision. MCP clients register as public (PKCE) clients automatically.
4. The client runs the OAuth 2.1 + PKCE authorization-code flow, including the RFC 8707 `resource` parameter that binds the token's audience to this server.
5. The client calls `/mcp` with `Authorization: Bearer <token>`.

## Tool surface

The server starts in a compact workflow so the MCP client only sees a small operator-style surface while the full Appwrite catalog stays internal.

- Up to 3 MCP tools are exposed to the model:
  - `appwrite_search_tools`
  - `appwrite_call_tool`
  - `appwrite_search_docs` — semantic search over the Appwrite documentation (only registered when the docs index and `OPENAI_API_KEY` are present; see [Documentation search](#documentation-search)).
- The full Appwrite tool catalog stays internal and is searched at runtime.
- Large tool outputs are stored as MCP resources and returned as preview text plus a resource URI.
- Mutating hidden tools require `confirm_write=true`.
- Every Appwrite service the installed SDK ships is registered automatically — 25 in total, each becoming a tool-name prefix: `account`, `activities`, `advisor`, `apps`, `avatars`, `backups`, `databases`, `functions`, `graphql`, `health`, `locale`, `messaging`, `oauth2`, `organization`, `presences`, `project`, `proxy`, `sites`, `storage`, `tables_db`, `teams`, `tokens`, `usage`, `users`, and `webhooks`. Which ones a given user can actually call is gated by the scopes their OAuth token was granted (enforced per-route by the Appwrite API), not by the catalog.

## Documentation search

`appwrite_search_docs` runs semantic search over the Appwrite documentation entirely in-process, replacing the standalone docs MCP server. It embeds the query with OpenAI `text-embedding-3-small` and ranks a prebuilt index of doc pages by cosine similarity, returning the most relevant pages with their full content. It needs no `project_id`.

The index is a small artifact committed under `src/mcp_server_appwrite/data/` (`docs_index.npz` + `docs_index_meta.json`) and shipped in the image. The tool is registered only when both the artifact and `OPENAI_API_KEY` are available; otherwise the server boots without it.

### Runtime configuration

- `OPENAI_API_KEY` — required to embed incoming queries (one OpenAI call per search).
- `DOCS_SEARCH_MIN_SCORE` — minimum cosine score for a match (default `0.25`).
- `DOCS_SEARCH_LIMIT` — default maximum pages returned (default `5`, max `10`).

### Rebuilding the index

Re-run the build script when the docs change and commit the refreshed artifact:

```bash
OPENAI_API_KEY=sk-... uv run python scripts/build_docs_index.py
```

It downloads `appwrite/website` docs from GitHub, chunks each page, embeds the chunks, and writes the artifact. Optional env vars: `DOCS_WEBSITE_REF` (git ref, default `main`), `DOCS_EMBED_BATCH` (default `100`).

## Local development

### Clone and install `uv`

```bash
git clone https://github.com/appwrite/mcp.git
cd mcp
# Linux or MacOS
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
# powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### Run the server

With Docker Compose:

```bash
docker compose up --build
```

Compose defaults to `MCP_PUBLIC_URL=http://localhost:8000` and exposes the MCP endpoint at:

```text
http://localhost:8000/mcp
```

To enable documentation search locally, provide `OPENAI_API_KEY` in your shell or a local `.env` file before running Compose.

With `uv` directly:

```bash
MCP_PUBLIC_URL=http://localhost:8000 APPWRITE_ENDPOINT=https://cloud.appwrite.io/v1 \
  uv run mcp-server-appwrite
```

## Testing

### Unit tests

```bash
uv run python -m unittest discover -s tests/unit -v
```

### Live integration tests

These create and delete real Appwrite resources against a real project. They authenticate to the Appwrite API with an API key supplied via the environment or `.env` (`APPWRITE_PROJECT_ID`, `APPWRITE_API_KEY`, `APPWRITE_ENDPOINT`) and are skipped when no credentials are present.

```bash
uv run --extra integration python -m unittest discover -s tests/integration -v
```

## Debugging

Use the MCP Inspector against a running server URL:

```bash
npx @modelcontextprotocol/inspector
```

Point it at `https://mcp.appwrite.io/mcp` and complete the OAuth flow when prompted.

## License

This MCP server is licensed under the MIT License. This means you are free to use, modify, and distribute the software, subject to the terms and conditions of the MIT License. For more details, please see the LICENSE file in the project repository.
