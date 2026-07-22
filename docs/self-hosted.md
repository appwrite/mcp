# Self-hosted Appwrite

Running your own Appwrite instance? Run the MCP server locally over `stdio` and
authenticate with a project API key instead of OAuth.

## Setup

1. In your Appwrite Console, create a project API key with the scopes you want the
   server to use.
2. Add the server to your client using the config below, replacing the
   placeholders:
   - `<YOUR_PROJECT_ID>` — your Appwrite project ID.
   - `<YOUR_API_KEY>` — the API key you just created.
   - `<YOUR_APPWRITE_DOMAIN>` — your instance domain, e.g. `localhost:9501` for a
     local Docker setup.

Self-hosted runs use `uvx`, so make sure [`uv`](https://docs.astral.sh/uv/) is
installed and on your `PATH`. `stdio` is the default transport for the package
command. The server validates the endpoint, project ID, API key, and at least one
supported service at startup, and fails before accepting tool calls if anything is
wrong.

If your MCP client reports a reconnect failure such as Cursor `-32000`, the stdio
process exited before the handshake — usually bad credentials, a wrong endpoint, or
an API key missing every readable scope the startup probe tries (`tables_db`,
`users`, `teams`, …). Run the same command in a terminal to see the real error:

```bash
APPWRITE_PROJECT_ID=<YOUR_PROJECT_ID> \
APPWRITE_API_KEY=<YOUR_API_KEY> \
APPWRITE_ENDPOINT=http://localhost:9501/v1 \
uvx mcp-server-appwrite
```

For a local Docker Appwrite instance the endpoint is typically
`http://localhost:9501/v1` (HTTP, not HTTPS). The value must be the API base
(`…/v1`), not the Console URL.

## Connect your client

<details open>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add appwrite \
  --env APPWRITE_PROJECT_ID=<YOUR_PROJECT_ID> \
  --env APPWRITE_API_KEY=<YOUR_API_KEY> \
  --env APPWRITE_ENDPOINT=https://<YOUR_APPWRITE_DOMAIN>/v1 \
  -- uvx mcp-server-appwrite
```

</details>

<details>
<summary><b>Claude Desktop</b></summary>

Edit your config via **Settings → Developer → Edit Config**
(`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
`%APPDATA%\Claude\claude_desktop_config.json` on Windows), then fully restart the
app.

```json
{
  "mcpServers": {
    "appwrite": {
      "command": "uvx",
      "args": ["mcp-server-appwrite"],
      "env": {
        "APPWRITE_PROJECT_ID": "<YOUR_PROJECT_ID>",
        "APPWRITE_API_KEY": "<YOUR_API_KEY>",
        "APPWRITE_ENDPOINT": "https://<YOUR_APPWRITE_DOMAIN>/v1"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Cursor</b></summary>

Edit `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project).

```json
{
  "mcpServers": {
    "appwrite": {
      "command": "uvx",
      "args": ["mcp-server-appwrite"],
      "env": {
        "APPWRITE_PROJECT_ID": "<YOUR_PROJECT_ID>",
        "APPWRITE_API_KEY": "<YOUR_API_KEY>",
        "APPWRITE_ENDPOINT": "https://<YOUR_APPWRITE_DOMAIN>/v1"
      }
    }
  }
}
```

</details>

<details>
<summary><b>VS Code</b> (GitHub Copilot)</summary>

Edit `.vscode/mcp.json` (workspace) or your user configuration via the Command
Palette → **MCP: Open User Configuration**.

```json
{
  "servers": {
    "appwrite": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-server-appwrite"],
      "env": {
        "APPWRITE_PROJECT_ID": "<YOUR_PROJECT_ID>",
        "APPWRITE_API_KEY": "<YOUR_API_KEY>",
        "APPWRITE_ENDPOINT": "https://<YOUR_APPWRITE_DOMAIN>/v1"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Codex</b></summary>

Edit `~/.codex/config.toml`.

```toml
[mcp_servers.appwrite]
command = "uvx"
args = ["mcp-server-appwrite"]

[mcp_servers.appwrite.env]
APPWRITE_PROJECT_ID = "<YOUR_PROJECT_ID>"
APPWRITE_API_KEY = "<YOUR_API_KEY>"
APPWRITE_ENDPOINT = "https://<YOUR_APPWRITE_DOMAIN>/v1"
```

</details>

<details>
<summary><b>OpenCode</b></summary>

Edit `opencode.json` (project) or `~/.config/opencode/opencode.json` (global).

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "appwrite": {
      "type": "local",
      "command": ["uvx", "mcp-server-appwrite"],
      "enabled": true,
      "environment": {
        "APPWRITE_PROJECT_ID": "<YOUR_PROJECT_ID>",
        "APPWRITE_API_KEY": "<YOUR_API_KEY>",
        "APPWRITE_ENDPOINT": "https://<YOUR_APPWRITE_DOMAIN>/v1"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Windsurf</b></summary>

Edit `~/.codeium/windsurf/mcp_config.json`.

```json
{
  "mcpServers": {
    "appwrite": {
      "command": "uvx",
      "args": ["mcp-server-appwrite"],
      "env": {
        "APPWRITE_PROJECT_ID": "<YOUR_PROJECT_ID>",
        "APPWRITE_API_KEY": "<YOUR_API_KEY>",
        "APPWRITE_ENDPOINT": "https://<YOUR_APPWRITE_DOMAIN>/v1"
      }
    }
  }
}
```

</details>
