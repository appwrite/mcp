import unittest

import mcp.types as types
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from mcp_server_appwrite import telemetry
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
        telemetry._build_instruments(meter, "http", "test")
        telemetry._enabled = True

    def tearDown(self) -> None:
        telemetry._enabled = False
        telemetry._instruments.clear()
        with telemetry._active_lock:
            telemetry._active_users.clear()
            telemetry._active_clients.clear()
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


class RecordHelperTests(TelemetryHarness):
    def test_appwrite_call_success_labels(self):
        telemetry.record_appwrite_call(
            service="storage",
            action="create",
            classification="write",
            outcome="success",
            duration_s=0.05,
        )
        points = self.points("mcp.appwrite.calls")
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0].value, 1)
        self.assertAttr(points[0], "appwrite.service", "storage")
        self.assertAttr(points[0], "appwrite.action", "create")
        self.assertAttr(points[0], "appwrite.classification", "write")
        self.assertAttr(points[0], "outcome", "success")
        # No error counter on success.
        self.assertEqual(self.points("mcp.appwrite.errors"), [])

    def test_appwrite_call_error_emits_error_counter(self):
        telemetry.record_appwrite_call(
            service="users",
            action="get",
            classification="read",
            outcome="error",
            duration_s=0.01,
            error_code=404,
            error_type="user_not_found",
        )
        errors = self.points("mcp.appwrite.errors")
        self.assertEqual(len(errors), 1)
        self.assertAttr(errors[0], "appwrite.service", "users")
        self.assertAttr(errors[0], "error.code", "404")
        self.assertAttr(errors[0], "error.type", "user_not_found")

    def test_auth_rejected_then_duration_without_double_count(self):
        # _verify_sync-style: counter with reason, no duration.
        telemetry.record_auth(outcome="rejected", reason="signature")
        # verify_token-style: duration only, no counter.
        telemetry.record_auth(outcome="rejected", duration_s=0.02, count=False)
        validations = self.points("mcp.auth.validations")
        self.assertEqual(len(validations), 1)
        self.assertEqual(validations[0].value, 1)
        self.assertAttr(validations[0], "reason", "signature")

    def test_active_users_gauge_counts_distinct_subjects(self):
        telemetry.record_initialize(
            session_id=1,
            client_name="claude",
            client_version="1.0",
            protocol_version="2025-06-18",
            oauth_client_id="app-1",
            subject="user-a",
        )
        telemetry.record_initialize(
            session_id=2,
            client_name="claude",
            client_version="1.0",
            protocol_version="2025-06-18",
            oauth_client_id="app-1",
            subject="user-b",
        )
        users = self.points("mcp.users.active")
        self.assertEqual(users[0].value, 2)
        inits = self.points("mcp.initializations")
        self.assertEqual(sum(p.value for p in inits), 2)
        self.assertAttr(inits[0], "client.name", "claude")

    def test_initialize_deduped_per_session(self):
        for _ in range(3):
            telemetry.record_initialize(
                session_id=42,
                client_name="cursor",
                client_version="2.0",
                protocol_version="2025-06-18",
                oauth_client_id="app-2",
                subject="user-c",
            )
        inits = self.points("mcp.initializations")
        self.assertEqual(sum(p.value for p in inits), 1)


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

    def test_write_confirmation_blocked(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])
        with self.assertRaises(RuntimeError):
            runtime.execute_public_tool(
                "appwrite_call_tool",
                {"tool_name": "tables_db_create", "arguments": {"database_id": "db"}},
            )
        confirmations = self.points("mcp.write.confirmations")
        self.assertEqual(len(confirmations), 1)
        self.assertAttr(confirmations[0], "outcome", "blocked")
        self.assertAttr(confirmations[0], "appwrite.classification", "write")

    def test_write_confirmation_confirmed(self):
        runtime = self.make_runtime(
            lambda name, arguments, *_: [types.TextContent(type="text", text="ok")]
        )
        runtime.execute_public_tool(
            "appwrite_call_tool",
            {
                "tool_name": "tables_db_create",
                "confirm_write": True,
                "database_id": "db",
            },
        )
        confirmed = [
            p
            for p in self.points("mcp.write.confirmations")
            if p.attributes.get("outcome") == "confirmed"
        ]
        self.assertEqual(len(confirmed), 1)

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
                p.attributes.get("tool.name") == "appwrite_call_tool"
                and p.attributes.get("outcome") == "success"
                for p in calls
            )
        )


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
            telemetry.record_request("tools/call", "success", 0.01)
            telemetry.record_appwrite_call(
                service="storage",
                action="create",
                classification="write",
                outcome="success",
                duration_s=0.01,
            )
            telemetry.record_auth(outcome="rejected", reason="malformed")

            recorded = {
                metric.name
                for rm in reader.get_metrics_data().resource_metrics
                for sm in rm.scope_metrics
                for metric in sm.metrics
                if metric.data.data_points
            }
            self.assertNotIn("mcp.requests", recorded)
            self.assertNotIn("mcp.appwrite.calls", recorded)
            self.assertNotIn("mcp.auth.validations", recorded)
        finally:
            telemetry._instruments.clear()

    def test_init_is_noop_for_stdio(self):
        self.assertFalse(telemetry.init_telemetry("stdio", "test"))


if __name__ == "__main__":
    unittest.main()
