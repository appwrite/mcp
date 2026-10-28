"""``view=executions``: a Function's executions, and one execution in full.

The list is a page of executions, newest first, optionally narrowed to one
status. With ``execution_id`` the payload also carries that execution's
request, response headers, logs and errors (Appwrite keeps a response body
only for the synchronous call that ran it), so "why did the last run fail?" opens on
the answer. The view polls while an execution on the page or the opened one
is still waiting or running.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from appwrite_console.query import Query

from ..constants import SHOW_TOOL_NAME

Read = Callable[[str, Any, dict[str, Any]], dict[str, Any]]
"""Runs a hidden read tool: ``(tool name, project ID, arguments)``."""

PAGE_SIZE = 25

STATUSES = ("waiting", "processing", "completed", "failed", "scheduled")
RUNNING = frozenset({"waiting", "processing"})

LOG_LINES = 200
"""Lines of logs and of errors an execution carries."""

BODY_LIMIT = 8192
"""Characters of the response body an execution carries."""

SUMMARY_ROWS = 10
SUMMARY_LOG_LINES = 20


def read(read: Read, arguments: dict[str, Any]) -> dict[str, Any]:
    project_id = arguments.get("project_id")
    function_id = arguments.get("function_id")
    if not isinstance(function_id, str) or not function_id:
        raise ValueError("function_id is required for view=executions.")
    status = arguments.get("status") or None
    if status is not None and status not in STATUSES:
        raise ValueError(f"status must be one of: {', '.join(STATUSES)}.")
    cursor = arguments.get("cursor") or None
    execution_id = arguments.get("execution_id") or None
    target = {"function_id": function_id}

    function = read("functions_get", project_id, target)
    # One past the page tells whether another page follows.
    queries = [Query.order_desc("$createdAt"), Query.limit(PAGE_SIZE + 1)]
    if status:
        queries.append(Query.equal("status", status))
    if cursor:
        queries.append(Query.cursor_after(str(cursor)))
    page = read("functions_list_executions", project_id, {**target, "queries": queries})
    executions = page.get("executions") or []
    more = len(executions) > PAGE_SIZE
    executions = executions[:PAGE_SIZE]

    execution = None
    if execution_id:
        execution = _detail(
            read(
                "functions_get_execution",
                project_id,
                {**target, "execution_id": execution_id},
            )
        )
    rows = [_summary(each) for each in executions]
    return {
        "view": "executions",
        "projectId": project_id,
        "functionId": function_id,
        "name": function.get("name") or function_id,
        "runtime": function.get("runtime"),
        "status": status,
        "executions": rows,
        "total": int(page.get("total", len(rows))),
        "cursor": cursor,
        "next": rows[-1]["id"] if more else None,
        "execution": execution,
        "running": any(each["status"] in RUNNING for each in rows)
        or bool(execution and execution["status"] in RUNNING),
    }


def _summary(execution: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": execution.get("$id"),
        "status": execution.get("status") or "unknown",
        "trigger": execution.get("trigger"),
        "method": execution.get("requestMethod") or None,
        "path": execution.get("requestPath") or None,
        "statusCode": int(execution.get("responseStatusCode") or 0),
        "duration": execution.get("duration"),
        "createdAt": execution.get("$createdAt"),
        "scheduledAt": execution.get("scheduledAt") or None,
    }


def _detail(execution: dict[str, Any]) -> dict[str, Any]:
    body = execution.get("responseBody") or ""
    logs = (execution.get("logs") or "").splitlines()
    errors = (execution.get("errors") or "").splitlines()
    return {
        **_summary(execution),
        "deploymentId": execution.get("deploymentId") or None,
        "requestHeaders": _headers(execution.get("requestHeaders")),
        "responseHeaders": _headers(execution.get("responseHeaders")),
        "responseBody": body[:BODY_LIMIT],
        "bodyTruncated": len(body) > BODY_LIMIT,
        "logs": logs[-LOG_LINES:],
        "logsTruncated": len(logs) > LOG_LINES,
        "errors": errors[-LOG_LINES:],
        "errorsTruncated": len(errors) > LOG_LINES,
    }


def _headers(headers: Any) -> list[dict[str, str]]:
    return [
        {"name": str(each.get("name", "")), "value": str(each.get("value", ""))}
        for each in headers or []
        if isinstance(each, dict)
    ]


def _request(execution: dict[str, Any]) -> str:
    if not execution["method"]:
        return execution["trigger"] or ""
    return f"{execution['method']} {execution['path'] or '/'}"


def summary(data: dict[str, Any]) -> str:
    rows: list[dict[str, Any]] = data["executions"]
    scope = f" {data['status']}" if data["status"] else ""
    lines = [
        f"Function {data['name']} ({data['functionId']}): {data['total']}"
        f"{scope} executions, showing {len(rows)} newest"
        + (" after the cursor." if data["cursor"] else ".")
    ]
    if rows:
        lines.append("id | status | trigger | request | code | duration | created")
        for each in rows[:SUMMARY_ROWS]:
            lines.append(
                " | ".join(
                    [
                        each["id"],
                        each["status"],
                        each["trigger"] or "",
                        _request(each),
                        str(each["statusCode"] or ""),
                        _duration(each["duration"]),
                        each["createdAt"] or "",
                    ]
                )
            )
        if len(rows) > SUMMARY_ROWS:
            lines.append(f"… {len(rows) - SUMMARY_ROWS} more shown in the view.")
    if data["next"]:
        lines.append(
            f"Next page: {SHOW_TOOL_NAME} with view=executions and "
            f"cursor={data['next']}."
        )
    execution = data["execution"]
    if execution:
        lines.append(
            f"Execution {execution['id']}: {execution['status']}, "
            f"{_request(execution)} → {execution['statusCode'] or 'no response'} "
            f"in {_duration(execution['duration'])}."
        )
        for label, key in (("Errors", "errors"), ("Logs", "logs")):
            if execution[key]:
                lines.append(f"{label} (last lines):")
                lines.extend(execution[key][-SUMMARY_LOG_LINES:])
        if execution["responseBody"]:
            lines.append("Response body: " + execution["responseBody"][:500])
    return "\n".join(lines)


def _duration(seconds: Any) -> str:
    # Executions that have not run yet report 0.
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return "–"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    return f"{seconds:.2f}s"
