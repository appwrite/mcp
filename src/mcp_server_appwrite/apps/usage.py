"""``view=usage``: a project's traffic, compute and resources over a range.

Two reads of the Usage API make the dashboard: ``usage_list_events`` for what
happened in each interval (requests, bandwidth in and out, executions and
their compute time) and ``usage_list_gauges`` for where the project stands
(users, files and deployment storage, and resource counts). Event series are
filled with zeros for intervals without traffic, so charts keep their shape.

Appwrite keeps usage history for as long as the plan allows. A range past it
fails with Appwrite's own message, which the view shows in place.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

Read = Callable[[str, Any, dict[str, Any]], dict[str, Any]]
"""Runs a hidden read tool: ``(tool name, project ID, arguments)``."""


@dataclass(frozen=True)
class Range:
    interval: str
    step: timedelta
    points: int


RANGES = {
    "24h": Range("1h", timedelta(hours=1), 24),
    "7d": Range("1d", timedelta(days=1), 7),
    "30d": Range("1d", timedelta(days=1), 30),
}

EVENTS = {
    "requests": "network.requests",
    "inbound": "network.inbound",
    "outbound": "network.outbound",
    "executions": "executions",
    "compute": "executions.compute",
}
"""Series key → Usage API event metric."""

GAUGES = {
    "users": "users",
    "functions": "functions",
    "sites": "sites",
    "databases": "databases",
    "buckets": "buckets",
    "filesStorage": "files.storage",
    "deploymentsStorage": "deployments.storage",
    "buildsStorage": "builds.storage",
}
"""Resource key → Usage API gauge metric."""


def read(
    read: Read, arguments: dict[str, Any], now: datetime | None = None
) -> dict[str, Any]:
    project_id = arguments.get("project_id")
    name = arguments.get("range") or "7d"
    if name not in RANGES:
        raise ValueError(f"range must be one of: {', '.join(RANGES)}.")
    span = RANGES[name]
    end = _floor(now or datetime.now(UTC), span.step)
    start = end - span.step * (span.points - 1)
    window = {
        "interval": span.interval,
        "start_at": start.isoformat(),
        "order_by": "time",
        "order_dir": "asc",
    }

    events = read(
        "usage_list_events",
        project_id,
        {**window, "metrics": list(EVENTS.values())},
    )
    gauges = read(
        "usage_list_gauges",
        project_id,
        {**window, "metrics": list(GAUGES.values())},
    )
    times = [start + span.step * index for index in range(span.points)]
    points = _points(events)
    series = {
        key: [points.get(metric, {}).get(time, 0) for time in times]
        for key, metric in EVENTS.items()
    }
    latest = _latest(gauges)
    return {
        "view": "usage",
        "projectId": project_id,
        "range": name,
        "interval": span.interval,
        "times": [time.isoformat() for time in times],
        "series": series,
        "totals": {key: sum(values) for key, values in series.items()},
        "resources": {key: latest.get(metric) for key, metric in GAUGES.items()},
    }


def _floor(moment: datetime, step: timedelta) -> datetime:
    """The start of the interval ``moment`` falls in, as Appwrite buckets it."""
    seconds = int(step.total_seconds())
    epoch = int(moment.timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, UTC)


def _metrics(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [each for each in response.get("metrics") or [] if isinstance(each, dict)]


def _points(response: dict[str, Any]) -> dict[str, dict[datetime, int]]:
    """Each metric's values by interval start."""
    found: dict[str, dict[datetime, int]] = {}
    for metric in _metrics(response):
        values = found.setdefault(str(metric.get("metric")), {})
        for point in metric.get("points") or []:
            try:
                time = datetime.fromisoformat(str(point["time"])).astimezone(UTC)
            except (KeyError, ValueError):
                continue
            values[time] = values.get(time, 0) + int(point.get("value") or 0)
    return found


def _latest(response: dict[str, Any]) -> dict[str, int]:
    """Each gauge's newest snapshot."""
    found: dict[str, int] = {}
    for metric in _metrics(response):
        points = metric.get("points") or []
        if points:
            newest = max(points, key=lambda point: str(point.get("time")))
            found[str(metric.get("metric"))] = int(newest.get("value") or 0)
    return found


def bytes_text(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if amount < 1000 or unit == "GB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1000
    return f"{amount:.1f} TB"


def summary(data: dict[str, Any]) -> str:
    totals = data["totals"]
    resources = data["resources"]
    lines = [
        f"Usage of project {data['projectId']} over the last {data['range']} "
        f"({data['interval']} intervals):",
        f"Requests: {totals['requests']:,}",
        f"Bandwidth: {bytes_text(totals['inbound'] + totals['outbound'])} "
        f"({bytes_text(totals['inbound'])} in, {bytes_text(totals['outbound'])} out)",
        f"Executions: {totals['executions']:,} "
        f"({totals['compute'] / 1000:,.1f} s of compute)",
    ]
    counts = [
        f"{key} {resources[key]:,}"
        for key in ("users", "functions", "sites", "databases", "buckets")
        if resources[key] is not None
    ]
    if counts:
        lines.append("Now: " + ", ".join(counts) + ".")
    storage = [
        f"{label} {bytes_text(resources[key])}"
        for key, label in (
            ("filesStorage", "files"),
            ("deploymentsStorage", "deployments"),
            ("buildsStorage", "builds"),
        )
        if resources[key] is not None
    ]
    if storage:
        lines.append("Storage: " + ", ".join(storage) + ".")
    busiest = max(
        range(len(data["times"])), key=lambda index: data["series"]["requests"][index]
    )
    if totals["requests"]:
        lines.append(
            f"Busiest interval: {data['times'][busiest]} with "
            f"{data['series']['requests'][busiest]:,} requests."
        )
    return "\n".join(lines)
