import os
import unittest
from unittest.mock import patch

from appwrite.exception import AppwriteException

from mcp_server_appwrite import error_monitoring


class ErrorMonitoringTests(unittest.TestCase):
    def setUp(self):
        error_monitoring._enabled = False

    def tearDown(self):
        error_monitoring._enabled = False

    def test_init_is_noop_without_dsn(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(error_monitoring.init_error_monitoring("http", "test"))

    def test_init_is_noop_for_stdio_even_with_dsn(self):
        with patch.dict(os.environ, {"SENTRY_DSN": "https://public@example.test/1"}):
            self.assertFalse(error_monitoring.init_error_monitoring("stdio", "test"))

    def test_init_uses_standard_sentry_environment(self):
        with (
            patch.dict(
                os.environ,
                {
                    "SENTRY_DSN": "https://public@example.test/1",
                    "SENTRY_ENVIRONMENT": "staging",
                    "SENTRY_RELEASE": "custom-release",
                },
            ),
            patch("sentry_sdk.init") as init,
        ):
            self.assertTrue(error_monitoring.init_error_monitoring("http", "1.2.3"))

        kwargs = init.call_args.kwargs
        self.assertEqual(kwargs["dsn"], "https://public@example.test/1")
        self.assertEqual(kwargs["environment"], "staging")
        self.assertEqual(kwargs["release"], "custom-release")
        self.assertFalse(kwargs["send_default_pii"])
        self.assertEqual(kwargs["traces_sample_rate"], 0.0)
        self.assertEqual(kwargs["profiles_sample_rate"], 0.0)

    def test_expected_value_errors_are_not_captured(self):
        error_monitoring._enabled = True
        with patch("sentry_sdk.capture_exception") as capture:
            captured = error_monitoring.capture_exception(ValueError("bad input"))

        self.assertFalse(captured)
        capture.assert_not_called()

    def test_wrapped_value_errors_are_captured(self):
        error_monitoring._enabled = True

        class FakeScope:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        scope = FakeScope()
        with patch("sentry_sdk.capture_exception") as capture:
            with patch("sentry_sdk.new_scope", return_value=scope):
                try:
                    raise ValueError("bad input")
                except ValueError as exc:
                    wrapped = RuntimeError("wrapped")
                    wrapped.__cause__ = exc
                    captured = error_monitoring.capture_exception(wrapped)

        self.assertTrue(captured)
        capture.assert_called_once_with(wrapped)

    def test_appwrite_4xx_errors_are_not_captured(self):
        error_monitoring._enabled = True
        exc = AppwriteException("missing scope", 401, "general_unauthorized_scope")

        with patch("sentry_sdk.capture_exception") as capture:
            captured = error_monitoring.capture_appwrite_exception(
                exc,
                service="users",
                action="list",
                classification="read",
            )

        self.assertFalse(captured)
        capture.assert_not_called()

    def test_wrapped_appwrite_4xx_errors_are_not_captured(self):
        error_monitoring._enabled = True
        exc = AppwriteException("not found", 404, "user_target_not_found")

        with patch("sentry_sdk.capture_exception") as capture:
            try:
                raise RuntimeError("wrapped") from exc
            except RuntimeError as wrapped:
                captured = error_monitoring.capture_exception(wrapped)

        self.assertFalse(captured)
        capture.assert_not_called()

    def test_appwrite_5xx_errors_are_captured_once(self):
        error_monitoring._enabled = True
        exc = AppwriteException("upstream failed", 503, "general_server_error")

        with patch("sentry_sdk.capture_exception") as capture:
            self.assertTrue(
                error_monitoring.capture_appwrite_exception(
                    exc,
                    service="users",
                    action="list",
                    classification="read",
                )
            )
            try:
                raise RuntimeError("wrapped") from exc
            except RuntimeError as wrapped:
                self.assertFalse(error_monitoring.capture_exception(wrapped))

        capture.assert_called_once_with(exc)

    def test_capture_sets_tags_and_context(self):
        error_monitoring._enabled = True

        class FakeScope:
            def __init__(self):
                self.tags = {}
                self.contexts = {}
                self.transaction = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def set_tag(self, key, value):
                self.tags[key] = value

            def set_context(self, key, value):
                self.contexts[key] = value

            def set_transaction_name(self, value):
                self.transaction = value

        scope = FakeScope()
        exc = RuntimeError("boom")
        with (
            patch("sentry_sdk.new_scope", return_value=scope),
            patch("sentry_sdk.capture_exception") as capture,
        ):
            captured = error_monitoring.capture_exception(
                exc,
                tags={"mcp.method": "tools/call"},
                context={"arguments": {"api_key": "secret"}, "safe": "ok"},
                transaction="mcp.tools/call:appwrite_call_tool",
            )

        self.assertTrue(captured)
        capture.assert_called_once_with(exc)
        self.assertEqual(scope.tags["mcp.method"], "tools/call")
        self.assertEqual(scope.contexts["appwrite_mcp"]["arguments"], "[Filtered]")
        self.assertEqual(scope.contexts["appwrite_mcp"]["safe"], "ok")
        self.assertEqual(scope.transaction, "mcp.tools/call:appwrite_call_tool")

    def test_appwrite_capture_sets_project_context(self):
        error_monitoring._enabled = True

        class FakeScope:
            def __init__(self):
                self.tags = {}
                self.contexts = {}
                self.transaction = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def set_tag(self, key, value):
                self.tags[key] = value

            def set_context(self, key, value):
                self.contexts[key] = value

            def set_transaction_name(self, value):
                self.transaction = value

        scope = FakeScope()
        exc = AppwriteException("upstream failed", 503, "general_server_error")
        with (
            patch("sentry_sdk.new_scope", return_value=scope),
            patch("sentry_sdk.capture_exception") as capture,
        ):
            captured = error_monitoring.capture_appwrite_exception(
                exc,
                service="users",
                action="list",
                classification="read",
                project_id="project-1",
                organization_id="org-1",
            )

        self.assertTrue(captured)
        capture.assert_called_once_with(exc)
        self.assertEqual(scope.tags["appwrite.project_id"], "project-1")
        self.assertEqual(scope.tags["appwrite.organization_id"], "org-1")
        self.assertEqual(
            scope.contexts["appwrite_mcp"]["appwrite"]["project_id"], "project-1"
        )
        self.assertEqual(scope.transaction, "appwrite.users.list")

    def test_before_send_redacts_sensitive_fields(self):
        event = {
            "request": {
                "headers": {
                    "Authorization": "Bearer secret",
                    "Content-Type": "application/json",
                    "X-Appwrite-Key": "appwrite-secret",
                },
                "data": {"token": "secret"},
            },
            "extra": {"password": "secret", "safe": "ok"},
        }

        scrubbed = error_monitoring._before_send(event, {})

        self.assertEqual(scrubbed["request"]["headers"]["Authorization"], "[Filtered]")
        self.assertEqual(scrubbed["request"]["headers"]["X-Appwrite-Key"], "[Filtered]")
        self.assertEqual(
            scrubbed["request"]["headers"]["Content-Type"], "application/json"
        )
        self.assertEqual(scrubbed["request"]["data"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["password"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["safe"], "ok")

    def test_before_send_drops_expected_exception_chains(self):
        appwrite_error = AppwriteException("not found", 404, "not_found")
        wrapped = RuntimeError("wrapped")
        wrapped.__cause__ = appwrite_error

        self.assertIsNone(
            error_monitoring._before_send(
                {"event_id": "1"}, {"exc_info": (RuntimeError, wrapped, None)}
            )
        )

    def test_before_send_normalizes_mcp_tool_call_transaction(self):
        event = {
            "transaction": "http://10.140.28.8:8000/mcp",
            "tags": {
                "mcp.method": "tools/call",
                "tool.name": "appwrite_call_tool",
            },
        }

        scrubbed = error_monitoring._before_send(event, {})

        self.assertEqual(scrubbed["transaction"], "mcp.tools/call:appwrite_call_tool")

    def test_before_send_normalizes_mcp_resource_transaction_from_list_tags(self):
        event = {
            "transaction": "http://10.140.28.8:8000/mcp",
            "tags": [
                ["mcp.method", "resources/read"],
                ["resource.type", "result"],
            ],
        }

        scrubbed = error_monitoring._before_send(event, {})

        self.assertEqual(scrubbed["transaction"], "mcp.resources/read:result")

    def test_before_send_keeps_explicit_semantic_transaction(self):
        event = {
            "transaction": "appwrite.users.list",
            "tags": {"mcp.method": "tools/call", "tool.name": "appwrite_call_tool"},
        }

        scrubbed = error_monitoring._before_send(event, {})

        self.assertEqual(scrubbed["transaction"], "appwrite.users.list")


if __name__ == "__main__":
    unittest.main()
