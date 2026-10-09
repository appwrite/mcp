# Events

> **In progress.** Events are being built behind the `--events` flag
> ([#127](https://github.com/appwrite/mcp/issues/127)). Today the server
> advertises the capability and lists the catalog; subscribing and delivery are
> not available yet.

[MCP Events](https://developers.openai.com/plugins/build/mcp-events) let an MCP
client such as ChatGPT subscribe to things that happen in an Appwrite project
and receive a signed webhook when they occur, for example "when a function
execution fails in project X, read the logs and open a fix".

Events are served only by the hosted HTTP server (`mcp.appwrite.io`), and v1
delivers only by webhook. Each subscription will be backed by one Appwrite
webhook in the subscriber's project, so the MCP server keeps no subscription
store.

## Enabling

```bash
MCP_PUBLIC_URL=http://localhost:8000 \
  uv run mcp-server-appwrite --transport http --events 1
```

`MCP_EVENTS=1` works too. See [flags.md](flags.md#--events--mcp-events-in-progress)
for quick checks with `curl`.

With the flag on, the server advertises the capability in both shapes that
clients read today:

| Where | Read by |
| --- | --- |
| `capabilities.events: {}` | ChatGPT |
| `capabilities.extensions["io.modelcontextprotocol/events"]: {"listChanged": false}` | SEP-3415 clients |

The `mcp` SDK does not model this capability yet
([python-sdk#3640](https://github.com/modelcontextprotocol/python-sdk/issues/3640)),
so a server middleware adds it to the `server/discover` and `initialize`
results.

## Catalog

`events/list` returns these events. Every event takes a required `project_id`.
ID arguments accept 1–36 letters, digits, `_` or `-`, so a `*` or `.` can never
widen a subscription beyond the named resource. Payloads carry IDs and metadata
only; the agent fetches details with `appwrite_call_tool`.

| Event | Arguments | Appwrite events | Delivered when | Payload |
| --- | --- | --- | --- | --- |
| `functions.execution.failed` | `function_id` | `functions.{function_id}.executions.*.update`, `functions.{function_id}.executions.*.create` | status is `failed` | `function_id`, `execution_id`, `status`, `trigger`, `response_status_code`, `created_at` |
| `functions.deployment.completed` | `function_id`, `status?` (`ready` \| `failed`) | `functions.{function_id}.deployments.*.update` | status is `ready` or `failed` (or the requested one) | `function_id`, `deployment_id`, `status`, `updated_at` |
| `sites.deployment.completed` | `site_id`, `status?` (`ready` \| `failed`) | `sites.{site_id}.deployments.*.update` | status is `ready` or `failed` (or the requested one) | `site_id`, `deployment_id`, `status`, `updated_at` |
| `tablesdb.row.created` | `database_id`, `table_id` | `tablesdb.{database_id}.tables.{table_id}.rows.*.create` | always | `database_id`, `table_id`, `row_id`, `created_at` |
| `storage.file.created` | `bucket_id` | `buckets.{bucket_id}.files.*.create` | always | `bucket_id`, `file_id`, `mime_type`, `size`, `created_at` |
| `users.user.created` | none | `users.*.create` | always | `user_id`, `created_at` |

Synchronous function executions fire only `.create`, after they finish, which
is why `functions.execution.failed` listens to both. Executions served through
function domains fire no Appwrite event and are not covered.

Timestamps are ISO 8601 strings and may be `null`. The catalog lives in
[`events/catalog.py`](../src/mcp_server_appwrite/events/catalog.py).

## Errors

Error codes follow the values ChatGPT uses today. SEP-3415 renumbers them, so
they are kept behind constants in
[`events/errors.py`](../src/mcp_server_appwrite/events/errors.py).

| Meaning | Code | `data` |
| --- | --- | --- |
| Bad arguments, URL or secret | `-32602` | |
| NotFound | `-32011` | `kind` |
| Forbidden | `-32012` | |
| ResourceExhausted | `-32013` | `limit`, `max` |
| Unsupported | `-32014` | |
| CallbackEndpointError | `-32015` | `reason`: `challenge_failed`, `timeout`, `connection_refused`, `tls_error`, `http_4xx`, `http_5xx` |
