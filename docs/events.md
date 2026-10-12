# Events

> **Not a supported feature yet.** Events sit behind the `--events` testing
> flag ([#127](https://github.com/appwrite/mcp/issues/127)), which is off in
> production. They cannot work on Appwrite Cloud until
> [appwrite/appwrite#14293](https://github.com/appwrite/appwrite/issues/14293)
> lets a webhook's `authPassword` hold the sealed subscription.

[MCP Events](https://developers.openai.com/plugins/build/mcp-events) let an MCP
client such as ChatGPT subscribe to things that happen in an Appwrite project
and receive a signed webhook when they occur, for example "when a function
execution fails in project X, read the logs and open a fix".

Events are served only by the hosted HTTP server (`mcp.appwrite.io`), and v1
delivers only by webhook. Each subscription is backed by one Appwrite webhook in
the subscriber's project, so the MCP server keeps no subscription store.

## Testing flag

For testers only: [flags.md](flags.md#--events--mcp-events-in-progress) shows
how to turn the flag on locally and check it with `curl`.

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

Failures on our side, including Appwrite answering `5xx`, are `-32603`.

## Subscribe and unsubscribe

```
events/subscribe   {name, arguments, delivery: {mode: "webhook", url, secret}, cursor, ttlMs?, maxAgeMs?}
                -> {id, refreshBefore, cursor: null, truncated: false}
events/unsubscribe {name, arguments, delivery: {url}}
                -> {}
```

The server keeps nothing. A subscription is one Appwrite webhook in the
subscriber's project, written with the subscriber's own OAuth token, and the
subscription id is that webhook's `$id`: `sub_` + 32 hex characters of a
SHA-256 over the principal (OAuth `iss`, `sub`, `client_id`), the callback URL,
the event name and the arguments. Subscribing again with the same values is a
refresh of the same webhook, and two users subscribing the same URL get two
subscriptions.

`events/subscribe`, in order:

1. Looks the event up (`-32011`, `data.kind = "event"`) and checks its
   arguments (`-32602`). `delivery.mode` must be `webhook` (`-32014`). The
   callback must be `https`, without credentials, and resolve only to public
   addresses (`-32602`). The secret must be `whsec_` + base64 of 24 to 64 bytes
   (`-32602`).
2. Needs a user behind the token: no `sub` is `-32012`. No token at all never
   reaches JSON-RPC (HTTP `401`). A token whose granted scopes include none of
   `project:webhooks.write`, `project:all` or `all` is `-32012` here, before
   the callback gets any request (listing webhooks below proves only read
   access, and Appwrite has no side-effect-free write check).
3. Grants a TTL and seals the subscription (see [Envelope](#envelope)). A
   callback URL too long for the envelope is `-32602`, and so is a delivery the
   subscription record refuses (a URL with a fragment, a secret whose base64
   is not canonical).
4. Reads the event's resource in the project with the caller's token:

   | Event | Read | Scope |
   | --- | --- | --- |
   | `functions.execution.failed`, `functions.deployment.completed` | `GET /functions/{function_id}` | `functions.read` |
   | `sites.deployment.completed` | `GET /sites/{site_id}` | `sites.read` |
   | `tablesdb.row.created` | `GET /tablesdb/{database_id}/tables/{table_id}` | `tables.read` |
   | `storage.file.created` | `GET /storage/buckets/{bucket_id}` | `buckets.read` |
   | `users.user.created` | `GET /users` with `limit(1)` | `users.read` |

   Appwrite checks access to the project before it looks the resource up. So a
   `404` for the resource means the caller is in the project and it does not
   exist: NotFound with `data.kind = "resource"`. A `401` or `403` (no access
   to the project, or a missing scope) is Forbidden (`-32012`), and so is a
   project that does not exist, so project ids cannot be probed. Every refresh
   repeats the read, so revoked access ends a subscription at its next refresh
   at the latest.
5. Lists the project's webhooks, which also proves the webhook scopes, and
   deletes this principal's long-expired subscriptions (see below).
6. Runs the verification handshake: a signed
   `{"type":"verification","challenge":…}` that the callback must echo in a
   `2xx`. A failure is `-32015` with the reason. The handshake runs on every
   subscribe and refresh: the server has no verification cache, and it cannot
   read the previous subscription back from Appwrite to know whether the
   callback or secret changed. One extra signed POST per refresh is cheap, and
   it proves the callback still wants deliveries under the secret it is about
   to get.
7. Creates the webhook, or rewrites it on a refresh (which also re-enables a
   webhook Appwrite paused after failed deliveries).

`events/unsubscribe` recomputes the id from the caller and the request and
deletes that webhook if it is a managed one. It returns `{}` when there was
nothing to delete, so it is idempotent, and it can only ever reach the caller's
own subscriptions because the id includes the principal. A project that does
not exist is `-32012`, as on subscribe, so project ids cannot be probed.

### TTL

| Suggestion (`ttlMs`) | Grant |
| --- | --- |
| absent or `null` | 1 hour. `null` asks for no expiry, which needs durable storage this server does not have. |
| below 5 minutes | 5 minutes (floor) |
| 5 minutes to 24 hours | as suggested |
| above 24 hours | 24 hours (cap) |

The subscription expires when the grant runs out. `refreshBefore` is a tenth of
the grant earlier (6 minutes for the default hour, 30 seconds at the floor), so
a slow refresh never lets deliveries lapse. The cap also bounds how long revoked
access can keep receiving events. `refreshBefore` is never `null`. `cursor` is
always `null` and `truncated` always `false`: Appwrite webhooks cannot replay,
so `maxAgeMs` is accepted and has no effect.

### The managed webhook

| Field | Value |
| --- | --- |
| `$id` | The subscription id |
| `name` | `MCP events \| <event> \| until <expiry, UTC> \| owner <first 8 characters of the principal digest>` |
| `url` | `{MCP_PUBLIC_URL}/appwrite/webhooks/{id}` |
| `events` | The event's Appwrite patterns |
| `tls` | `true` |
| `authUsername` | `mcp-events`, a fixed non-secret value. Appwrite sends Basic auth only when both username and password are set. |
| `authPassword` | The sealed envelope, and nothing else |
| `secret` | The derived signing key for `X-Appwrite-Webhook-Signature` |

Appwrite never returns `authPassword`, so the envelope cannot be read back. The
name carries the two facts cleanup needs instead: when the subscription expires
and whose it is. It is ASCII because Appwrite sends it back in the
`X-Appwrite-Webhook-Name` header. A webhook counts as managed only if its id is
a subscription id and its name parses as this label. Anything else in the
project is never changed or deleted, and a webhook that has a subscription's id
but a different name (renamed in the Console) makes subscribe fail with
`-32012` rather than be overwritten.

Before writing, subscribe deletes the caller's managed webhooks whose label says
they expired more than 10 minutes ago. It only touches the caller's own (matched
by the owner tag) and only long-expired ones, so a tag collision between two
users could at worst remove an expired webhook whose deliveries the ingress
already drops. Appwrite has no conditional delete, so a late refresh of that
same subscription could race the cleanup. The grace period keeps a subscription
that is still being refreshed out of the candidates, and each candidate is read
again right before the delete and kept if its name or `$updatedAt` changed
since the listing. What remains is a refresh landing between that read and the
delete, of a subscription left expired for over 10 minutes; its next refresh
creates the webhook again. Expired
subscriptions of a user who never comes back stay until that user subscribes
again in the project, or the webhook is deleted in the Console. Appwrite keeps
delivering to them, and the ingress answers `200` and drops them.

### Secret rotation

A refresh with a new `delivery.secret` replaces the secret. The previous one
cannot be kept for dual signing, because it exists only in the old envelope,
which Appwrite never returns. Deliveries already in flight were opened from the
old envelope, so their retries keep signing with the old secret. Receivers
should accept both secrets for a short while after rotating, as Standard
Webhooks receivers do.

### Failure handling

| Appwrite answer | Result |
| --- | --- |
| `401 general_unauthorized_scope` on `/webhooks` | `-32012`, naming `project:webhooks.read` and `project:webhooks.write` |
| `401` / `403` otherwise on `/webhooks` | `-32012` |
| `404 project_not_found` on `/webhooks` (unsubscribe from a missing project) | `-32012` |
| `403 additional_resource_not_allowed` (plan limit) | `-32013`, `data: {"limit": "webhooks", "max": <webhooks in the project>}` |
| `5xx`, or anything else | `-32603` |

Creating a webhook is a single call. A refresh sets the signing key first,
because the update endpoint does not take it, and then rewrites the webhook
with the new envelope. If that second call fails, the webhook keeps its previous
envelope with the new signing key. The ingress accepts the signing key of every
ring key, and the previous envelope's sealing key stays in the ring until that
envelope expires, so deliveries continue; the error (`-32603`) tells the client
to retry. In the other order, a failure could leave a new envelope with a
signing key derived from a sealing key that is retired while the envelope is
still live, and deliveries would be rejected until the next refresh.

### Free plan

Appwrite Cloud allows 2 webhooks per project on Free, 50 on Start and unlimited
on Pro and Scale, and every subscription uses one. At the limit, subscribe
returns `-32013` and tells Free users to upgrade to Pro or unsubscribe from
another event. Cleanup runs before the create, so expired subscriptions of the
caller never hold a slot.

### OAuth scopes

| Scope | For |
| --- | --- |
| `project:webhooks.read` | Listing the project's webhooks (refresh, cleanup, unsubscribe) |
| `project:webhooks.write` | Creating, rewriting and deleting the subscription's webhook |
| `project:functions.read`, `project:sites.read`, `project:tables.read`, `project:buckets.read`, `project:users.read` | The authorization read for each event (see above) |

The server already advertises these. A user can narrow them at consent, and
the error message names the scope to grant.

## Ingress

Each subscription is one Appwrite webhook in the subscriber's project. It points
at `{MCP_PUBLIC_URL}/appwrite/webhooks/{subscription id}` and carries the sealed
subscription envelope as its Basic auth password, so Appwrite hands the
subscription back with every delivery and the server keeps no store. The route
is mounted only with the events flag on and the HTTP transport, and it is not
behind the bearer-token gate: the Appwrite signature is its authentication.

For each delivery the ingress:

1. Reads the envelope from Basic auth and takes the sealing key id (`kid`) from
   its JWE header.
2. Verifies `X-Appwrite-Webhook-Signature`. Appwrite signs
   `base64(HMAC-SHA1(url + body))` with the webhook secret, where `url` is the
   URL the webhook was registered with. The ingress rebuilds that URL from
   `MCP_PUBLIC_URL` and the path and ignores the inbound `Host`. The secret is
   derived from the subscription id and a sealing key, and the signature is
   checked against the secret of every key in the ring, not only the one that
   sealed the envelope (see key rotation below). The body is hashed as it
   streams in; up to 2 MiB of it is kept. The route is public and the
   signature covers the whole body, so the read itself is bounded: a request
   without a signature is refused before the body is read, a body past
   16 MiB or one still arriving after 10 seconds is not read on, and a process
   reads at most 32 bodies at once (`READS`; a request waits for a slot within
   the same 10 seconds).
3. Opens the envelope for this server (`MCP_PUBLIC_URL`), the project in
   `X-Appwrite-Webhook-Project-Id` and the subscription id in the path (see
   [Envelope](#envelope)), and checks its lease expiry.
4. Checks that `X-Appwrite-Webhook-Events` contains one of the
   subscription's Appwrite patterns, and projects the body onto the event's
   payload schema. Only the declared fields are forwarded, never the raw body:
   Appwrite sends whole rows, users with email and phone, and executions with
   request headers and logs.
5. Starts the delivery in a background task and answers `200` straight away,
   well inside Appwrite's 15 second timeout. The delivery is signed with
   Standard Webhooks and retried in-process (see `events/delivery.py`).

### Envelope

The envelope is the encrypted profile of
[mcp-event-subscriptions](https://github.com/ChiragAgg5k/mcp-event-subscriptions)
(`mcp-event-subscriptions` on PyPI): a
[Subscription Record v1](https://github.com/ChiragAgg5k/mcp-event-subscriptions/blob/main/SPEC.md)
sealed as JWE Compact with the
[`mcp-subscription+jwe` profile](https://github.com/ChiragAgg5k/mcp-event-subscriptions/blob/main/profiles/encrypted-envelope.md)
(`alg` `dir`, `enc` `A256GCM`, a random 96-bit IV). `events/envelope.py` is the
adapter around the package.

- **Record.** `version` 1, `id` (the subscription id), `principal` (a digest of
  the OAuth `iss`, `sub` and `client_id`, never a token), `event.name` and
  `event.arguments`, `delivery` (`mode` `webhook`, the callback `url` and its one
  `whsec_` `secret`), `lease` (`expiresAt` and `refreshBefore` in UTC RFC 3339
  with `Z`) and empty `extensions`.
- **Context binding.** Every envelope is sealed for a context of `server`
  (`MCP_PUBLIC_URL`), `tenant` (the project id) and `resource` (the
  subscription id, which is also the webhook id). The ingress takes the expected
  context only from its configuration, the routed webhook id and
  `X-Appwrite-Webhook-Project-Id`, never from the envelope, so an envelope
  copied to another webhook, project or deployment does not open. The record
  must also hash back to the subscription id, or it does not open either.
- **Keys.** Each key in `MCP_EVENTS_SEALING_KEYS` is 32 random bytes (keys that
  are short, long or obviously not random stop the server at startup) and is
  the JWE key directly. The webhook signing secret is derived from it with
  HKDF-SHA256 under its own label, so the two never share a key.
- **Size.** The envelope must fit Appwrite's `authPassword`, which
  appwrite/appwrite#14293 has to raise to at least 2048 characters. Sealing
  refuses anything longer (`ENVELOPE_BUDGET`). Measured with 36-character ids,
  key id `k1` and server `https://mcp.appwrite.io`:

  | Case | Envelope length |
  | --- | --- |
  | `tablesdb.row.created`, 59-character URL, 32-byte secret | 1108 |
  | `users.user.created`, 100-character URL, 32-byte secret | 1115 |
  | `tablesdb.row.created`, 150-character URL, 32-byte secret | 1322 |
  | `functions.deployment.completed` + `status`, 300-character URL, 64-byte secret | 1551 |
  | **Worst case:** `tablesdb.row.created`, 300-character URL, 64-byte secret | **1580** |
  | `ENVELOPE_BUDGET` | 2048 |

### Delivery backlog

A delivery to a failing callback stays in flight through its retries, up to
about 13 minutes. To keep memory bounded, a process holds at most **1024**
deliveries in flight, and one subscription at most **64** of them
(`BACKLOG` and `SUBSCRIPTION_BACKLOG` in `events/ingress.py`). A delivery that
arrives while either limit is reached is still answered `200`, so Appwrite never
pauses the subscriber's webhook, but it is dropped and counted as
`mcp.events.ingress{outcome="dropped", reason="backlog_full"}`. Delivery is at
most once anyway; the per-subscription limit keeps one busy subscription with a
dead callback from crowding out everyone else.

Outbound requests share one deadline (10 s) that also covers waiting for one of
the four per-host slots, and each attempt is signed only once it holds a slot,
so a queued request never goes out late or with a stale `webhook-timestamp`.

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
| Subscription expired (checked before the signature) | `200` dropped `expired` | no |
| Envelope sealed with a key no longer in the ring | `200` dropped `retired_key` | no |
| Event name not in the catalog | `200` dropped `unknown_event` | no |
| Appwrite event outside the subscription's patterns | `200` dropped `event_mismatch` | no |
| Body names another resource | `200` dropped `resource_mismatch` | no |
| Body is not the expected model (deployment activation) | `200` dropped `shape` | no |
| Status filter does not match (`waiting`, `ready` for a `failed` filter) | `200` dropped `status` | no |
| Body is not a JSON object | `200` dropped `malformed` | no |
| Body over 2 MiB (signature still checked) | `200` dropped `too_large` | no |
| Body over 16 MiB, or slot wait and body slower than 10 s (signature never checked) | `200` dropped `unread` | no |
| Delivery backlog full (process or subscription) | `200` dropped `backlog_full` | no |
| No Basic auth, or the password is not an envelope | `401` `credentials` | no |
| Signature missing or wrong | `401` `signature` | no |
| Envelope tampered with, not the profile, or for another server, webhook or project | `401` `envelope` | no |

A legitimate webhook cannot get a `401` in normal operation:

- **Key rotation.** Rotate in three deploys, each rolled out to every replica
  before the next:
  1. Add the new key *after* the current one (`k1:…,k2:…`). Every replica can
     now open `k2` envelopes, but `k1` still seals.
  2. Move the new key first (`k2:…,k1:…`). Refreshes now seal with `k2`. A
     replica still on step 1 opens them, so no delivery that reaches it is
     dropped as `retired_key`.
  3. Once 25 hours have passed since step 2 reached every replica, remove the
     old key (`k2:…`).

  Sealing with a key that some replica does not have yet would make that
  replica drop the subscription's deliveries as `retired_key`. Replicas that
  refresh the same subscription during step 2 can interleave their writes and
  leave an envelope from one key next to a signing secret from the other; that
  still verifies, because the signature is checked against every key in the
  ring. Only a replica still on step 1 writes an old-key signing secret, so
  every envelope paired with one was written before step 2 finished, give or
  take one refresh, and expires within the longest TTL (24 hours) after that;
  the extra hour covers refreshes in flight. After the old key is removed, only
  expired subscriptions still name it. Their webhooks are orphans awaiting
  cleanup, so they get `200` (`retired_key`) instead of being paused.
- **Public URL.** The signature covers the registered URL, and every envelope
  is bound to `MCP_PUBLIC_URL`. Changing it breaks every subscription until it
  refreshes, so keep it fixed while subscriptions exist.
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
| `mcp.events.subscriptions` | `operation` (`subscribe`, `unsubscribe`, `cleanup`), `outcome` (`created`, `refreshed`, `removed`, `absent`, `error`), `event`, `reason` (the JSON-RPC code of an error) |

`events/subscribe` and `events/unsubscribe` are also counted in
`mcp.messages.received` and `mcp.jsonrpc.errors`, like `tools/call`. Errors on
our side (`-32603`) go to Sentry with the same tags as `tools/call` plus
`event.name` and `appwrite.project_id`. Errors the caller caused are counted but
not reported.

## Testing

Events are tested end to end in `tests/e2e/`: the real hosted app runs under
uvicorn on a random localhost port, and the tests talk to it over HTTP exactly
like ChatGPT does (`MCP-Protocol-Version: 2026-07-28`, `Mcp-Method`, `_meta`).
Only the OAuth token verifier is stubbed, because Cloud OAuth is not reachable
from CI. The suite needs no credentials and runs on every PR:

```bash
uv run --group e2e python -m unittest discover -s tests/e2e -v
```

The flows also play the parties around the server on localhost. An Appwrite
REST emulator (`support.Cloud`) serves the resource reads and `/v1/webhooks`
with Appwrite's scopes, error types and plan limit, so `events/subscribe` and
`events/unsubscribe` write real webhooks into it. Appwrite's webhooks worker
fires those stored webhooks, or hand-built ones, with recorded-shape bodies
(`tests/e2e/fixtures/appwrite/`) at the real ingress, and deliveries go through
the real dispatcher and egress to an HTTPS receiver that answers the
verification handshake and checks every request with the official
`standardwebhooks` library. Counters are read from the server's real OTLP
export.

A live end-to-end run against Cloud staging (real OAuth tokens, real Appwrite
webhooks) is planned once
[appwrite/appwrite#14293](https://github.com/appwrite/appwrite/issues/14293)
ships; until then the server's surroundings are played locally.
