"""The public Appwrite docs MCP server, served at ``/docs/mcp``.

It has one tool, ``appwrite_search_docs``, and needs no sign-in, so agents
can answer questions from the documentation before they connect a project.
Calls cost the same single query embedding as ``POST /docs/search``.
"""

from __future__ import annotations

import anyio.to_thread
import mcp.types as types
from mcp import MCPError
from mcp.server import Server, ServerRequestContext
from mcp.types import INVALID_PARAMS

from .constants import (
    DOCS_MAX_QUERY_LENGTH,
    SERVER_ICON_URL,
    SERVER_VERSION,
    SERVER_WEBSITE_URL,
)
from .docs_search import DocsSearch

INSTRUCTIONS = (
    "Search the Appwrite documentation with appwrite_search_docs: it returns the "
    "most relevant pages with their full Markdown content. It is read-only and "
    "needs no sign-in. To create or change resources in an Appwrite project, "
    "connect the Appwrite MCP server at https://mcp.appwrite.io instead."
)


def build_docs_server(docs_search: DocsSearch) -> Server:
    tool = docs_search.get_tool()

    async def handle_list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=[tool])

    async def handle_call_tool(
        ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        if params.name != tool.name:
            raise MCPError(
                INVALID_PARAMS, f"Unknown tool: {params.name}. Use {tool.name}."
            )
        query = (params.arguments or {}).get("query")
        if not isinstance(query, str):
            return _error("query must be a string.")
        if len(query) > DOCS_MAX_QUERY_LENGTH:
            return _error(
                f"query must be at most {DOCS_MAX_QUERY_LENGTH} characters long."
            )
        try:
            content = await anyio.to_thread.run_sync(
                docs_search.search, params.arguments, abandon_on_cancel=True
            )
        except (ValueError, TypeError, RuntimeError) as error:
            return _error(str(error))
        return types.CallToolResult(content=list(content))

    return Server(
        "Appwrite Docs",
        version=SERVER_VERSION,
        instructions=INSTRUCTIONS,
        website_url=SERVER_WEBSITE_URL,
        icons=[types.Icon(src=SERVER_ICON_URL, mime_type="image/svg+xml")],
        on_list_tools=handle_list_tools,
        on_call_tool=handle_call_tool,
    )


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )
