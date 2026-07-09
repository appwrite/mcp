"""OpenTelemetry metrics for the hosted Appwrite MCP server.

This module mirrors the ``utopia-php/telemetry`` pattern used by the other Appwrite
services: it exports OpenTelemetry metrics over OTLP/HTTP to an OpenTelemetry
Collector, which forwards them to the shared Prometheus/Mimir + Grafana stack
(``telemetry.appwrite.systems``).

The metric names follow the "MCP Server Observability" reference dashboard
(grafana.com/grafana/dashboards/25252): after the standard OTLP-to-Prometheus
translation (dots become underscores, unit and ``_total`` suffixes appended)
the instruments below land as ``mcp_tool_calls_total``,
``mcp_tool_duration_seconds_bucket``, ``mcp_active_sessions``, and so on.
Only metrics queried by the hosted ``MCP`` Grafana dashboard
(appwrite-labs/dashboards ``mcp.json``) are emitted — when a panel is removed
there, drop its instrument here too.

Design notes:

* **No-op by default.** Like the PHP ``None``/``NoTelemetry`` adapter, the module
  emits nothing unless an OTLP endpoint is configured *and* the server runs the
  hosted ``http`` transport. The self-hosted ``stdio`` transport runs on users'
  machines and must never phone home, so ``init_telemetry`` is a no-op there.
* **Exception-safe.** Every ``record_*`` helper swallows its own errors —
  telemetry must never break a request.
* **Cardinality-disciplined.** No user id (``sub``), token, raw query text, file
  name, result id, or IP is ever used as a metric attribute. ``client_id`` is the
  MCP client *name* (``claude-code``, ``cursor``, ...), a small bounded set.
  Distinct-user and per-client session counts are derived in-process from rolling
  TTL sets and exposed only as aggregate gauges.
* **Stateless sessions.** The hosted transport is stateless — every HTTP request
  is its own short-lived MCP session. "Session" metrics therefore describe user
  activity windows: a session starts when a (client, user) pair is first seen and
  ends after ``ACTIVE_WINDOW_SECONDS`` without traffic ("idle" disconnect).

Configuration (env):

* ``OTEL_EXPORTER_OTLP_ENDPOINT`` — OTLP/HTTP endpoint; setting it enables export.
  In the cluster this points at the in-cluster Alloy collector, which authenticates
  and forwards upstream and stamps the ``deployment.*`` resource attributes — so the
  app needs no credentials and no per-deployment resource attributes.
* ``OTEL_SERVICE_NAME`` / ``OTEL_RESOURCE_ATTRIBUTES`` — picked up by the SDK to set
  ``service.name`` etc.
"""

from __future__ import annotations

import os
import re
import socket
import sys
import threading
import time
from contextvars import ContextVar
from typing import Any, Iterable

from .constants import ACTIVE_WINDOW_SECONDS

_enabled = False
_lock = threading.Lock()

# Metric instruments, populated by init_telemetry when enabled.
_instruments: dict[str, Any] = {}

# Rolling TTL stores behind the observable gauges. All expire after
# ACTIVE_WINDOW_SECONDS so the gauges reflect a recent window, not all time.
_active_users: dict[str, float] = {}  # subject -> expiry
# (client, subject) -> [first_seen, expiry]; pruning records session duration
# and an "idle" disconnect.
_active_sessions: dict[tuple[str, str], list[float]] = {}
# (protocol version, client, subject) -> expiry
_active_versions: dict[tuple[str, str, str], float] = {}
_active_lock = threading.Lock()

# Dedupe connection events: one per MCP session object.
_seen_sessions: set[int] = set()

# Identity of the request currently being served, set once per request by the
# MCP handlers and read by every record helper that labels by client.
_request_client: ContextVar[str] = ContextVar("appwrite_mcp_client", default="unknown")

# CPU gauge state: previous (wall clock, process cpu time) sample.
_cpu_sample: list[float] = []

_BYTE_BUCKETS: list[float] = [
    64,
    256,
    1024,
    4096,
    16384,
    65536,
    262144,
    1048576,
    4194304,
    16777216,
]
_TOKEN_BUCKETS: list[float] = [16, 64, 256, 1024, 4096, 16384, 65536, 262144]
# Rough but stable chars-per-token heuristic for JSON/English payloads.
_CHARS_PER_TOKEN = 4

_TOOL_NAME_SAFE = re.compile(r"[^A-Za-z0-9_.:-]")


def _log(message: str) -> None:
    print(f"[appwrite-mcp][telemetry] {message}", file=sys.stderr, flush=True)


def is_enabled() -> bool:
    return _enabled


def _resolve_endpoint() -> str | None:
    return os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")


def init_telemetry(transport: str, version: str) -> bool:
    """Configure the global meter provider and build instruments.

    Returns True if telemetry was enabled. A no-op (returns False) unless the
    transport is ``http`` and an OTLP endpoint is configured.
    """
    global _enabled
    with _lock:
        if _enabled:
            return True

        if transport != "http":
            return False

        endpoint = _resolve_endpoint()
        if not endpoint:
            _log("disabled: no OTLP endpoint configured")
            return False

        try:
            from opentelemetry import metrics
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
                OTLPMetricExporter,
            )
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.sdk.resources import Resource

            os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", endpoint)

            # Resource.create() merges OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES
            # from the environment over these defaults.
            resource = Resource.create(
                {
                    "service.name": "mcp-server-appwrite",
                    "service.namespace": "appwrite",
                    "service.version": version,
                    "service.instance.id": _instance_id(),
                }
            )
            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(), export_interval_millis=60_000
            )
            provider = MeterProvider(resource=resource, metric_readers=[reader])
            metrics.set_meter_provider(provider)
            meter = provider.get_meter("mcp_server_appwrite", version)
            _build_instruments(meter, transport, version)
        except Exception as exc:  # pragma: no cover - defensive
            _log(f"disabled: failed to initialize ({exc})")
            return False

        _enabled = True
        _log(f"enabled: exporting metrics to {endpoint}")
        return True


def _instance_id() -> str:
    return os.getenv("HOSTNAME") or socket.gethostname() or "unknown"


def _histogram(
    meter: Any,
    name: str,
    *,
    unit: str,
    description: str,
    boundaries: list[float] | None = None,
) -> Any:
    if boundaries is not None:
        try:
            return meter.create_histogram(
                name,
                unit=unit,
                description=description,
                explicit_bucket_boundaries_advisory=boundaries,
            )
        except TypeError:  # pragma: no cover - older SDK without advisory support
            pass
    return meter.create_histogram(name, unit=unit, description=description)


def _build_instruments(meter: Any, transport: str, version: str) -> None:
    # --- Transport & sessions ------------------------------------------------
    _instruments["handshake"] = meter.create_counter(
        "mcp.handshake",
        unit="{handshake}",
        description="MCP handshake outcomes: initialized sessions vs rejected tokens.",
    )
    _instruments["session_duration"] = _histogram(
        meter,
        "mcp.session.duration",
        unit="s",
        description="User activity-session length (first to last request, idle-bounded).",
    )
    _instruments["session_disconnects"] = meter.create_counter(
        "mcp.session.disconnects",
        unit="{disconnect}",
        description="Activity-session ends by reason (stateless transport: idle expiry).",
    )

    # --- Protocol & messages -------------------------------------------------
    _instruments["messages_received"] = meter.create_counter(
        "mcp.messages.received",
        unit="{message}",
        description="Instrumented MCP JSON-RPC handler calls by message type.",
    )
    _instruments["message_latency"] = _histogram(
        meter,
        "mcp.message.latency",
        unit="s",
        description="MCP message handler duration.",
    )
    _instruments["jsonrpc_errors"] = meter.create_counter(
        "mcp.jsonrpc.errors",
        unit="{error}",
        description="MCP handler failures by JSON-RPC error code.",
    )
    _instruments["message_size"] = _histogram(
        meter,
        "mcp.message.size",
        unit="By",
        description="Approximate MCP payload size by direction.",
        boundaries=_BYTE_BUCKETS,
    )

    # --- Tool execution -------------------------------------------------------
    _instruments["tool_calls"] = meter.create_counter(
        "mcp.tool.calls",
        unit="{call}",
        description="Public operator tool invocations.",
    )
    _instruments["tool_duration"] = _histogram(
        meter,
        "mcp.tool.duration",
        unit="s",
        description="Public operator tool duration.",
    )
    _instruments["tool_errors"] = meter.create_counter(
        "mcp.tool.errors",
        unit="{error}",
        description="Failed public operator tool invocations by error type.",
    )
    _instruments["tool_inflight"] = meter.create_up_down_counter(
        "mcp.tool.inflight",
        unit="{call}",
        description="Tool calls currently executing, per tool.",
    )
    _instruments["tool_inflight_total"] = meter.create_up_down_counter(
        "mcp.tool.inflight.total",
        unit="{call}",
        description="Tool calls currently executing across all tools.",
    )
    _instruments["tool_result_size"] = _histogram(
        meter,
        "mcp.tool.result.size",
        unit="By",
        description="Tool result payload size.",
        boundaries=_BYTE_BUCKETS,
    )
    _instruments["tool_hallucination"] = meter.create_counter(
        "mcp.tool.hallucination",
        unit="{call}",
        description="Calls to tools that do not exist (hallucinated tool names).",
    )

    # --- Agentic & token metrics ----------------------------------------------
    _instruments["token_usage"] = meter.create_counter(
        "mcp.token.usage",
        unit="{token}",
        description="Estimated tokens moved through tools (chars/4 heuristic).",
    )
    _instruments["token_usage_per_call"] = _histogram(
        meter,
        "mcp.token.usage.per.call",
        unit="{token}",
        description="Estimated tokens per tool call (input + output).",
        boundaries=_TOKEN_BUCKETS,
    )

    # --- Observable gauges ------------------------------------------------------
    meter.create_observable_gauge(
        "mcp.active_sessions",
        callbacks=[_observe_active_sessions],
        unit="{session}",
        description="Distinct authenticated users active in the last 5 minutes.",
    )
    meter.create_observable_gauge(
        "mcp.active_sessions.by_client",
        callbacks=[_observe_active_sessions_by_client],
        unit="{session}",
        description="Active (client, user) sessions in the last 5 minutes, per client.",
    )
    meter.create_observable_gauge(
        "mcp.protocol.version.count",
        callbacks=[_observe_protocol_versions],
        unit="{session}",
        description="Active sessions by negotiated MCP protocol version and client.",
    )
    meter.create_observable_gauge(
        "mcp.cpu.usage.percent",
        callbacks=[_observe_cpu],
        description="Process CPU usage since the previous observation, percent.",
    )
    meter.create_observable_gauge(
        "mcp.memory.usage.mb",
        callbacks=[_observe_memory],
        description="Process resident memory, megabytes.",
    )

    def _observe_info(_options: Any):
        from opentelemetry.metrics import Observation

        return [Observation(1, {"version": version, "transport": transport})]

    meter.create_observable_gauge(
        "mcp.server.info",
        callbacks=[_observe_info],
        unit="{server}",
        description="Server build info (value is always 1).",
    )


# --- Request identity ---------------------------------------------------------

_CLIENT_NAME_WHITESPACE = re.compile(r"\s+")


def _normalize_client_name(name: str | None) -> str | None:
    """Canonicalize a client-reported name so one client maps to one
    ``client_id`` label value. ``clientInfo.name`` arrives raw from the
    initialize request while User-Agent-derived names are lowercased product
    tokens, so the same client would otherwise split (``Trae`` vs ``trae``)."""
    if not name:
        return None
    text = _CLIENT_NAME_WHITESPACE.sub("-", str(name).strip().lower())[:64]
    return text or None


def set_request_identity(
    *,
    client_name: str | None,
    subject: str | None,
    protocol_version: str | None = None,
) -> None:
    """Bind the current request's client/user identity to the calling context and
    refresh the rolling activity stores. Contextvars propagate into the worker
    threads that execute tools, so record helpers can label by client."""
    # Never downgrade an identity already bound for this request (e.g. by the
    # HTTP-layer middleware) to "unknown".
    client = _normalize_client_name(client_name) or _request_client.get()
    _request_client.set(client)
    if not _enabled:
        # The rolling stores are only pruned by the gauge callbacks, which never
        # run while disabled — do not let them grow.
        return
    now = time.monotonic()
    expiry = now + ACTIVE_WINDOW_SECONDS
    with _active_lock:
        if subject:
            _active_users[subject] = expiry
            session = _active_sessions.get((client, subject))
            if session is None:
                _active_sessions[(client, subject)] = [now, expiry]
            else:
                session[1] = expiry
            if protocol_version:
                _active_versions[(protocol_version, client, subject)] = expiry


def current_client_id() -> str:
    return _request_client.get()


# --- Active-set bookkeeping -----------------------------------------------------


def _prune(store: dict, now: float) -> list:
    expired = [key for key, value in store.items() if _expiry(value) < now]
    for key in expired:
        del store[key]
    return expired


def _expiry(value: Any) -> float:
    return value[1] if isinstance(value, list) else value


def _observe_active_sessions(_options: Any) -> Iterable[Any]:
    from opentelemetry.metrics import Observation

    now = time.monotonic()
    with _active_lock:
        _prune(_active_users, now)
        _prune(_active_versions, now)
        count = len(_active_users)
    return [Observation(count)]


def _observe_active_sessions_by_client(_options: Any) -> Iterable[Any]:
    from opentelemetry.metrics import Observation

    now = time.monotonic()
    counts: dict[str, int] = {}
    ended: list[tuple[tuple[str, str], list[float]]] = []
    with _active_lock:
        expired = [key for key, session in _active_sessions.items() if session[1] < now]
        for key in expired:
            ended.append((key, _active_sessions.pop(key)))
        for client, _subject in _active_sessions:
            counts[client] = counts.get(client, 0) + 1
    # Expired sessions were idle for the full window; the session itself lasted
    # from first sight to the start of that idle period.
    for (client, _subject), (first_seen, expiry) in ended:
        duration = max(0.0, (expiry - ACTIVE_WINDOW_SECONDS) - first_seen)
        _safe_record("session_duration", duration, {})
        _safe_add("session_disconnects", 1, {"client_id": client, "reason": "idle"})
    return [Observation(n, {"client_id": name}) for name, n in counts.items()]


def _observe_protocol_versions(_options: Any) -> Iterable[Any]:
    from opentelemetry.metrics import Observation

    now = time.monotonic()
    counts: dict[tuple[str, str], int] = {}
    with _active_lock:
        _prune(_active_versions, now)
        for version, client, _subject in _active_versions:
            counts[(version, client)] = counts.get((version, client), 0) + 1
    return [
        Observation(n, {"version": version, "client_id": client})
        for (version, client), n in counts.items()
    ]


def _observe_cpu(_options: Any) -> Iterable[Any]:
    from opentelemetry.metrics import Observation

    times = os.times()
    wall = time.monotonic()
    cpu = times.user + times.system
    if not _cpu_sample:
        _cpu_sample.extend([wall, cpu])
        return []
    prev_wall, prev_cpu = _cpu_sample[0], _cpu_sample[1]
    _cpu_sample[0], _cpu_sample[1] = wall, cpu
    elapsed = wall - prev_wall
    if elapsed <= 0:
        return []
    return [Observation(max(0.0, (cpu - prev_cpu) / elapsed * 100.0))]


def _observe_memory(_options: Any) -> Iterable[Any]:
    from opentelemetry.metrics import Observation

    rss = _resident_bytes()
    if rss is None:
        return []
    return [Observation(rss / (1024 * 1024))]


def _resident_bytes() -> float | None:
    try:
        with open("/proc/self/status", encoding="ascii", errors="ignore") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return float(line.split()[1]) * 1024
    except OSError:
        pass
    try:
        import resource

        peak = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        # ru_maxrss is bytes on macOS, kilobytes on Linux.
        return peak if sys.platform == "darwin" else peak * 1024
    except Exception:  # pragma: no cover - defensive
        return None


# --- Record helpers (all exception-safe, no-op when disabled) --------------------


def _safe_add(name: str, value: int, attributes: dict[str, Any]) -> None:
    if not _enabled:
        return
    try:
        _instruments[name].add(value, _clean(attributes))
    except Exception:  # pragma: no cover - defensive
        pass


def _safe_record(name: str, value: float, attributes: dict[str, Any]) -> None:
    if not _enabled:
        return
    try:
        _instruments[name].record(value, _clean(attributes))
    except Exception:  # pragma: no cover - defensive
        pass


def _clean(attributes: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in attributes.items() if v is not None}


def _estimate_tokens(size_chars: int) -> int:
    return max(1, size_chars // _CHARS_PER_TOKEN) if size_chars > 0 else 0


def _sanitize_tool_name(name: Any) -> str:
    text = str(name) if name is not None else ""
    text = _TOOL_NAME_SAFE.sub("_", text)[:64]
    return text or "invalid"


# --- Transport & sessions ---------------------------------------------------------


def record_connection(
    *,
    session_id: int,
    client_name: str | None,
    protocol_version: str | None,
    subject: str | None,
) -> None:
    """Count a successful MCP handshake, deduped per session object, and refresh
    the request-identity stores."""
    set_request_identity(
        client_name=client_name,
        subject=subject,
        protocol_version=protocol_version,
    )
    if not _enabled:
        return
    with _active_lock:
        if session_id in _seen_sessions:
            return
        _seen_sessions.add(session_id)
        # Bound the dedupe set so it can't grow without limit on a long-lived process.
        if len(_seen_sessions) > 100_000:
            _seen_sessions.clear()
            _seen_sessions.add(session_id)
    client = _normalize_client_name(client_name) or "unknown"
    _safe_add("handshake", 1, {"status": "success", "client_id": client})


def record_handshake_failure(reason: str | None = None) -> None:
    """A presented bearer token was rejected — the session never initialized."""
    _safe_add(
        "handshake",
        1,
        {"status": "failure", "client_id": current_client_id(), "reason": reason},
    )


# --- Protocol & messages ------------------------------------------------------------


def record_message(
    method: str,
    outcome: str,
    duration_s: float,
    *,
    error_code: int | None = None,
    error_message: str | None = None,
) -> None:
    _safe_add(
        "messages_received",
        1,
        {"msg_type": method, "client_id": current_client_id(), "outcome": outcome},
    )
    _safe_record("message_latency", duration_s, {"msg_type": method})
    if outcome == "error":
        _safe_add(
            "jsonrpc_errors",
            1,
            {
                "error_code": str(error_code if error_code is not None else -32603),
                "error_message": error_message or "InternalError",
            },
        )


def record_message_size(direction: str, size_bytes: int) -> None:
    _safe_record("message_size", size_bytes, {"direction": direction})


# --- Tool execution --------------------------------------------------------------------


def tool_call_started(tool_name: str) -> None:
    _safe_add("tool_inflight", 1, {"tool_name": tool_name})
    _safe_add("tool_inflight_total", 1, {})


def record_tool_call(
    tool_name: str,
    status: str,
    duration_s: float,
    *,
    error_type: str | None = None,
    input_chars: int | None = None,
    output_chars: int | None = None,
) -> None:
    """Finish a tool call started with ``tool_call_started``."""
    client = current_client_id()
    _safe_add("tool_inflight", -1, {"tool_name": tool_name})
    _safe_add("tool_inflight_total", -1, {})
    _safe_add(
        "tool_calls",
        1,
        {"tool_name": tool_name, "client_id": client, "status": status},
    )
    _safe_record("tool_duration", duration_s, {"tool_name": tool_name})
    if status == "error":
        _safe_add(
            "tool_errors",
            1,
            {"tool_name": tool_name, "error_type": error_type or "unknown"},
        )

    input_tokens = _estimate_tokens(input_chars or 0)
    output_tokens = _estimate_tokens(output_chars or 0)
    if input_chars:
        record_message_size("received", input_chars)
        _safe_add(
            "token_usage", input_tokens, {"tool_name": tool_name, "direction": "input"}
        )
    if output_chars:
        record_message_size("sent", output_chars)
        _safe_record("tool_result_size", output_chars, {"tool_name": tool_name})
        _safe_add(
            "token_usage",
            output_tokens,
            {"tool_name": tool_name, "direction": "output"},
        )
    if input_chars or output_chars:
        _safe_record(
            "token_usage_per_call",
            input_tokens + output_tokens,
            {"tool_name": tool_name},
        )


def record_hallucination(attempted_tool: Any) -> None:
    _safe_add(
        "tool_hallucination",
        1,
        {
            "attempted_tool": _sanitize_tool_name(attempted_tool),
            "client_id": current_client_id(),
        },
    )
