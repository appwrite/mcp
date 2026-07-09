import time
import unittest

import mcp.types as types
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from mcp_server_appwrite import telemetry
from mcp_server_appwrite.constants import ACTIVE_WINDOW_SECONDS
from mcp_server_appwrite.operator import Operator
from mcp_server_appwrite.tool_manager import ToolManager


def make_tool(
    name: str, description: str, required: list[str] | None = None
) -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": {"parameter": {"type": "string"}},
            "required": required or [],
        },
    )


class TelemetryHarness(unittest.TestCase):
    """Base class that wires telemetry to an in-memory reader for assertions."""

    def setUp(self) -> None:
        self.reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[self.reader])
        meter = provider.get_meter("test")
        telemetry._instruments.clear()
        self._clear_stores()
        telemetry._build_instruments(meter, "http", "test")
        telemetry._enabled = True

    def tearDown(self) -> None:
        telemetry._enabled = False
        telemetry._instruments.clear()
        self._clear_stores()

    def _clear_stores(self) -> None:
        with telemetry._active_lock:
            telemetry._active_users.clear()
            telemetry._active_sessions.clear()
            telemetry._active_versions.clear()
            telemetry._seen_sessions.clear()

    def points(self, metric_name: str) -> list:
        data = self.reader.get_metrics_data()
        if data is None:
            return []
        for resource_metrics in data.resource_metrics:
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    if metric.name == metric_name:
                        return list(metric.data.data_points)
        return []

    def assertAttr(self, point, key, value):
        self.assertEqual(point.attributes.get(key), value)

    def connect(self, session_id=1, client="claude-code", subject="user-a"):
        telemetry.record_connection(
            session_id=session_id,
            client_name=client,
            protocol_version="2025-06-18",
            subject=subject,
        )


class SessionTests(TelemetryHarness):
    def test_active_sessions_counts_distinct_subjects(self):
        self.connect(session_id=1, subject="user-a")
        self.connect(session_id=2, subject="user-b")
        sessions = self.points("mcp.active_sessions")
        self.assertEqual(sessions[0].value, 2)
        handshakes = self.points("mcp.handshake")
        self.assertEqual(sum(p.value for p in handshakes), 2)
        self.assertAttr(handshakes[0], "status", "success")
        self.assertAttr(handshakes[0], "client_id", "claude-code")

    def test_active_sessions_by_client_and_protocol_version(self):
        self.connect(session_id=1, client="claude-code", subject="user-a")
        self.connect(session_id=2, client="cursor", subject="user-b")
        by_client = self.points("mcp.active_sessions.by_client")
        counts = {p.attributes["client_id"]: p.value for p in by_client}
        self.assertEqual(counts, {"claude-code": 1, "cursor": 1})
        versions = self.points("mcp.protocol.version.count")
        self.assertEqual(sum(p.value for p in versions), 2)
        self.assertAttr(versions[0], "version", "2025-06-18")

    def test_handshake_deduped_per_session(self):
        for _ in range(3):
            self.connect(session_id=42, client="cursor", subject="user-c")
        handshakes = self.points("mcp.handshake")
        self.assertEqual(sum(p.value for p in handshakes), 1)

    def test_expired_session_records_duration_and_idle_disconnect(self):
        self.connect(session_id=1, client="cursor", subject="user-a")
        with telemetry._active_lock:
            first_seen, _expiry = telemetry._active_sessions[("cursor", "user-a")]
            telemetry._active_sessions[("cursor", "user-a")] = [
                first_seen - 60,
                time.monotonic() - 1,
            ]
        by_client = self.points("mcp.active_sessions.by_client")
        self.assertEqual(by_client, [])
        durations = self.points("mcp.session.duration")
        self.assertEqual(durations[0].count, 1)
        disconnects = self.points("mcp.session.disconnects")
        self.assertAttr(disconnects[0], "reason", "idle")
        self.assertAttr(disconnects[0], "client_id", "cursor")

    def test_handshake_failure(self):
        telemetry.record_handshake_failure(reason="invalid_token")
        handshakes = self.points("mcp.handshake")
        self.assertEqual(len(handshakes), 1)
        self.assertAttr(handshakes[0], "status", "failure")

    def test_client_names_are_case_normalized(self):
        # clientInfo.name arrives raw from the initialize request while
        # User-Agent-derived names are lowercased, so the same client must not
        # split into two client_id values ("Trae" vs "trae").
        self.connect(session_id=1, client="Trae", subject="user-a")
        telemetry.set_request_identity(client_name="trae", subject="user-a")
        by_client = self.points("mcp.active_sessions.by_client")
        counts = {p.attributes["client_id"]: p.value for p in by_client}
        self.assertEqual(counts, {"trae": 1})
        handshakes = self.points("mcp.handshake")
        self.assertAttr(handshakes[0], "client_id", "trae")

    def test_client_name_normalization_shapes(self):
        cases = {
            "Trae": "trae",
            "  Claude Code  ": "claude-code",
            "cursor": "cursor",
            "": None,
            None: None,
            "   ": None,
            "X" * 100: "x" * 64,
        }
        for raw, expected in cases.items():
            self.assertEqual(
                telemetry._normalize_client_name(raw), expected, msg=repr(raw)
            )


class MessageTests(TelemetryHarness):
    def test_message_success(self):
        self.connect()
        telemetry.record_message("tools/call", "success", 0.01)
        messages = self.points("mcp.messages.received")
        self.assertEqual(len(messages), 1)
        self.assertAttr(messages[0], "msg_type", "tools/call")
        self.assertAttr(messages[0], "client_id", "claude-code")
        latency = self.points("mcp.message.latency")
        self.assertEqual(latency[0].count, 1)
        self.assertEqual(self.points("mcp.jsonrpc.errors"), [])

    def test_message_error_emits_jsonrpc_error(self):
        telemetry.record_message(
            "tools/call",
            "error",
            0.01,
            error_code=-32602,
            error_message="ValueError",
        )
        errors = self.points("mcp.jsonrpc.errors")
        self.assertEqual(len(errors), 1)
        self.assertAttr(errors[0], "error_code", "-32602")
        self.assertAttr(errors[0], "error_message", "ValueError")

    def test_message_size_by_direction(self):
        telemetry.record_message_size("received", 128)
        telemetry.record_message_size("sent", 4096)
        sizes = self.points("mcp.message.size")
        directions = {p.attributes["direction"]: p.sum for p in sizes}
        self.assertEqual(directions, {"received": 128, "sent": 4096})


class ToolExecutionTests(TelemetryHarness):
    def test_tool_call_success_with_sizes_and_tokens(self):
        self.connect()
        telemetry.tool_call_started("appwrite_call_tool")
        telemetry.record_tool_call(
            "appwrite_call_tool",
            "success",
            0.2,
            input_chars=400,
            output_chars=2000,
        )
        calls = self.points("mcp.tool.calls")
        self.assertEqual(len(calls), 1)
        self.assertAttr(calls[0], "tool_name", "appwrite_call_tool")
        self.assertAttr(calls[0], "client_id", "claude-code")
        self.assertAttr(calls[0], "status", "success")
        inflight = self.points("mcp.tool.inflight")
        self.assertEqual(inflight[0].value, 0)
        inflight_total = self.points("mcp.tool.inflight.total")
        self.assertEqual(inflight_total[0].value, 0)
        result_size = self.points("mcp.tool.result.size")
        self.assertEqual(result_size[0].sum, 2000)
        tokens = {
            p.attributes["direction"]: p.value for p in self.points("mcp.token.usage")
        }
        self.assertEqual(tokens, {"input": 100, "output": 500})
        per_call = self.points("mcp.token.usage.per.call")
        self.assertEqual(per_call[0].sum, 600)
        self.assertEqual(self.points("mcp.tool.errors"), [])

    def test_tool_call_error_emits_error_type(self):
        telemetry.tool_call_started("appwrite_search_tools")
        telemetry.record_tool_call(
            "appwrite_search_tools", "error", 0.05, error_type="ValueError"
        )
        errors = self.points("mcp.tool.errors")
        self.assertEqual(len(errors), 1)
        self.assertAttr(errors[0], "tool_name", "appwrite_search_tools")
        self.assertAttr(errors[0], "error_type", "ValueError")

    def test_hallucination_sanitizes_tool_name(self):
        self.connect()
        telemetry.record_hallucination("no such tool!{}" + "x" * 100)
        points = self.points("mcp.tool.hallucination")
        self.assertEqual(len(points), 1)
        attempted = points[0].attributes["attempted_tool"]
        self.assertLessEqual(len(attempted), 64)
        self.assertNotIn(" ", attempted)
        self.assertNotIn("!", attempted)


class SystemGaugeTests(TelemetryHarness):
    def test_system_gauges_registered(self):
        # CPU needs two observations for a delta; memory should report on the first.
        self.reader.get_metrics_data()
        cpu = self.points("mcp.cpu.usage.percent")
        self.assertEqual(len(cpu), 1)
        self.assertGreaterEqual(cpu[0].value, 0)
        memory = self.points("mcp.memory.usage.mb")
        self.assertEqual(len(memory), 1)
        self.assertGreater(memory[0].value, 0)


class OperatorTelemetryTests(TelemetryHarness):
    def make_runtime(self, executor):
        manager = ToolManager()
        manager.tools_registry = {
            "tables_db_list": {
                "definition": make_tool("tables_db_list", "List all databases."),
                "function": object(),
                "parameter_types": {},
            },
            "tables_db_create": {
                "definition": make_tool(
                    "tables_db_create", "Create a database.", ["database_id"]
                ),
                "function": object(),
                "parameter_types": {},
            },
        }
        return Operator(manager, executor)

    def test_blocked_write_counts_tool_error(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])
        with self.assertRaises(RuntimeError):
            runtime.execute_public_tool(
                "appwrite_call_tool",
                {"tool_name": "tables_db_create", "arguments": {"database_id": "db"}},
            )
        errors = self.points("mcp.tool.errors")
        self.assertEqual(len(errors), 1)
        self.assertAttr(errors[0], "error_type", "RuntimeError")

    def test_tool_call_counter(self):
        runtime = self.make_runtime(
            lambda name, arguments, *_: [types.TextContent(type="text", text="ok")]
        )
        runtime.execute_public_tool(
            "appwrite_call_tool", {"tool_name": "tables_db_list"}
        )
        calls = self.points("mcp.tool.calls")
        self.assertTrue(
            any(
                p.attributes.get("tool_name") == "appwrite_call_tool"
                and p.attributes.get("status") == "success"
                for p in calls
            )
        )

    def test_unknown_hidden_tool_counts_hallucination(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])
        with self.assertRaises(ValueError):
            runtime.execute_public_tool(
                "appwrite_call_tool", {"tool_name": "made_up_tool"}
            )
        points = self.points("mcp.tool.hallucination")
        self.assertEqual(len(points), 1)
        self.assertAttr(points[0], "attempted_tool", "made_up_tool")


class NoOpTests(unittest.TestCase):
    def test_no_emission_when_disabled(self):
        # Disabled with instruments registered on a live reader: record_* must be a
        # no-op so the reader collects no MCP counters/histograms.
        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        telemetry._instruments.clear()
        telemetry._build_instruments(provider.get_meter("test"), "http", "test")
        telemetry._enabled = False
        try:
            telemetry.record_message("tools/call", "success", 0.01)
            telemetry.record_tool_call("appwrite_call_tool", "success", 0.01)
            telemetry.record_handshake_failure(reason="invalid_token")

            recorded = {
                metric.name
                for rm in reader.get_metrics_data().resource_metrics
                for sm in rm.scope_metrics
                for metric in sm.metrics
                if metric.data.data_points
            }
            self.assertNotIn("mcp.messages.received", recorded)
            self.assertNotIn("mcp.tool.calls", recorded)
            self.assertNotIn("mcp.handshake", recorded)
        finally:
            telemetry._instruments.clear()

    def test_init_is_noop_for_stdio(self):
        self.assertFalse(telemetry.init_telemetry("stdio", "test"))

    def test_record_connection_does_not_grow_stores_when_disabled(self):
        # When disabled, the rolling activity stores must not accumulate — they are
        # only pruned by the gauge callbacks, which never run while disabled.
        telemetry._enabled = False
        with telemetry._active_lock:
            telemetry._active_users.clear()
            telemetry._active_sessions.clear()
            telemetry._active_versions.clear()
        telemetry.record_connection(
            session_id=7,
            client_name="claude",
            protocol_version="2025-06-18",
            subject="user-x",
        )
        self.assertEqual(len(telemetry._active_users), 0)
        self.assertEqual(len(telemetry._active_sessions), 0)
        self.assertEqual(len(telemetry._active_versions), 0)

    def test_session_window_constant_positive(self):
        self.assertGreater(ACTIVE_WINDOW_SECONDS, 0)


if __name__ == "__main__":
    unittest.main()
