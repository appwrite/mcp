# Testing flags

Testing flags are opt-in behavior overrides for testers — they are off by
default and are not part of the supported configuration surface. All flags are
declared in one place, [`src/mcp_server_appwrite/flags.py`](../src/mcp_server_appwrite/flags.py),
and every flag can be enabled two equivalent ways:

```bash
# CLI argument …
uv run mcp-server-appwrite --transport http --<name> <value>

# … or environment variable (also works with Docker/Compose and .env)
<ENV_VAR>=<value> uv run mcp-server-appwrite --transport http
```

The CLI argument simply writes through to the environment variable, which is
the single runtime source of truth.

## Adding a flag

1. Declare a `Flag(name=..., env=..., help=...)` in `flags.py` and add it to
   `FLAGS`. That's all it takes to get the `--<name>` CLI argument and
   `$<ENV>` variable.
2. Read it where needed with `flags.value(flags.MY_FLAG)` — read at request
   time (not import time) so tests can toggle it via `os.environ`.
3. Add unit tests for the behavior the flag changes.
4. Document it below: what it does, how to enable it, and how to verify it.

Flags are for testing overrides only — permanent configuration belongs in
`constants.py` or a plain environment variable.

## Available flags

| CLI | Env | What it does |
| --- | --- | --- |
| `--console-url` | `MCP_CONSOLE_URL` | Send OAuth login/consent to an alternative Appwrite Console (HTTP transport only). |

### `--console-url` — test OAuth against a pre-release console

By default the Appwrite authorization server redirects users to the production
console (`cloud.appwrite.io/console`) for login and consent. This flag lets
testers run the whole OAuth flow through a different console deployment, for
example the new console at `new.appwrite.io`.

How it works: the MCP server advertises itself as the OAuth authorization
server and mirrors the real server's discovery document with one change — the
`authorization_endpoint` points at a local `/oauth2/authorize` proxy. The proxy
forwards each authorize request to the real Appwrite authorize endpoint
(keeping all upstream validation) and rewrites its consent redirect from the
default console to `<console-url>/oauth2/consent`. Token, registration, and
JWKS endpoints stay on the real authorization server, so token validation is
unchanged.

**Enable it:**

```bash
MCP_PUBLIC_URL=http://localhost:8000 \
  uv run mcp-server-appwrite --transport http --console-url https://new.appwrite.io
```

**Quick checks (no browser):**

```bash
# Protected resource metadata names this MCP server as the authorization server
curl -s http://localhost:8000/.well-known/oauth-protected-resource | jq .authorization_servers

# Mirrored discovery points the authorize step at the local proxy
curl -s http://localhost:8000/.well-known/oauth-authorization-server | jq .authorization_endpoint

# The authorize proxy rewrites the consent redirect to the override console
curl -sI "http://localhost:8000/oauth2/authorize?client_id=<id>&response_type=code&redirect_uri=<uri>&scope=openid" | grep -i location
```

**Full flow with an MCP client:**

```bash
claude mcp add --transport http appwrite-test http://localhost:8000/
```

Then run `/mcp` in Claude Code and authenticate — the browser should open the
override console's sign-in page, and after consent the client completes the
token exchange against the real authorization server.
