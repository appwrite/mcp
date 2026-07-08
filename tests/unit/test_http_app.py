import asyncio
import json
import logging
import os
import unittest
from unittest import mock

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from mcp_server_appwrite import auth, telemetry
from mcp_server_appwrite.http_app import (
    HealthzAccessLogFilter,
    MCPIdentityMiddleware,
    _client_from_user_agent,
    _find_initialize_params,
    _send_401,
)


class HealthzAccessLogFilterTests(unittest.TestCase):
    def setUp(self):
        self.filter = HealthzAccessLogFilter()

    def _record(self, args):
        return logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=args,
            exc_info=None,
        )

    def test_filters_healthz_access_logs(self):
        record = self._record(("127.0.0.1:12345", "GET", "/healthz", "1.1", 200))

        self.assertFalse(self.filter.filter(record))

    def test_filters_healthz_access_logs_with_query_string(self):
        record = self._record(
            ("127.0.0.1:12345", "GET", "/healthz?ready=1", "1.1", 200)
        )

        self.assertFalse(self.filter.filter(record))

    def test_keeps_non_healthz_access_logs(self):
        record = self._record(("127.0.0.1:12345", "GET", "/mcp", "1.1", 401))

        self.assertTrue(self.filter.filter(record))


class Send401Tests(unittest.TestCase):
    ENV = {
        "APPWRITE_ENDPOINT": "https://cloud.appwrite.io/v1",
        "MCP_PUBLIC_URL": "https://mcp.appwrite.io",
        "APPWRITE_PROJECT_ID": "console",
    }

    def setUp(self):
        patcher = mock.patch.dict(os.environ, self.ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        auth._deprecated_scope_cache.clear()
        self.addCleanup(auth._deprecated_scope_cache.clear)

    def _challenge(self) -> str:
        messages = []

        async def send(message):
            messages.append(message)

        asyncio.run(_send_401(send))
        start = messages[0]
        self.assertEqual(start["status"], 401)
        headers = dict(start["headers"])
        return headers[b"www-authenticate"].decode()

    def _empty_deprecated_scope_catalog(self):
        async def no_deprecated_scopes(_client, _kind):
            return set()

        return mock.patch.object(auth, "_load_deprecated_scopes", no_deprecated_scopes)

    def test_401_includes_scope_hint_from_discovery(self):
        # SEP-835: the challenge's `scope` parameter mirrors the advertised
        # catalog so clients know exactly what to request.
        pid = "console"
        auth._store_discovery(
            pid,
            {
                "issuer": "https://cloud.appwrite.io/v1/oauth2/console",
                "jwks_uri": "https://cloud.appwrite.io/v1/oauth2/console/jwks",
                "scopes_supported": ["openid", "all", "project:users.read"],
            },
        )
        try:
            with self._empty_deprecated_scope_catalog():
                challenge = self._challenge()
        finally:
            auth._discovery_cache.pop(pid, None)
        self.assertIn('resource_metadata="', challenge)
        self.assertIn('scope="openid all project:users.read"', challenge)

    def test_401_scope_hint_drops_tokens_outside_rfc6749_grammar(self):
        # The hint lands inside a quoted-string header value; a malicious or
        # misconfigured authorization server must not be able to inject headers
        # (CRLF) or break out of the quoted string (double quote, backslash).
        pid = "console"
        auth._store_discovery(
            pid,
            {
                "issuer": "https://cloud.appwrite.io/v1/oauth2/console",
                "jwks_uri": "https://cloud.appwrite.io/v1/oauth2/console/jwks",
                "scopes_supported": [
                    "openid",
                    'evil"',
                    "back\\slash",
                    "crlf\r\nSet-Cookie: pwned=1",
                    "tab\tseparated",
                    "spa ce",
                    "",
                    42,
                    "project:users.read",
                ],
            },
        )
        try:
            with self._empty_deprecated_scope_catalog():
                challenge = self._challenge()
        finally:
            auth._discovery_cache.pop(pid, None)
        self.assertIn('scope="openid project:users.read"', challenge)
        self.assertNotIn("Set-Cookie", challenge)
        self.assertNotIn("\r", challenge)
        self.assertNotIn("\n", challenge)
        self.assertNotIn("evil", challenge)
        self.assertNotIn("back", challenge)

    def test_401_omits_scope_hint_when_no_valid_scope_tokens_remain(self):
        pid = "console"
        auth._store_discovery(
            pid,
            {
                "issuer": "https://cloud.appwrite.io/v1/oauth2/console",
                "jwks_uri": "https://cloud.appwrite.io/v1/oauth2/console/jwks",
                "scopes_supported": ['bad"scope', "crlf\r\n"],
            },
        )
        try:
            challenge = self._challenge()
        finally:
            auth._discovery_cache.pop(pid, None)
        self.assertIn('resource_metadata="', challenge)
        self.assertNotIn("scope=", challenge)

    def test_401_omits_scope_hint_when_discovery_unavailable(self):
        # An unauthenticated 401 must never fail because discovery is down.
        with mock.patch.dict(
            os.environ,
            {
                "APPWRITE_ENDPOINT": "http://127.0.0.1:1/v1",
                "APPWRITE_PROJECT_ID": "unreachableproj",
            },
        ):
            challenge = self._challenge()
        self.assertIn('error="invalid_token"', challenge)
        self.assertIn('resource_metadata="', challenge)
        self.assertNotIn("scope=", challenge)


class ClientFromUserAgentTests(unittest.TestCase):
    def test_product_token(self):
        self.assertEqual(
            _client_from_user_agent("claude-code/2.0.1 (darwin)"), "claude-code"
        )
        self.assertEqual(_client_from_user_agent("Cursor/1.2"), "cursor")

    def test_missing_or_garbage(self):
        self.assertIsNone(_client_from_user_agent(None))
        self.assertIsNone(_client_from_user_agent(""))
        self.assertIsNone(_client_from_user_agent("!!"))


class FindInitializeParamsTests(unittest.TestCase):
    def test_single_initialize(self):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "clientInfo": {"name": "claude-code", "version": "2.0"},
                },
            }
        ).encode()
        params = _find_initialize_params(body)
        assert params is not None
        self.assertEqual(params["clientInfo"]["name"], "claude-code")

    def test_batch_with_initialize(self):
        body = json.dumps(
            [
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            ]
        ).encode()
        self.assertEqual(_find_initialize_params(body), {})

    def test_non_initialize_and_invalid(self):
        self.assertIsNone(
            _find_initialize_params(b'{"jsonrpc":"2.0","method":"tools/call"}')
        )
        self.assertIsNone(_find_initialize_params(b"not json"))


class MCPIdentityMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[self.reader])
        telemetry._instruments.clear()
        telemetry._build_instruments(provider.get_meter("test"), "http", "test")
        telemetry._enabled = True
        self.seen_client: list[str] = []

    def tearDown(self):
        telemetry._enabled = False
        telemetry._instruments.clear()
        with telemetry._active_lock:
            telemetry._active_users.clear()
            telemetry._active_sessions.clear()
            telemetry._active_versions.clear()

    def _run(self, body: bytes, headers: list[tuple[bytes, bytes]]):
        downstream_bodies: list[bytes] = []

        async def app(scope, receive, send):
            self.seen_client.append(telemetry.current_client_id())
            message = await receive()
            downstream_bodies.append(message.get("body", b""))

        scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers}
        received = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive():
            return received.pop(0)

        async def send(message):
            pass

        asyncio.run(MCPIdentityMiddleware(app)(scope, receive, send))
        return downstream_bodies

    def _points(self, metric_name: str) -> list:
        data = self.reader.get_metrics_data()
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    if metric.name == metric_name:
                        return list(metric.data.data_points)
        return []

    def test_initialize_records_connection_and_replays_body(self):
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "clientInfo": {"name": "claude-code", "version": "2.0"},
                },
            }
        ).encode()
        replayed = self._run(body, [(b"user-agent", b"claude-code/2.0")])
        self.assertEqual(replayed, [body])
        connections = self._points("mcp.connection")
        self.assertEqual(len(connections), 1)
        self.assertEqual(connections[0].attributes.get("client_id"), "claude-code")
        handshakes = self._points("mcp.handshake")
        self.assertEqual(handshakes[0].attributes.get("status"), "success")

    def test_tool_call_binds_identity_from_headers(self):
        body = b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{}}'
        self._run(
            body,
            [
                (b"user-agent", b"cursor/1.5 (linux)"),
                (b"mcp-protocol-version", b"2025-06-18"),
            ],
        )
        self.assertEqual(self.seen_client, ["cursor"])
        # No connection counted for non-initialize requests.
        self.assertEqual(self._points("mcp.connection"), [])


if __name__ == "__main__":
    unittest.main()
