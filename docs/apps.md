# MCP Apps

[MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview) (SEP-1865,
extension `io.modelcontextprotocol/ui`) let a tool result render as an
interactive view inside the chat. This server offers one view,
`ui://appwrite/view`, behind the `apps` flag (`MCP_APPS=1`, both transports).

## Surface

| Name | Seen by | What it does |
| --- | --- | --- |
| `appwrite_show` | model and view | `view=table`: a page of a table's rows, newest first, with `cursor` paging. `view=deployment`: a Function or Site deployment (the latest unless `deployment_id` is given) with the last 200 build log lines. `view=executions`: a Function's executions, newest first, optionally one `status`, with `cursor` paging; `execution_id` also opens one with its request and response headers, status code, duration, and the last 200 lines of logs and of errors. Read-only. |
| `appwrite_view_write` | view only (`visibility: ["app"]`) | Runs one of `tables_db_update_row`, `functions_update_deployment_status`, `sites_update_deployment_status`, only with `confirm_write=true`. |
| `ui://appwrite/view` | host | One HTML document (`assets/view.html`) that renders every view. |

`appwrite_show` returns a text summary in `content`, which is what the model and
hosts without MCP Apps see, and the data in `structuredContent` for the view.
The view calls `appwrite_show` again to page, refresh and follow a build (every
3 seconds until it is ready, failed or canceled), and to follow executions
while any on the page, or the opened one, is waiting or processing. Appwrite
keeps an execution's response body only for the synchronous call that ran it,
so the view shows it only when present. An edited row or a cancelled
build goes through a confirmation dialog in the view, and only then through
`appwrite_view_write`. A successful edit and a finished build are passed to the
model with `ui/update-model-context` when the host supports it.

## Who is offered the view

Hosts that declare the extension in their client capabilities get the two tools
and the resource. Hosts that declare capabilities without it do not. The
hosted transport is stateless, so a 2025-era client's `tools/list` carries no
capabilities; it is offered the view, and a host that cannot render it uses the
text summary. 2026-07-28 clients send `clientCapabilities` with every request,
and stdio has the handshake, so both are gated exactly. `tools/call` is not
gated, because the view's own calls reach the server through the host.

## Security

- The resource declares no CSP domains, so hosts apply the spec's restrictive
  default: no network access, inline script and style only. The view never
  holds a token; every Appwrite call is a `tools/call` the host proxies to this
  server with the user's own credentials.
- Everything the view renders comes from Appwrite. It builds the DOM with
  `textContent` and `createElement` only (the e2e flow fails on `innerHTML`),
  because a script injected through that data could call `appwrite_view_write`.
  The allowlist bounds what such a script could do, and `confirm_write` is
  required as for `appwrite_call_write_tool`.

## Trying it in a real host

The ext-apps repository ships `basic-host`, the reference host. It connects
without auth, so point it at a local server whose requests carry a token, for
example the e2e harness (`tests/e2e/support.py`) with its fake Appwrite:

```bash
git clone --depth 1 https://github.com/modelcontextprotocol/ext-apps.git
cd ext-apps/examples/basic-host && npm install
NODE_ENV=development npx cross-env INPUT=index.html vite build
NODE_ENV=development npx cross-env INPUT=sandbox.html vite build
SERVERS='["http://localhost:3001/mcp"]' bun serve.ts   # host on :8080
```

Pick `appwrite_show`, call it with
`{"view":"table","project_id":"…","database_id":"…","table_id":"…"}`, and the
view renders under the tool call. The host's theme toggle switches the view
between light and dark.
