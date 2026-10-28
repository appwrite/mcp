"""The tools behind the Appwrite view (``ui://appwrite/view``).

``appwrite_show`` is the one tool the model sees. It reads what a view shows
(a page of a table's rows, a Function or Site deployment with its build logs,
a Function's executions, a Storage bucket's files, a project's Auth users,
or a project's usage) through the hidden catalog, and returns it twice: a
text summary in ``content`` for the model and for hosts without MCP Apps,
and the data in ``structuredContent`` for the view. The view calls it again to page,
refresh and follow a build, so every read it makes is one the model could
make too.

``appwrite_view_write`` is visible to the view only (``visibility: ["app"]``).
It runs a short allowlist of catalog writes the view offers buttons for, and
like ``appwrite_call_write_tool`` it refuses to run without
``confirm_write=true``, which the view sends only after the user confirms the
change in a dialog. Anything the view renders comes from Appwrite, so a
script injected through that data could call this tool; the allowlist bounds
what such a script could do.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from enum import StrEnum
from typing import Any

import mcp.types as types
from appwrite_console.query import Query

from ..constants import SHOW_TOOL_NAME, VIEW_WRITE_TOOL_NAME
from . import executions, files, resource, usage, users

Run = Callable[[dict[str, Any], bool], list[Any]]
"""Runs a hidden catalog tool from raw call arguments (``tool_name``,
``arguments``, ``project_id``, ``confirm_write``), as a write or a read."""

Handler = Callable[[dict[str, Any]], types.CallToolResult]

PAGE_SIZE = 25
"""Rows per page: Appwrite's default list limit."""

LOG_LINES = 200
"""Build log lines a view carries. Hosts may put ``structuredContent`` in the
model's context, and a build log can run to megabytes."""

SUMMARY_LOG_LINES = 20
SUMMARY_ROWS = 10
SUMMARY_COLUMNS = 4
SUMMARY_CELL = 24

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
"""Terminal escape sequences (colors, cursor moves) in build logs: the view
renders the colors, the model's summary drops them."""

TERMINAL_STATUSES = frozenset({"ready", "failed", "canceled"})

WRITES = (
    "tables_db_update_row",
    "functions_update_deployment_status",
    "sites_update_deployment_status",
    *users.WRITES,
)
"""The writes the view offers: editing a row, cancelling a build, and
blocking, verifying or labelling a user."""

_SHOW_TITLE = "Show Appwrite view"
_WRITE_TITLE = "Run Appwrite view action"


class View(StrEnum):
    TABLE = "table"
    DEPLOYMENT = "deployment"
    EXECUTIONS = "executions"
    FILES = "files"
    USERS = "users"
    USAGE = "usage"


class Views:
    def __init__(self, run: Run):
        self._run = run

    def tools(self) -> list[tuple[types.Tool, Handler]]:
        return [(_show_tool(), self.show), (_write_tool(), self.write)]

    def show(self, arguments: dict[str, Any]) -> types.CallToolResult:
        view = arguments.get("view")
        if view == View.TABLE:
            data = self._table(arguments)
            text = _table_summary(data)
        elif view == View.DEPLOYMENT:
            data = self._deployment(arguments)
            text = _deployment_summary(data)
        elif view == View.EXECUTIONS:
            data = executions.read(self._read, arguments)
            text = executions.summary(data)
        elif view == View.FILES:
            data = files.read(self._read, self._preview, arguments)
            text = files.summary(data)
        elif view == View.USERS:
            data = users.read(self._read, arguments)
            text = users.summary(data)
        elif view == View.USAGE:
            data = usage.read(self._read, arguments)
            text = usage.summary(data)
        else:
            raise ValueError(f"view must be one of: {', '.join(View)}. Got {view!r}.")
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)],
            structured_content=data,
        )

    def write(self, arguments: dict[str, Any]) -> types.CallToolResult:
        tool_name = arguments.get("tool_name")
        if tool_name not in WRITES:
            raise ValueError(
                f"{VIEW_WRITE_TOOL_NAME} runs only {', '.join(WRITES)}. "
                f"Got {tool_name!r}."
            )
        # Only the literal true confirms: a string such as "false" is truthy.
        if arguments.get("confirm_write") is not True:
            raise ValueError(
                f"{VIEW_WRITE_TOOL_NAME} runs {tool_name} only with confirm_write=true."
            )
        result = _json(self._run(arguments, True))
        if isinstance(result, dict) and tool_name == "tables_db_update_row":
            result = _row(result)
        elif isinstance(result, dict) and tool_name in users.WRITES:
            result = users.user(result)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"{tool_name} succeeded.")],
            structured_content=result if isinstance(result, dict) else None,
        )

    def _read(
        self, tool_name: str, project_id: Any, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        result = _json(
            self._run(
                {
                    "tool_name": tool_name,
                    "project_id": project_id,
                    "arguments": arguments,
                },
                False,
            )
        )
        if not isinstance(result, dict):
            raise ValueError(f"{tool_name} returned an unexpected response.")
        return result

    def _preview(self, project_id: Any, arguments: dict[str, Any]) -> str | None:
        """A file preview as a data URI. A file Appwrite cannot transform
        shows without one rather than failing the page."""
        try:
            content = self._run(
                {
                    "tool_name": "storage_get_file_preview",
                    "project_id": project_id,
                    "arguments": arguments,
                },
                False,
            )
        except Exception:
            return None
        for item in content:
            if isinstance(item, types.ImageContent):
                return f"data:{item.mime_type};base64,{item.data}"
        return None

    def _table(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = arguments.get("project_id")
        database_id = _required(arguments, "database_id")
        table_id = _required(arguments, "table_id")
        cursor = arguments.get("cursor")
        target = {"database_id": database_id, "table_id": table_id}

        table = self._read("tables_db_get_table", project_id, target)
        # One row past the page tells whether another page follows.
        queries = [Query.order_desc("$createdAt"), Query.limit(PAGE_SIZE + 1)]
        if cursor:
            queries.append(Query.cursor_after(str(cursor)))
        page = self._read(
            "tables_db_list_rows", project_id, {**target, "queries": queries}
        )
        rows = [_row(each) for each in page.get("rows") or []]
        more = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        columns = [
            {"key": column.get("key"), "type": column.get("type")}
            for column in table.get("columns") or []
            if column.get("status", "available") == "available"
        ]
        return {
            "view": View.TABLE.value,
            "projectId": project_id,
            "databaseId": database_id,
            "tableId": table_id,
            "name": table.get("name") or table_id,
            "columns": columns,
            "rows": rows,
            "total": int(page.get("total", len(rows))),
            "cursor": cursor,
            "next": rows[-1].get("$id") if more else None,
        }

    def _deployment(self, arguments: dict[str, Any]) -> dict[str, Any]:
        project_id = arguments.get("project_id")
        function_id = arguments.get("function_id")
        site_id = arguments.get("site_id")
        if bool(function_id) == bool(site_id):
            raise ValueError("Pass exactly one of function_id or site_id.")
        kind, resource_key, resource_id = (
            ("function", "function_id", function_id)
            if function_id
            else ("site", "site_id", site_id)
        )
        service = "functions" if function_id else "sites"
        target = {resource_key: resource_id}

        deployment_id = arguments.get("deployment_id")
        if deployment_id:
            deployment = self._read(
                f"{service}_get_deployment",
                project_id,
                {**target, "deployment_id": deployment_id},
            )
        else:
            latest = self._read(
                f"{service}_list_deployments",
                project_id,
                {
                    **target,
                    "queries": [Query.order_desc("$createdAt"), Query.limit(1)],
                },
            )
            deployments = latest.get("deployments") or []
            if not deployments:
                raise ValueError(f"The {kind} {resource_id} has no deployments.")
            deployment = deployments[0]

        logs = (deployment.get("buildLogs") or "").splitlines()
        status = deployment.get("status") or "unknown"
        return {
            "view": View.DEPLOYMENT.value,
            "projectId": project_id,
            "kind": kind,
            "resourceId": resource_id,
            "deployment": {
                "id": deployment.get("$id"),
                "status": status,
                "createdAt": deployment.get("$createdAt"),
                "updatedAt": deployment.get("$updatedAt"),
                "buildDuration": deployment.get("buildDuration"),
                "sourceSize": deployment.get("sourceSize"),
                "buildSize": deployment.get("buildSize"),
                "activate": deployment.get("activate"),
                "branch": deployment.get("providerBranch") or None,
                "commit": deployment.get("providerCommitHash") or None,
                "commitMessage": deployment.get("providerCommitMessage") or None,
                "logs": logs[-LOG_LINES:],
                "truncated": len(logs) > LOG_LINES,
            },
            "done": status in TERMINAL_STATUSES,
        }


def _show_tool() -> types.Tool:
    return types.Tool(
        name=SHOW_TOOL_NAME,
        title=_SHOW_TITLE,
        annotations=_annotations(_SHOW_TITLE, read_only=True),
        description=(
            "Show an interactive Appwrite view in the chat: view=table pages "
            "through a table's rows and lets the user edit one; view=deployment "
            "follows a Function or Site deployment's status and build logs live; "
            "view=executions lists a Function's executions and opens one with "
            "its request, response, logs and errors. "
            "view=files browses a Storage bucket's files with image previews. "
            "view=users lists the project's Auth users (search by name, email, "
            "phone or ID) and opens one with their sessions, where the user can "
            "block, verify or label them. "
            "view=usage charts a project's requests, bandwidth and executions "
            "over a range, with its users, resources and storage. "
            "Use it when the user wants to see or browse this data rather than "
            "reason over it; the result also carries a short text summary. Calls "
            "the Appwrite REST API."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": [view.value for view in View]},
                "project_id": {
                    "type": "string",
                    "description": "Appwrite project ID the data lives in.",
                },
                "database_id": {
                    "type": "string",
                    "description": "view=table: the TablesDB database ID.",
                },
                "table_id": {
                    "type": "string",
                    "description": "view=table: the table ID.",
                },
                "cursor": {
                    "type": "string",
                    "description": (
                        "view=table, view=executions, view=files or view=users: "
                        "continue after this ID (the previous page's `next`). "
                        "Omit for the newest."
                    ),
                },
                "function_id": {
                    "type": "string",
                    "description": (
                        "view=deployment: the Function ID (or site_id). "
                        "view=executions: the Function ID."
                    ),
                },
                "site_id": {
                    "type": "string",
                    "description": "view=deployment: the Site ID (or function_id).",
                },
                "deployment_id": {
                    "type": "string",
                    "description": "view=deployment: omit for the latest deployment.",
                },
                "execution_id": {
                    "type": "string",
                    "description": (
                        "view=executions: also open this execution with its "
                        "request, response, logs and errors."
                    ),
                },
                "status": {
                    "type": "string",
                    "enum": list(executions.STATUSES),
                    "description": "view=executions: only executions in this status.",
                },
                "bucket_id": {
                    "type": "string",
                    "description": "view=files: the Storage bucket ID.",
                },
                "search": {
                    "type": "string",
                    "description": (
                        "view=files: only files whose name matches. view=users: "
                        "only users whose name, email, phone or ID matches."
                    ),
                },
                "file_id": {
                    "type": "string",
                    "description": "view=files: also open this file.",
                },
                "thumbnails": {
                    "type": "boolean",
                    "description": (
                        "view=files: include image previews. The view asks for "
                        "these itself; leave unset."
                    ),
                },
                "user_id": {
                    "type": "string",
                    "description": "view=users: also open this user with their sessions.",
                },
                "range": {
                    "type": "string",
                    "enum": list(usage.RANGES),
                    "description": "view=usage: how far back to chart. Defaults to 7d.",
                },
            },
            "required": ["view"],
            "additionalProperties": False,
        },
        _meta={
            "ui": {"resourceUri": resource.uri(), "visibility": ["model", "app"]},
            # The flat key hosts read before ``ui.resourceUri``; SEP-1865 drops
            # it at GA.
            "ui/resourceUri": resource.uri(),
        },
    )


def _write_tool() -> types.Tool:
    return types.Tool(
        name=VIEW_WRITE_TOOL_NAME,
        title=_WRITE_TITLE,
        annotations=_annotations(_WRITE_TITLE, read_only=False),
        description=(
            "Used by the Appwrite view after the user confirms a change in it. "
            "Runs one of a fixed set of Appwrite writes; requires "
            "confirm_write=true. Calls the Appwrite REST API."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "tool_name": {"type": "string", "enum": list(WRITES)},
                "arguments": {"type": "object"},
                "project_id": {"type": "string"},
                "confirm_write": {"type": "boolean"},
            },
            "required": ["tool_name", "arguments", "confirm_write"],
            "additionalProperties": False,
        },
        _meta={"ui": {"resourceUri": resource.uri(), "visibility": ["app"]}},
    )


def _annotations(title: str, *, read_only: bool) -> types.ToolAnnotations:
    return types.ToolAnnotations(
        title=title,
        read_only_hint=read_only,
        destructive_hint=not read_only,
        idempotent_hint=read_only,
        open_world_hint=True,
    )


def _required(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required for view={arguments.get('view')}.")
    return value


def _row(row: dict[str, Any]) -> dict[str, Any]:
    """A row with its column values beside the system fields. The SDK's ``Row``
    model moves column values into ``data``."""
    values = row.get("data")
    if not isinstance(values, dict):
        return row
    return {key: value for key, value in row.items() if key != "data"} | values


def _json(content: list[Any]) -> Any:
    text = "\n".join(
        item.text for item in content if isinstance(item, types.TextContent)
    )
    try:
        return json.loads(text)
    except ValueError as error:
        raise ValueError("Appwrite returned a response that is not JSON.") from error


def _cell(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = text.replace("\n", " ")
    if len(text) > SUMMARY_CELL:
        return text[: SUMMARY_CELL - 1] + "…"
    return text


def _table_summary(data: dict[str, Any]) -> str:
    rows: list[dict[str, Any]] = data["rows"]
    keys = ["$id"] + [
        column["key"] for column in data["columns"][: SUMMARY_COLUMNS - 1]
    ]
    lines = [
        f"Table {data['name']} ({data['databaseId']}/{data['tableId']}): "
        f"{data['total']} rows, showing {len(rows)} newest"
        + (" after the cursor." if data["cursor"] else ".")
    ]
    if rows:
        lines.append(" | ".join(keys))
        for row in rows[:SUMMARY_ROWS]:
            lines.append(" | ".join(_cell(row.get(key, "")) for key in keys))
        if len(rows) > SUMMARY_ROWS:
            lines.append(f"… {len(rows) - SUMMARY_ROWS} more rows shown in the view.")
    if data["next"]:
        lines.append(
            f"Next page: {SHOW_TOOL_NAME} with view=table and cursor={data['next']}."
        )
    return "\n".join(lines)


def _deployment_summary(data: dict[str, Any]) -> str:
    deployment = data["deployment"]
    lines = [
        f"Deployment {deployment['id']} of {data['kind']} {data['resourceId']} "
        f"is {deployment['status']}."
    ]
    if deployment["commit"]:
        lines.append(
            f"Source: {deployment['branch'] or 'unknown branch'} @ "
            f"{deployment['commit'][:7]}."
        )
    logs = deployment["logs"][-SUMMARY_LOG_LINES:]
    if logs:
        lines.append("Last build log lines:")
        lines.extend(ANSI.sub("", line) for line in logs)
    return "\n".join(lines)
