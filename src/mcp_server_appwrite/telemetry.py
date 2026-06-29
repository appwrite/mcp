"""OpenTelemetry metrics for the hosted Appwrite MCP server.

This module mirrors the ``utopia-php/telemetry`` pattern used by the other Appwrite
services: it exports OpenTelemetry metrics over OTLP/HTTP to an OpenTelemetry
Collector, which forwards them to the shared Prometheus/Mimir + Grafana stack
(``telemetry.appwrite.systems``).

Design notes:

* **No-op by default.** Like the PHP ``None``/``NoTelemetry`` adapter, the module
  emits nothing unless an OTLP endpoint is configured *and* the server runs the
  hosted ``http`` transport. The self-hosted ``stdio`` transport runs on users'
  machines and must never phone home, so ``init_telemetry`` is a no-op there.
* **Exception-safe.** Every ``record_*`` helper swallows its own errors —
  telemetry must never break a request.
* **Cardinality-disciplined.** No user id (``sub``), token, raw query text, file
  name, result id, or IP is ever used as a metric attribute. Distinct-user and
  distinct-client counts are derived in-process from rolling TTL sets and exposed
  only as aggregate gauges.

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
import socket
import sys
import threading
import time
from typing import Any, Iterable

_ACTIVE_WINDOW_SECONDS = 300.0  # rolling window for "active users/clients" gauges

_enabled = False
_lock = threading.Lock()

# Metric instruments, populated by init_telemetry when enabled.
_instruments: dict[str, Any] = {}

# Rolling TTL sets for the active-user/active-client observable gauges. Keys expire
# after _ACTIVE_WINDOW_SECONDS so the gauges reflect a recent window, not all time.
_active_users: dict[str, float] = {}
_active_clients: dict[str, float] = {}  # key: client name -> last-seen monotonic-ish ts
_active_lock = threading.Lock()

# Dedupe initialize events: one per MCP session object.
_seen_sessions: set[int] = set()


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


def _build_instruments(meter: Any, transport: str, version: str) -> None:
    _instruments["initializations"] = meter.create_counter(
        "mcp.initializations",
        unit="{init}",
        description="MCP initialize handshakes (connections / logins).",
    )
    _instruments["requests"] = meter.create_counter(
        "mcp.requests",
        unit="{request}",
        description="MCP JSON-RPC requests handled.",
    )
    _instruments["request_duration"] = meter.create_histogram(
        "mcp.request.duration",
        unit="s",
        description="MCP request handler duration.",
    )
    _instruments["tool_calls"] = meter.create_counter(
        "mcp.tool.calls",
        unit="{call}",
        description="Public operator tool invocations.",
    )
    _instruments["tool_duration"] = meter.create_histogram(
        "mcp.tool.duration",
        unit="s",
        description="Public operator tool duration.",
    )
    _instruments["appwrite_calls"] = meter.create_counter(
        "mcp.appwrite.calls",
        unit="{call}",
        description="Hidden Appwrite catalog tool executions.",
    )
    _instruments["appwrite_call_duration"] = meter.create_histogram(
        "mcp.appwrite.call.duration",
        unit="s",
        description="Underlying Appwrite REST call duration.",
    )
    _instruments["appwrite_errors"] = meter.create_counter(
        "mcp.appwrite.errors",
        unit="{error}",
        description="Failed Appwrite catalog tool executions.",
    )
    _instruments["write_confirmations"] = meter.create_counter(
        "mcp.write.confirmations",
        unit="{confirmation}",
        description="Write/delete confirmation outcomes (confirmed vs blocked).",
    )
    _instruments["search_tools_queries"] = meter.create_counter(
        "mcp.search_tools.queries",
        unit="{query}",
        description="appwrite_search_tools catalog searches.",
    )
    _instruments["search_tools_results"] = meter.create_histogram(
        "mcp.search_tools.results",
        unit="{match}",
        description="Match count returned by appwrite_search_tools.",
    )
    _instruments["search_docs_queries"] = meter.create_counter(
        "mcp.search_docs.queries",
        unit="{query}",
        description="appwrite_search_docs documentation searches.",
    )
    _instruments["search_docs_embedding_duration"] = meter.create_histogram(
        "mcp.search_docs.embedding.duration",
        unit="s",
        description="Query embedding duration for docs search.",
    )
    _instruments["context_requests"] = meter.create_counter(
        "mcp.context.requests",
        unit="{request}",
        description="appwrite_get_context invocations.",
    )
    _instruments["auth_validations"] = meter.create_counter(
        "mcp.auth.validations",
        unit="{validation}",
        description="Bearer-token validation outcomes.",
    )
    _instruments["auth_duration"] = meter.create_histogram(
        "mcp.auth.duration",
        unit="s",
        description="Bearer-token verification duration.",
    )
    _instruments["results_stored"] = meter.create_counter(
        "mcp.results.stored",
        unit="{result}",
        description="Large tool results spilled to MCP resources.",
    )
    _instruments["resources_reads"] = meter.create_counter(
        "mcp.resources.reads",
        unit="{read}",
        description="resources/read calls.",
    )
    _instruments["uploads"] = meter.create_counter(
        "mcp.uploads",
        unit="{upload}",
        description="File upload attempts.",
    )
    _instruments["upload_bytes"] = meter.create_histogram(
        "mcp.upload.bytes",
        unit="By",
        description="Uploaded file size.",
    )
    _instruments["upload_errors"] = meter.create_counter(
        "mcp.upload.errors",
        unit="{error}",
        description="File upload failures.",
    )
    _instruments["startup_validation"] = meter.create_counter(
        "mcp.startup.validation",
        unit="{probe}",
        description="Startup service-validation probe outcomes.",
    )

    # Observable gauges: distinct active users/clients over a rolling window, and a
    # build-info gauge (always 1, carries version/transport labels).
    meter.create_observable_gauge(
        "mcp.users.active",
        callbacks=[_observe_active_users],
        unit="{user}",
        description="Distinct authenticated users seen in the last 5 minutes.",
    )
    meter.create_observable_gauge(
        "mcp.clients.active",
        callbacks=[_observe_active_clients],
        unit="{client}",
        description="Distinct connected agents seen in the last 5 minutes.",
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


# --- Active-set bookkeeping -------------------------------------------------


def _prune(store: dict[str, float], now: float) -> None:
    expired = [key for key, ts in store.items() if ts < now]
    for key in expired:
        del store[key]


def _observe_active_users(_options: Any) -> Iterable[Any]:
    from opentelemetry.metrics import Observation

    now = time.monotonic()
    with _active_lock:
        _prune(_active_users, now)
        count = len(_active_users)
    return [Observation(count)]


def _observe_active_clients(_options: Any) -> Iterable[Any]:
    from opentelemetry.metrics import Observation

    now = time.monotonic()
    counts: dict[str, int] = {}
    with _active_lock:
        _prune(_active_clients, now)
        for compound_key in _active_clients:
            client_name = compound_key.split("\x00", 1)[0]
            counts[client_name] = counts.get(client_name, 0) + 1
    return [Observation(n, {"client.name": name}) for name, n in counts.items()]


def _touch_user(subject: str | None) -> None:
    if not subject:
        return
    expiry = time.monotonic() + _ACTIVE_WINDOW_SECONDS
    with _active_lock:
        _active_users[subject] = expiry


def _touch_client(client_name: str | None, subject: str | None) -> None:
    # Keyed by (client_name, subject) so the per-client gauge counts distinct users
    # of each agent. subject stays in-process only.
    if not client_name:
        return
    key = f"{client_name}\x00{subject or ''}"
    expiry = time.monotonic() + _ACTIVE_WINDOW_SECONDS
    with _active_lock:
        _active_clients[key] = expiry


# --- Record helpers (all exception-safe, no-op when disabled) ---------------


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


def record_request(method: str, outcome: str, duration_s: float) -> None:
    _safe_add("requests", 1, {"mcp.method": method, "outcome": outcome})
    _safe_record(
        "request_duration", duration_s, {"mcp.method": method, "outcome": outcome}
    )


def record_initialize(
    *,
    session_id: int,
    client_name: str | None,
    client_version: str | None,
    protocol_version: str | None,
    oauth_client_id: str | None,
    subject: str | None,
) -> None:
    # Track active users/clients regardless of whether this is a new session.
    _touch_user(subject)
    _touch_client(client_name, subject)
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
    _safe_add(
        "initializations",
        1,
        {
            "client.name": client_name or "unknown",
            "client.version": client_version,
            "mcp.protocol.version": protocol_version,
            "oauth.client_id": oauth_client_id,
        },
    )


def record_tool_call(tool_name: str, outcome: str, duration_s: float) -> None:
    _safe_add("tool_calls", 1, {"tool.name": tool_name, "outcome": outcome})
    _safe_record(
        "tool_duration", duration_s, {"tool.name": tool_name, "outcome": outcome}
    )


def record_appwrite_call(
    *,
    service: str,
    action: str,
    classification: str,
    outcome: str,
    duration_s: float,
    error_code: Any = None,
    error_type: str | None = None,
) -> None:
    attrs = {
        "appwrite.service": service or "unknown",
        "appwrite.action": action or "unknown",
        "appwrite.classification": classification or "unknown",
        "outcome": outcome,
    }
    _safe_add("appwrite_calls", 1, attrs)
    _safe_record(
        "appwrite_call_duration",
        duration_s,
        {
            "appwrite.service": service or "unknown",
            "appwrite.action": action or "unknown",
        },
    )
    if outcome == "error":
        _safe_add(
            "appwrite_errors",
            1,
            {
                "appwrite.service": service or "unknown",
                "appwrite.action": action or "unknown",
                "error.code": str(error_code) if error_code is not None else "unknown",
                "error.type": error_type or "unknown",
            },
        )


def record_write_confirmation(classification: str, outcome: str) -> None:
    _safe_add(
        "write_confirmations",
        1,
        {"appwrite.classification": classification, "outcome": outcome},
    )


def record_search_tools(*, include_mutating: bool, match_count: int) -> None:
    _safe_add(
        "search_tools_queries",
        1,
        {"include_mutating": include_mutating, "matched": match_count > 0},
    )
    _safe_record("search_tools_results", match_count, {})


def record_search_docs(
    *, outcome: str, match_count: int, embedding_duration_s: float | None = None
) -> None:
    _safe_add(
        "search_docs_queries", 1, {"outcome": outcome, "matched": match_count > 0}
    )
    if embedding_duration_s is not None:
        _safe_record(
            "search_docs_embedding_duration", embedding_duration_s, {"outcome": outcome}
        )


def record_context_request(*, mode: str, include_services: bool) -> None:
    _safe_add(
        "context_requests", 1, {"mode": mode, "include_services": include_services}
    )


def record_auth(
    *,
    outcome: str,
    reason: str | None = None,
    duration_s: float | None = None,
    count: bool = True,
) -> None:
    if count:
        _safe_add("auth_validations", 1, {"outcome": outcome, "reason": reason})
    if duration_s is not None:
        _safe_record("auth_duration", duration_s, {"outcome": outcome})


def record_result_stored(tool_name: str) -> None:
    _safe_add("results_stored", 1, {"tool.name": tool_name})


def record_resource_read(resource_type: str) -> None:
    _safe_add("resources_reads", 1, {"resource.type": resource_type})


def record_upload(*, source: str, outcome: str, size_bytes: int | None = None) -> None:
    _safe_add("uploads", 1, {"source": source, "outcome": outcome})
    if outcome == "success" and size_bytes is not None:
        _safe_record("upload_bytes", size_bytes, {"source": source})


def record_upload_error(reason: str) -> None:
    _safe_add("upload_errors", 1, {"reason": reason})


def record_startup_validation(service: str, outcome: str) -> None:
    _safe_add("startup_validation", 1, {"service": service, "outcome": outcome})
