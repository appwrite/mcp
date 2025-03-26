# Appwrite MCP server

[![smithery badge](https://smithery.ai/badge/@appwrite/mcp)](https://smithery.ai/server/@appwrite/mcp)

<!-- Cover image will go here once available -->

<a href="https://glama.ai/mcp/servers/xkj2px7r0v">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/xkj2px7r0v/badge" alt="Appwrite MCP server" />
</a>

## Overview

A Model Context Protocol server for interacting with Appwrite's API. This server provides tools to manage databases, users, functions, teams, and more within your Appwrite project.

## Quick Links
- [Configuration](#configuration)
- [Installation](#installation)
- IDE Integration:
  - [Claude Desktop](#usage-with-claude-desktop)
  - [Cursor](#usage-with-cursor)
  - [Windsurf Editor](#usage-with-windsurf-editor)
- [Local Development](#local-development)
- [Debugging](#debugging)

## Configuration

> Before launching the MCP server, you must setup an [Appwrite project](https://cloud.appwrite.io/) and create an API key with the necessary scopes enabled.

Create a `.env` file in your working directory and add the following:

```env
APPWRITE_PROJECT_ID=your-project-id
APPWRITE_API_KEY=your-api-key
APPWRITE_ENDPOINT=https://cloud.appwrite.io/v1
```

Then, open your terminal and run the following command

### Linux and MacOS

```sh
source .env
```

### Windows

#### Command Prompt

```cmd
for /f "tokens=1,2 delims==" %A in (.env) do set %A=%B
```

#### PowerShell

```powershell
Get-Content .\.env | ForEach-Object {
  if ($_ -match '^(.*?)=(.*)$') {
    [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2], "Process")
  }
}
```

## Installation

### Installing via Smithery

To install Appwrite MCP Server for Claude Desktop automatically via [Smithery](https://smithery.ai/server/@appwrite/mcp):

```bash
npx -y @smithery/cli install @appwrite/mcp --client claude
```

### Using uv (recommended)
When using [`uv`](https://docs.astral.sh/uv/) no specific installation is needed. We will
use [`uvx`](https://docs.astral.sh/uv/guides/tools/) to directly run *mcp-server-appwrite*.

```bash
uvx mcp-server-appwrite [args]
```

### Using pip

```bash
pip install mcp-server-appwrite
```
Then run the server using 

```bash
python -m mcp_server_appwrite [args]
```

### Command-line arguments

Both the `uv` and `pip` setup processes require certain arguments to enable MCP tools for various Appwrite APIs.

> When an MCP tool is enabled, the tool's definition is passed to the LLM, using up tokens from the model's available context window. As a result, the effective context window is reduced.  
>  
> The default Appwrite MCP server ships with only the Databases tools (our most commonly used API) enabled to stay within these limits. Additional tools can be enabled by using the flags below.

| Argument | Description |
| --- | --- |
| `--databases` | Enables the Databases API |
| `--users` | Enables the Users API |
| `--teams` | Enables the Teams API |
| `--storage` | Enables the Storage API |
| `--functions` | Enables the Functions API |
| `--messaging` | Enables the Messaging API |
| `--locale` | Enables the Locale API |
| `--avatars` | Enables the Avatars API |
| `--all` | Enables all Appwrite APIs |

## Usage with Claude Desktop

In the Claude Desktop app, open the app's **Settings** page (press `CTRL + ,` on Windows or `CMD + ,` on MacOS) and head to the **Developer** tab. Clicking on the **Edit Config** button will take you to the `claude_desktop_config.json` file, where you must add the following:

```json
{
  "mcpServers": {
    "appwrite": {
      "command": "uvx",
      "args": [
        "mcp-server-appwrite"
      ],
      "env": {
        "APPWRITE_PROJECT_ID": "your-project-id",
        "APPWRITE_API_KEY": "your-api-key",
        "APPWRITE_ENDPOINT": "https://cloud.appwrite.io/v1" // Optional
      }
    }
  }
}

```

> Note: In case you see a `uvx ENOENT` error, ensure that you either add `uvx` to the `PATH` environment variable on your system or use the full path to your `uvx` installation in the config file.

Upon successful configuration, you should be able to see the server in the list of available servers in Claude Desktop.

![Claude Desktop Config](images/claude-desktop-integration.png)

## Usage with [Cursor](https://www.cursor.com/)

Head to Cursor `Settings > MCP` and click on **Add new MCP server**. Choose the type as `Command` and add the command below to the **Command** field.

- **MacOS**

```bash
env APPWRITE_API_KEY=your-api-key env APPWRITE_PROJECT_ID=your-project-id uvx mcp-server-appwrite
```

- **Windows**

```cmd
cmd /c SET APPWRITE_PROJECT_ID=your-project-id && SET APPWRITE_API_KEY=your-api-key && uvx mcp-server-appwrite
```

![Cursor Settings](./images/cursor-integration.png)

## Usage with [Windsurf Editor](https://codeium.com/windsurf)

Head to Windsurf `Settings > Cascade > Model Context Protocol (MCP) Servers` and click on **View raw config**. Update the `mcp_config.json` file to include the following:

```json
{
  "mcpServers": {
    "appwrite": {
      "command": "uvx",
      "args": [
        "mcp-server-appwrite"
      ],
      "env": {
        "APPWRITE_PROJECT_ID": "your-project-id",
        "APPWRITE_API_KEY": "your-api-key",
        "APPWRITE_ENDPOINT": "https://cloud.appwrite.io/v1" // Optional
      }
    }
  }
}
```

![Windsurf Settings](./images/windsurf-integration.png)

## Local Development

### Clone the repository

```bash
git clone https://github.com/appwrite/mcp.git
```

### Install `uv`

- Linux or MacOS

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

- Windows (PowerShell)

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### Prepare virtual environment

First, create a virtual environment.

```bash
uv venv
```

Next, activate the virtual environment.

- Linux or MacOS

```bash
source .venv/bin/activate
```

- Windows

```powershell
.venv\Scripts\activate
```

### Run the server

```bash
uv run -v --directory ./ mcp-server-appwrite
```

## Debugging

You can use the MCP inspector to debug the server. 

```bash
npx @modelcontextprotocol/inspector \
  uv \
  --directory . \
  run mcp-server-appwrite
```

Make sure your `.env` file is properly configured before running the inspector. You can then access the inspector at `http://localhost:5173`.

## License

This MCP server is licensed under the MIT License. This means you are free to use, modify, and distribute the software, subject to the terms and conditions of the MIT License. For more details, please see the LICENSE file in the project repository.
