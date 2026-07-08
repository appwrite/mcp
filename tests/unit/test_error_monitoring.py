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

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def set_tag(self, key, value):
                self.tags[key] = value

            def set_context(self, key, value):
                self.contexts[key] = value

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
            )

        self.assertTrue(captured)
        capture.assert_called_once_with(exc)
        self.assertEqual(scope.tags["mcp.method"], "tools/call")
        self.assertEqual(scope.contexts["appwrite_mcp"]["arguments"], "[Filtered]")
        self.assertEqual(scope.contexts["appwrite_mcp"]["safe"], "ok")

    def test_before_send_redacts_sensitive_fields(self):
        event = {
            "request": {
                "headers": {
                    "Authorization": "Bearer secret",
                    "Content-Type": "application/json",
                },
                "data": {"token": "secret"},
            },
            "extra": {"password": "secret", "safe": "ok"},
        }

        scrubbed = error_monitoring._before_send(event, {})

        self.assertEqual(scrubbed["request"]["headers"]["Authorization"], "[Filtered]")
        self.assertEqual(
            scrubbed["request"]["headers"]["Content-Type"], "application/json"
        )
        self.assertEqual(scrubbed["request"]["data"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["password"], "[Filtered]")
        self.assertEqual(scrubbed["extra"]["safe"], "ok")


if __name__ == "__main__":
    unittest.main()
