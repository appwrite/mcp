# Appwrite MCP server

mcp-name: io.github.appwrite/mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server for Appwrite.
It exposes Appwrite's API — databases, users, functions, teams, storage, and more
— as tools your MCP client can call.

Connect to the hosted server at **`https://mcp.appwrite.io/mcp`** and authenticate
through your browser. The first time you connect, your client opens an Appwrite
consent screen; approve the scopes and you're connected. There are no keys to
copy.

## Connect your client

Pick your client below. Each adds the hosted Appwrite Cloud server.

<details open>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add --transport http appwrite https://mcp.appwrite.io/mcp
```

</details>

<details>
<summary><b>Claude Desktop</b></summary>

Go to **Settings → Connectors → Add custom connector** and paste
`https://mcp.appwrite.io/mcp`.

On the free plan, bridge the remote server through stdio instead (requires
Node.js) by editing your config via **Settings → Developer → Edit Config**:

```json
{
  "mcpServers": {
    "appwrite": {
      "command": "npx",
      "args": ["mcp-remote", "https://mcp.appwrite.io/mcp"]
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
      "url": "https://mcp.appwrite.io/mcp"
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
      "type": "http",
      "url": "https://mcp.appwrite.io/mcp"
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
url = "https://mcp.appwrite.io/mcp"
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
      "type": "remote",
      "url": "https://mcp.appwrite.io/mcp",
      "enabled": true
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
      "serverUrl": "https://mcp.appwrite.io/mcp"
    }
  }
}
```

</details>

## Self-hosted Appwrite

Running your own Appwrite instance? Run the MCP server locally over `stdio` and
authenticate with a project API key. See [docs/self-hosted.md](docs/self-hosted.md)
for per-client setup.

## Documentation

- [Tool surface](docs/tool-surface.md) — the tools exposed to the model and the
  internal Appwrite catalog.
- [How Cloud authentication works](docs/authentication.md) — the OAuth 2.1 flow.
- [Documentation search](docs/documentation-search.md) — the in-process
  `appwrite_search_docs` tool and how to rebuild its index.
- [Self-hosted Appwrite](docs/self-hosted.md) — run the server locally with a
  project API key.
- [Local development](docs/development.md) — running, testing, and debugging the
  server locally.
- [AGENTS.md](AGENTS.md) — full contributor guide and pre-PR checklist.

## License

This MCP server is licensed under the MIT License. See the [LICENSE](LICENSE) file
for details.
