# Local development

> Full contributor guide — architecture, conventions, and the pre-PR checklist
> that mirrors CI — lives in [AGENTS.md](../AGENTS.md).

## Clone and install `uv`

```bash
git clone https://github.com/appwrite/mcp.git
cd mcp
# Linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (PowerShell)
# powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## Transports at a glance

| Transport | Auth | Use case |
| --- | --- | --- |
| `http` (hosted) | OAuth 2.1 bearer token | Cloud / production parity |
| `stdio` (self-hosted) | Project API key | Local self-hosted dev |

## Run the server

**Docker Compose** — hosted HTTP/OAuth transport, endpoint at
`http://localhost:8000/mcp` (default `MCP_PUBLIC_URL=http://localhost:8000`):

```bash
docker compose up --build
```

> To enable docs search locally, set `OPENAI_API_KEY` in your shell or `.env`
> before running Compose.

**`uv` directly — HTTP:**

```bash
MCP_PUBLIC_URL=http://localhost:8000 APPWRITE_ENDPOINT=https://cloud.appwrite.io/v1 \
  uv run mcp-server-appwrite --transport http
```

**`uv` directly — self-hosted stdio:**

```bash
APPWRITE_ENDPOINT=http://localhost:9501/v1 \
APPWRITE_PROJECT_ID=<YOUR_PROJECT_ID> \
APPWRITE_API_KEY=<YOUR_API_KEY> \
  uv run mcp-server-appwrite
```

## Testing

| Suite | Command | Needs credentials |
| --- | --- | --- |
| Unit | `uv run python -m unittest discover -s tests/unit -v` | No |
| Integration | `uv run --extra integration python -m unittest discover -s tests/integration -v` | Yes |

Integration tests create and delete **real** Appwrite resources. They
authenticate via `APPWRITE_PROJECT_ID`, `APPWRITE_API_KEY`, `APPWRITE_ENDPOINT`
(shell or `.env`) and are skipped when no credentials are present.

## Debugging

Run the MCP Inspector against a server:

```bash
npx @modelcontextprotocol/inspector
```

To debug the hosted transport, point it at `https://mcp.appwrite.io/mcp` and
complete the OAuth flow when prompted. For self-hosted, start the Inspector in
stdio mode with `uv run mcp-server-appwrite` as the command and the `APPWRITE_*`
env vars above.
