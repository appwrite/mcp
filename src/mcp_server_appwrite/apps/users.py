"""``view=users``: a project's Auth users, and one user in full.

The list is a page of users, newest first, optionally narrowed by a search
over name, email, phone and ID. With ``user_id`` the payload also carries that
user's sessions. The view offers blocking and unblocking a user, verifying
their email and editing their labels, each through ``appwrite_view_write``
after the user confirms it.

User documents hold the password hash, its options, prefs and messaging
targets, and sessions hold OAuth provider tokens. None of it is needed to show
a user, so only the fields below reach the view or the model.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from appwrite_console.query import Query

from ..constants import SHOW_TOOL_NAME

Read = Callable[[str, Any, dict[str, Any]], dict[str, Any]]
"""Runs a hidden read tool: ``(tool name, project ID, arguments)``."""

PAGE_SIZE = 25

SESSIONS = 10
"""Sessions an opened user carries, newest first."""

WRITES = (
    "users_update_status",
    "users_update_labels",
    "users_update_email_verification",
)
"""The writes the view offers on a user."""

SUMMARY_ROWS = 10


def read(read: Read, arguments: dict[str, Any]) -> dict[str, Any]:
    project_id = arguments.get("project_id")
    search = arguments.get("search") or None
    cursor = arguments.get("cursor") or None
    user_id = arguments.get("user_id") or None

    # One past the page tells whether another page follows.
    queries = [Query.order_desc("$createdAt"), Query.limit(PAGE_SIZE + 1)]
    if cursor:
        queries.append(Query.cursor_after(str(cursor)))
    listing: dict[str, Any] = {"queries": queries}
    if search:
        listing["search"] = str(search)
    page = read("users_list", project_id, listing)
    rows = [user(each) for each in page.get("users") or []]
    more = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]

    opened = None
    if user_id:
        opened = user(read("users_get", project_id, {"user_id": user_id}))
        sessions = read("users_list_sessions", project_id, {"user_id": user_id})
        listed = sessions.get("sessions") or []
        opened["sessions"] = [_session(each) for each in listed[-SESSIONS:][::-1]]
        opened["sessionsTotal"] = int(sessions.get("total", len(listed)))
    return {
        "view": "users",
        "projectId": project_id,
        "search": search,
        "users": rows,
        "total": int(page.get("total", len(rows))),
        "cursor": cursor,
        "next": rows[-1]["id"] if more else None,
        "user": opened,
    }


def user(document: dict[str, Any]) -> dict[str, Any]:
    """The fields of a ``User`` the view shows; never its password hash,
    prefs or targets."""
    return {
        "id": document.get("$id"),
        "name": document.get("name") or "",
        "email": document.get("email") or "",
        "phone": document.get("phone") or "",
        "status": bool(document.get("status", True)),
        "emailVerification": bool(document.get("emailVerification")),
        "phoneVerification": bool(document.get("phoneVerification")),
        "mfa": bool(document.get("mfa")),
        "labels": [str(label) for label in document.get("labels") or []],
        "createdAt": document.get("$createdAt"),
        "accessedAt": document.get("accessedAt") or None,
    }


def _session(session: dict[str, Any]) -> dict[str, Any]:
    """The fields of a ``Session`` the view shows; never provider tokens or
    the secret."""
    client = " ".join(
        str(part)
        for part in (session.get("clientName"), session.get("clientVersion"))
        if part
    )
    system = " ".join(
        str(part) for part in (session.get("osName"), session.get("osVersion")) if part
    )
    return {
        "id": session.get("$id"),
        "provider": session.get("provider") or "",
        "client": client,
        "os": system,
        "device": session.get("deviceName") or "",
        "country": session.get("countryName") or "",
        "ip": session.get("ip") or "",
        "createdAt": session.get("$createdAt"),
        "expire": session.get("expire"),
    }


def _contact(row: dict[str, Any]) -> str:
    return row["email"] or row["phone"] or ""


def _state(row: dict[str, Any]) -> str:
    states = ["active" if row["status"] else "blocked"]
    if row["email"]:
        states.append("verified" if row["emailVerification"] else "unverified")
    if row["mfa"]:
        states.append("mfa")
    return ", ".join(states)


def summary(data: dict[str, Any]) -> str:
    rows: list[dict[str, Any]] = data["users"]
    scope = f" matching {data['search']!r}" if data["search"] else ""
    lines = [
        f"{data['total']} users{scope}, showing {len(rows)} newest"
        + (" after the cursor." if data["cursor"] else ".")
    ]
    if rows:
        lines.append("id | name | email or phone | state | labels | joined")
        for row in rows[:SUMMARY_ROWS]:
            lines.append(
                " | ".join(
                    [
                        row["id"],
                        row["name"],
                        _contact(row),
                        _state(row),
                        ",".join(row["labels"]),
                        row["createdAt"] or "",
                    ]
                )
            )
        if len(rows) > SUMMARY_ROWS:
            lines.append(f"… {len(rows) - SUMMARY_ROWS} more shown in the view.")
    if data["next"]:
        lines.append(
            f"Next page: {SHOW_TOOL_NAME} with view=users and cursor={data['next']}."
        )
    opened = data["user"]
    if opened:
        lines.append(
            f"User {opened['id']} ({opened['name'] or _contact(opened)}): "
            f"{_state(opened)}; {opened['sessionsTotal']} sessions; last active "
            f"{opened['accessedAt'] or 'never'}."
        )
    return "\n".join(lines)
