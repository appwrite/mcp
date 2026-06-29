# AGENTS.md

Guidance for AI agents and human contributors working in this repository.

## What this repo is

`mcp-server-appwrite` is a [Model Context Protocol](https://modelcontextprotocol.io)
server for Appwrite. It exposes Appwrite's API to MCP clients as a small set of
operator-style tools, supporting two deployments from one codebase:

- **Cloud (hosted HTTP):** a Starlette ASGI app that acts as an OAuth 2.1
  Resource Server. It validates the client's bearer token and forwards it to the
  Appwrite REST API. Served at `mcp.appwrite.io/mcp`.
- **Self-hosted (`stdio`):** runs locally and authenticates with a project API
  key (`APPWRITE_PROJECT_ID`, `APPWRITE_API_KEY`, `APPWRITE_ENDPOINT`).

Python ≥ 3.12, packaged with `hatchling`, managed with `uv`.

## Architecture

Source lives in `src/mcp_server_appwrite/`:

| File | Responsibility |
| --- | --- |
| `__main__.py` / `server.py` | Entry point, CLI args, transport selection (`--transport stdio\|http`), service registration, low-level MCP server. |
| `http_app.py` | Hosted Streamable-HTTP transport: `/mcp`, RFC 9728 protected-resource metadata, `/healthz`. |
| `auth.py` | OAuth 2.1 resource-server layer — bearer-token validation against the project's Appwrite authorization server. |
| `service.py` | `Service` base class: introspects an Appwrite SDK service and turns its methods into MCP tool definitions. |
| `tool_manager.py` | Registry of all services and their generated tools. |
| `operator.py` | The compact "operator" surface — `appwrite_search_tools`, `appwrite_call_tool`, result/resource storage, write confirmation. |
| `context.py` | `appwrite_get_context` — workspace summary (project, services, account/org for OAuth). |
| `docs_search.py` | In-process semantic docs search (`appwrite_search_docs`) over a prebuilt index. |
| `telemetry.py` | OpenTelemetry metrics layer (OTLP/HTTP). No-op unless an OTLP endpoint is configured and the transport is `http`. |
| `data/` | Committed docs index artifact (`docs_index.npz`, `docs_index_meta.json`), shipped in the wheel/image. |

`scripts/build_docs_index.py` rebuilds the docs index (requires `OPENAI_API_KEY`).

### Telemetry (metrics)

The hosted HTTP server emits OpenTelemetry metrics over OTLP/HTTP to the shared
Appwrite observability stack (OpenTelemetry Collector → Prometheus/Mimir → Grafana
at `telemetry.appwrite.systems`), mirroring the `utopia-php/telemetry` pattern used
by the PHP services. All instrumentation lives in `telemetry.py` and is wired in at
the operator/handler/auth boundaries.

* **Hosted-only & no-op by default.** Telemetry is enabled only when the transport
  is `http` *and* an OTLP endpoint is set. The self-hosted `stdio` transport never
  emits, and an unconfigured hosted server is a silent no-op.
* **Config (env):** `OTEL_EXPORTER_OTLP_ENDPOINT` enables export. Headers come from
  `OTEL_EXPORTER_OTLP_HEADERS`, or — if that is unset — are assembled from
  `CF_ACCESS_CLIENT_ID` + `CF_ACCESS_CLIENT_SECRET` (the shared `telemetry-auth`
  Cloudflare Access secret) into `CF-Access-Client-Id=…,CF-Access-Client-Secret=…`,
  so the deployment passes those two vars directly and reuses the existing secret.
  Set `OTEL_RESOURCE_ATTRIBUTES` to carry
  `deployment.environment.name` / `deployment.region.name` / `deployment.cluster.name`
  so the metrics match the fleet-wide Grafana dashboard variable filters
  (`deployment_environment_name`, etc.).
* **Metrics** are prefixed `mcp.` (e.g. `mcp.requests`, `mcp.appwrite.calls`,
  `mcp.initializations`, `mcp.auth.validations`). User ids (`sub`) are never used as
  labels — distinct-user/-client counts are derived in-process and exposed only as
  the aggregate gauges `mcp.users.active` / `mcp.clients.active`.
* **Dashboards** live in the separate `dashboards` repo under `MCP/`
  (`overview.json`, `adoption.json`).
* **Local check:** run an OTel Collector on `:4318` with a debug exporter, start the
  server with `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 ... --transport http`,
  and confirm metrics appear. Unit tests use an in-memory reader
  (`tests/unit/test_telemetry.py`) — no collector required.

### Tool surface (key design point)

The server boots in a compact workflow: the client sees up to 4 tools
(`appwrite_get_context`, `appwrite_search_tools`, `appwrite_call_tool`, and
optionally `appwrite_search_docs`), while the full Appwrite catalog (25 services)
stays internal and is searched at runtime. Mutating hidden tools require
`confirm_write=true`. Large outputs are stored as MCP resources and returned as a
preview + resource URI.

## Local development

```bash
# Install uv, then sync deps
uv sync                      # runtime deps
uv sync --group dev          # + black, ruff (lint/format)
uv sync --extra integration  # + integration-test deps

# Run hosted HTTP transport
MCP_PUBLIC_URL=http://localhost:8000 APPWRITE_ENDPOINT=https://cloud.appwrite.io/v1 \
  uv run mcp-server-appwrite --transport http

# Run self-hosted stdio transport
APPWRITE_ENDPOINT=http://localhost:9501/v1 \
APPWRITE_PROJECT_ID=<id> APPWRITE_API_KEY=<key> \
  uv run mcp-server-appwrite

# Or via Docker (hosted HTTP/OAuth)
docker compose up --build    # compose.yaml; endpoint at http://localhost:8000/mcp
```

## Pre-PR checklist

Run these locally before opening a PR. They mirror the `CI` workflow
(`.github/workflows/ci.yml`), which runs on every pull request and on pushes to
`main`. **All four jobs must pass.**

1. **Lint** (`lint` job)
   ```bash
   uv sync --group dev
   uv run --group dev ruff check src tests
   ```
   Ruff config: `target-version = py312`, rules `E`, `F`, `W`, `I` (import
   sorting), with `E501` (line length) delegated to black.

2. **Format** (`lint` job)
   ```bash
   uv run --group dev black --check src tests
   ```
   Run `uv run --group dev black src tests` (without `--check`) to auto-fix.

3. **Unit tests** (`unit` job)
   ```bash
   uv sync
   uv run python -m unittest discover -s tests/unit -v
   ```
   Fast, no external services or credentials required.

4. **Docker build** (`docker` job)
   ```bash
   docker build -t appwrite-mcp:ci .
   ```
   The hosted HTTP image must build cleanly.

5. **Integration tests** (`integration` job) — *CI runs these only for pushes and
   for PRs from branches on the same repo (not forks).* They create and delete
   **real** Appwrite resources, so they need live credentials and are skipped
   when absent:
   ```bash
   uv sync --extra integration
   APPWRITE_PROJECT_ID=<id> APPWRITE_API_KEY=<key> APPWRITE_ENDPOINT=<url> \
     uv run --extra integration python -m unittest discover -s tests/integration -v
   ```

### CI environment versions

CI pins Python `3.12` and `uv` `0.11.22`. Match these locally if you hit
version-specific differences.

### If you change the docs index

Rebuilding `src/mcp_server_appwrite/data/` requires `OPENAI_API_KEY`. Re-run the
build script and commit the refreshed artifact:
```bash
OPENAI_API_KEY=sk-... uv run python scripts/build_docs_index.py
```

## Other workflows

- `publish.yml` — package publishing.
- `staging.yml` / `production.yml` — deployment (publishes images to Docker Hub).

These are not gated on PRs the way `ci.yml` is, but be mindful when touching the
`Dockerfile`, `pyproject.toml` version, or deployment config.

## Conventions

- Keep the exposed tool surface small — new Appwrite capabilities should flow
  through the operator/catalog mechanism, not become top-level tools.
- New SDK services are registered automatically; you generally don't hand-write
  tool definitions.
- Match existing style: black formatting, ruff-clean imports, type hints, module
  docstrings explaining intent (see `auth.py`, `http_app.py`, `docs_search.py`).
- Add unit tests under `tests/unit/` for any non-trivial logic; add integration
  coverage under `tests/integration/` when touching real API behavior.
