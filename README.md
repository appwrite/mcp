# Appwrite MCP server

mcp-name: io.github.appwrite/mcp-for-api

## Overview

A Model Context Protocol server for interacting with Appwrite's API. It provides tools to manage databases, users, functions, teams, and more within your Appwrite project.

The server is a hosted, multi-tenant [OAuth 2.1 Resource Server](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization) served over the MCP [Streamable HTTP](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) transport. A single deployment serves every project; users authenticate with Appwrite Cloud's OAuth 2.1 authorization server, and no API keys are distributed to clients.

## Quick Links
- [Connecting a client](#connecting-a-client)
- [How authentication works](#how-authentication-works)
- [Project setup](#project-setup)
- [Tool surface](#tool-surface)
- [Self-hosting](#self-hosting)
- [Local development](#local-development)
- [Debugging](#debugging)

## Connecting a client

Add the server to any MCP client that supports remote (Streamable HTTP) servers by its per-project URL — the project ID goes in the path:

```
https://<your-mcp-host>/<project_id>/mcp
```

For example, in a client that accepts a JSON server config:

```json
{
  "mcpServers": {
    "appwrite": {
      "type": "http",
      "url": "https://<your-mcp-host>/<project_id>/mcp"
    }
  }
}
```

The first time you connect, the client opens an Appwrite consent screen in your browser. Approve the requested scopes and the client is connected — there are no keys to copy.

## How authentication works

The MCP server validates the bearer access token on every request and forwards it to the Appwrite REST API, which accepts the OAuth2 access token directly. The flow (handled automatically by MCP-aware clients):

1. The client requests `/<project_id>/mcp` without a token and receives `401` with a `WWW-Authenticate` header pointing to the protected-resource metadata.
2. The client fetches `GET /.well-known/oauth-protected-resource/<project_id>/mcp` (RFC 9728), which lists the project's authorization server (`<APPWRITE_ENDPOINT>/oauth2/<project_id>`) and supported scopes.
3. The client discovers the authorization server (RFC 8414 / OIDC) and **self-registers** via RFC 7591 Dynamic Client Registration — the project's OAuth server exposes an open `registration_endpoint`, so there is no client ID or secret to pre-provision. MCP clients register as public (PKCE) clients automatically.
4. The client runs the OAuth 2.1 + PKCE authorization-code flow, including the RFC 8707 `resource` parameter that binds the token's audience to this server.
5. The client calls `/<project_id>/mcp` with `Authorization: Bearer <token>`.

## Project setup

Each project must enable its OAuth server (`oAuth2Server.enabled = true`) and include the scopes the MCP advertises in `oAuth2Server.scopes`. The advertised set covers read+write for users, sessions, teams, databases (tables/columns/indexes/rows), storage (buckets/files), functions (executions), messaging (providers/topics/subscribers/targets/messages), and sites, plus `locale.read` and `avatars.read`. Clients request only the subset they need; the consent screen and the API's per-route scope checks enforce what is actually granted.

No OAuth client needs to be created by hand: enabling the OAuth server also exposes the RFC 7591 `registration_endpoint`, and MCP clients self-register against it on first connect. Each registered client becomes an `apps` document in the project.

## Tool surface

The server starts in a compact workflow so the MCP client only sees a small operator-style surface while the full Appwrite catalog stays internal.

- Only 2 MCP tools are exposed to the model:
  - `appwrite_search_tools`
  - `appwrite_call_tool`
- The full Appwrite tool catalog stays internal and is searched at runtime.
- Large tool outputs are stored as MCP resources and returned as preview text plus a resource URI.
- Mutating hidden tools require `confirm_write=true`.
- Every Appwrite service the installed SDK ships is registered automatically (account, databases, tablesDB, users, teams, storage, functions, messaging, sites, tokens, locale, avatars, graphql, health).

## Self-hosting

The server is a standard ASGI app. Configure it via environment variables (see [`.env.example`](.env.example)) — chiefly `APPWRITE_ENDPOINT` (the Appwrite Cloud base) and `MCP_PUBLIC_URL` (the external URL clients use to reach this server, used to build canonical resource URIs and metadata). `GET /healthz` is a liveness probe.

> **Regional endpoints:** `APPWRITE_ENDPOINT` must be the host that actually **issues** tokens. The server validates each token's `iss` claim against this endpoint, so if your project lives in a region whose discovery reports a regional issuer (e.g. `https://fra.cloud.appwrite.io/v1`), set `APPWRITE_ENDPOINT` to that regional host — otherwise valid tokens are rejected.

### Docker (recommended)

```bash
docker build -t appwrite-mcp .
docker run -p 8000:8000 \
  -e MCP_PUBLIC_URL=https://<your-mcp-host> \
  -e APPWRITE_ENDPOINT=https://cloud.appwrite.io/v1 \
  appwrite-mcp
```

### From the package

```bash
pip install mcp-server-appwrite
MCP_PUBLIC_URL=https://<your-mcp-host> APPWRITE_ENDPOINT=https://cloud.appwrite.io/v1 \
  mcp-server-appwrite        # serves Streamable HTTP on $HOST:$PORT (default 0.0.0.0:8000)
```

## Local development

### Clone and install `uv`

```bash
git clone https://github.com/appwrite/mcp-for-api.git
cd mcp-for-api
# Linux or MacOS
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
# powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### Run the server

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

Point it at `https://<your-mcp-host>/<project_id>/mcp` and complete the OAuth flow when prompted.

## License

This MCP server is licensed under the MIT License. This means you are free to use, modify, and distribute the software, subject to the terms and conditions of the MIT License. For more details, please see the LICENSE file in the project repository.
