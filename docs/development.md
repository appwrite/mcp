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
`http://localhost:8000/` (default `MCP_PUBLIC_URL=http://localhost:8000`;
`/mcp` is also supported):

```bash
docker compose up --build
```

> To enable docs search locally, set `OPENAI_API_KEY` in your shell or `.env`
> before running Compose.

> To enable hosted HTTP error monitoring locally, set `SENTRY_DSN`. You can also
> set `SENTRY_ENVIRONMENT`; Compose defaults it to `development`.

> MCP Events seals each subscription into its Appwrite webhook with the keys in
> `MCP_EVENTS_SEALING_KEYS` (`<id>:<base64 32 bytes>`, comma separated, first one
> active). Generate a key with `openssl rand -base64 32`. To rotate, put a new key
> first and remove the old one once the longest subscription TTL (24h) has passed.

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
| E2E | `uv run --group e2e python -m unittest discover -s tests/e2e -v` | No |
| Integration | `uv run --extra integration python -m unittest discover -s tests/integration -v` | Yes |

E2E tests boot the real hosted HTTP app on a random localhost port and drive it
over real HTTP like an MCP client. Only the OAuth token verifier is stubbed, so
they need no credentials and run on every PR.

Integration tests create and delete **real** Appwrite resources. They
authenticate via `APPWRITE_PROJECT_ID`, `APPWRITE_API_KEY`, `APPWRITE_ENDPOINT`
(shell or `.env`) and are skipped when no credentials are present.

## Debugging

Run the MCP Inspector against a server:

```bash
npx @modelcontextprotocol/inspector
```

To debug the hosted transport, point it at `https://mcp.appwrite.io/` and
complete the OAuth flow when prompted. For self-hosted, start the Inspector in
stdio mode with `uv run mcp-server-appwrite` as the command and the `APPWRITE_*`
env vars above.
