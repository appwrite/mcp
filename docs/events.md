# Events

> **In progress.** Events are being built behind the `--events` flag
> ([#127](https://github.com/appwrite/mcp/issues/127)). Today the server
> advertises the capability, lists the catalog, and receives Appwrite webhooks
> and forwards them as events; `events/subscribe` and `events/unsubscribe` are
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

## Ingress

Each subscription is one Appwrite webhook in the subscriber's project. It points
at `{MCP_PUBLIC_URL}/appwrite/webhooks/{subscription id}` and carries the sealed
subscription envelope as its Basic auth password, so Appwrite hands the
subscription back with every delivery and the server keeps no store. The route
is mounted only with the events flag on and the HTTP transport, and it is not
behind the bearer-token gate: the Appwrite signature is its authentication.

For each delivery the ingress:

1. Reads the envelope from Basic auth and takes its sealing key id.
2. Verifies `X-Appwrite-Webhook-Signature`. Appwrite signs
   `base64(HMAC-SHA1(url + body))` with the webhook secret, where `url` is the
   URL the webhook was registered with. The ingress rebuilds that URL from
   `MCP_PUBLIC_URL` and the path and ignores the inbound `Host`. The secret is
   derived from the subscription id and the envelope's sealing key. The body is
   hashed as it streams in; up to 2 MiB of it is kept.
3. Opens the envelope for the subscription id and
   `X-Appwrite-Webhook-Project-Id`. The envelope is bound to both, so one copied
   to another webhook or project does not open.
4. Checks expiry, checks that `X-Appwrite-Webhook-Events` contains one of the
   subscription's Appwrite patterns, and projects the body onto the event's
   payload schema. Only the declared fields are forwarded, never the raw body:
   Appwrite sends whole rows, users with email and phone, and executions with
   request headers and logs.
5. Starts the delivery in a background task and answers `200` straight away,
   well inside Appwrite's 15 second timeout. The delivery is signed with
   Standard Webhooks and retried in-process (see `events/delivery.py`).

`eventId` is `evt_` + a SHA-256 over `X-Appwrite-Webhook-Delivery-Id` and the
subscription id. Appwrite keeps the delivery id across its own retries, so a
retried webhook produces the same `eventId` and receivers can dedupe it. The
event `timestamp` is when the event happened according to the body (completion
for executions and deployments, creation otherwise), or the receive time when
the body has none.

### Mapping Appwrite bodies

| Event | Appwrite body | Forwarded when |
| --- | --- | --- |
| `functions.execution.failed` | Execution: `resourceType` must be `functions` and `resourceId` the subscribed function | `status` is `failed`. Synchronous runs fire only `.create`, already finished. Asynchronous runs fire `.create` while `waiting` (dropped) and `.update` when done. |
| `functions.deployment.completed`, `sites.deployment.completed` | Deployment: `resourceType` `functions` / `sites`, `resourceId` the subscribed resource | `status` is `ready` or `failed` (or the requested one). Activating a deployment fires the same event with the function or site model, which has no `resourceType` and is dropped. A duplicated deployment is still `waiting` and is dropped. |
| `tablesdb.row.created` | Row: `$databaseId` and `$tableId`, when present, must match | always |
| `storage.file.created` | File: `bucketId`, when present, must match | always |
| `users.user.created` | User | always; only `user_id` and `created_at` are forwarded |

Timestamps arrive either in the response-model format or as raw database values
without a zone (`2026-10-09 09:38:26.634`, which is UTC), and are sometimes
missing (asynchronous execution updates have no `$createdAt`). They are
normalized to ISO 8601 UTC with milliseconds (`2026-10-09T09:38:26.634Z`) or
`null`.

### Always answer 2xx

Appwrite counts every response `>= 400`, and after 10 in a row it pauses the
webhook and emails the project owner. So anything that comes from the
subscriber's own webhook is answered with `200`, delivered or not, and only a
request that fails authentication gets `401`.

| Case | Response | Delivered |
| --- | --- | --- |
| Valid, matching event | `200` `{"status":"accepted","eventId":…}` | yes |
| Subscription expired | `200` dropped `expired` | no |
| Envelope sealed with a key no longer in the ring | `200` dropped `retired_key` | no |
| Event name not in the catalog | `200` dropped `unknown_event` | no |
| Appwrite event outside the subscription's patterns | `200` dropped `event_mismatch` | no |
| Body names another resource | `200` dropped `resource_mismatch` | no |
| Body is not the expected model (deployment activation) | `200` dropped `shape` | no |
| Status filter does not match (`waiting`, `ready` for a `failed` filter) | `200` dropped `status` | no |
| Body is not a JSON object | `200` dropped `malformed` | no |
| Body over 2 MiB (signature still checked) | `200` dropped `too_large` | no |
| No Basic auth, or the password is not an envelope | `401` `credentials` | no |
| Signature missing or wrong | `401` `signature` | no |
| Envelope tampered with, or for another webhook or project | `401` `envelope` | no |

A legitimate webhook cannot get a `401` in normal operation:

- **Key rotation.** A new sealing key goes first in `MCP_EVENTS_SEALING_KEYS`
  and the old one stays until the longest subscription TTL has passed, so
  webhooks written with the old key still verify and open. After the old key is
  removed, only expired subscriptions still name it. Their webhooks are orphans
  awaiting cleanup, so they get `200` (`retired_key`) instead of being paused.
- **Public URL.** The signature covers the registered URL. Changing
  `MCP_PUBLIC_URL` breaks every subscription until it refreshes, so keep it
  fixed while subscriptions exist.
- **Edited webhooks.** Changing the webhook's password, secret or URL in the
  Console does produce `401`s. The subscription is broken either way, and
  Appwrite pausing it is the right result.

Deliveries run in a task group owned by the app lifespan, next to the MCP
session manager. One `Egress` and one `Dispatcher` serve the whole process and
are closed on shutdown. A delivery still retrying when the process stops is
lost: delivery is at most once, like Appwrite webhooks themselves.

### Metrics

| Metric | Attributes |
| --- | --- |
| `mcp.events.ingress` | `outcome` (`accepted`, `dropped`, `rejected`), `reason`, `event` |
| `mcp.events.deliveries` | `event`, `outcome` (`delivered`, `rejected`, `abandoned`, `too_large`, `error`), `reason` |

## Testing

Events are tested end to end in `tests/e2e/`: the real hosted app runs under
uvicorn on a random localhost port, and the tests talk to it over HTTP exactly
like ChatGPT does (`MCP-Protocol-Version: 2026-07-28`, `Mcp-Method`, `_meta`).
Only the OAuth token verifier is stubbed, because Cloud OAuth is not reachable
from CI. The suite needs no credentials and runs on every PR:

```bash
uv run --group e2e python -m unittest discover -s tests/e2e -v
```

A live end-to-end run against Cloud staging (real OAuth tokens, real Appwrite
webhooks) is planned once
[appwrite/appwrite#14293](https://github.com/appwrite/appwrite/issues/14293)
ships; until then the server's surroundings are played locally.
