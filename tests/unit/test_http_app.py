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
    RequireBearer,
    _client_from_user_agent,
    _find_initialize_params,
    _send_401,
    authorization_server_metadata_endpoint,
    build_app,
    mcp_path_protected_resource_metadata_endpoint,
    protected_resource_metadata_endpoint,
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
        auth._store_discovery(
            "console",
            {
                "issuer": "https://cloud.appwrite.io/v1/oauth2/console",
                "jwks_uri": "https://cloud.appwrite.io/v1/oauth2/console/jwks",
                "scopes_supported": [],
            },
        )
        self.addCleanup(lambda: auth._discovery_cache.pop("console", None))

    def _challenge(self, resource: str | None = None) -> str:
        messages = []

        async def send(message):
            messages.append(message)

        asyncio.run(_send_401(send, resource))
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

    def test_401_uses_metadata_for_requested_resource(self):
        with self._empty_deprecated_scope_catalog():
            root_challenge = self._challenge(auth.canonical_resource())
            mcp_path_challenge = self._challenge(auth.mcp_path_resource())

        self.assertIn(
            'resource_metadata="https://mcp.appwrite.io/'
            '.well-known/oauth-protected-resource"',
            root_challenge,
        )
        self.assertIn(
            'resource_metadata="https://mcp.appwrite.io/'
            '.well-known/oauth-protected-resource/mcp"',
            mcp_path_challenge,
        )

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


class RequireBearerTests(unittest.TestCase):
    def _challenge_for_path(self, path: str) -> str:
        messages = []

        async def app(scope, receive, send):  # pragma: no cover - must stay gated
            self.fail("unauthenticated request reached the MCP handler")

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        async def metadata(*, resource=None):
            return {"resource": resource, "scopes_supported": []}

        scope = {"type": "http", "method": "GET", "path": path, "headers": []}
        with (
            mock.patch.dict(
                os.environ, {"MCP_PUBLIC_URL": "http://localhost:8000"}, clear=False
            ),
            mock.patch(
                "mcp_server_appwrite.http_app.protected_resource_metadata", metadata
            ),
        ):
            asyncio.run(RequireBearer(app)(scope, receive, send))

        self.assertEqual(messages[0]["status"], 401)
        return dict(messages[0]["headers"])[b"www-authenticate"].decode()

    def test_challenge_matches_requested_endpoint(self):
        root_challenge = self._challenge_for_path("/")
        mcp_path_challenge = self._challenge_for_path("/mcp")

        self.assertIn(
            'resource_metadata="http://localhost:8000/'
            '.well-known/oauth-protected-resource"',
            root_challenge,
        )
        self.assertIn(
            'resource_metadata="http://localhost:8000/'
            '.well-known/oauth-protected-resource/mcp"',
            mcp_path_challenge,
        )


class WellKnownMetadataEndpointTests(unittest.TestCase):
    """Discovery endpoints, including the legacy shims for clients on the
    2025-03-26 MCP authorization spec (e.g. Raycast) that fetch
    ``/.well-known/oauth-authorization-server`` from the MCP origin instead of
    following ``resource_metadata`` from the 401 challenge."""

    ENV = {
        "APPWRITE_ENDPOINT": "https://cloud.appwrite.io/v1",
        "MCP_PUBLIC_URL": "https://mcp.appwrite.io",
        "APPWRITE_PROJECT_ID": "console",
    }
    DISCOVERY = {
        "issuer": "https://cloud.appwrite.io/v1/oauth2/console",
        "authorization_endpoint": "https://cloud.appwrite.io/v1/oauth2/console/authorize",
        "token_endpoint": "https://cloud.appwrite.io/v1/oauth2/console/token",
        "registration_endpoint": "https://cloud.appwrite.io/v1/oauth2/console/register",
        "jwks_uri": "https://cloud.appwrite.io/v1/oauth2/console/.well-known/jwks.json",
        "scopes_supported": ["openid", "all"],
    }

    def setUp(self):
        patcher = mock.patch.dict(os.environ, self.ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        auth._store_discovery("console", dict(self.DISCOVERY))
        self.addCleanup(lambda: auth._discovery_cache.pop("console", None))

    def _body(self, response) -> dict:
        return json.loads(response.body)

    def test_authorization_server_metadata_mirrors_discovery(self):
        response = asyncio.run(authorization_server_metadata_endpoint(mock.Mock()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._body(response), self.DISCOVERY)
        self.assertIn("access-control-allow-origin", response.headers)

    def test_protected_resource_metadata_names_canonical_resource(self):
        response = asyncio.run(protected_resource_metadata_endpoint(mock.Mock()))
        self.assertEqual(response.status_code, 200)
        body = self._body(response)
        self.assertEqual(body["resource"], "https://mcp.appwrite.io/")
        self.assertEqual(body["authorization_servers"], [self.DISCOVERY["issuer"]])

    def test_mcp_path_metadata_names_matching_resource(self):
        response = asyncio.run(
            mcp_path_protected_resource_metadata_endpoint(mock.Mock())
        )
        self.assertEqual(response.status_code, 200)
        body = self._body(response)
        self.assertEqual(body["resource"], "https://mcp.appwrite.io/mcp")
        self.assertEqual(body["authorization_servers"], [self.DISCOVERY["issuer"]])

    def test_app_routes_include_legacy_discovery_paths(self):
        # build_mcp_server flips the module-global upload transport to "http";
        # restore it so stdio-transport tests elsewhere keep seeing the default.
        from mcp_server_appwrite import server as server_module

        original_transport = server_module._UPLOAD_TRANSPORT
        self.addCleanup(setattr, server_module, "_UPLOAD_TRANSPORT", original_transport)
        paths = {getattr(route, "path", None) for route in build_app().routes}
        self.assertIn("/", paths)
        self.assertIn("/mcp", paths)
        self.assertIn("/.well-known/oauth-protected-resource/mcp", paths)
        self.assertIn("/.well-known/oauth-protected-resource", paths)
        self.assertIn("/.well-known/oauth-authorization-server", paths)


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

    def test_initialize_records_handshake_and_replays_body(self):
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
        handshakes = self._points("mcp.handshake")
        self.assertEqual(len(handshakes), 1)
        self.assertEqual(handshakes[0].attributes.get("client_id"), "claude-code")
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
        # No handshake counted for non-initialize requests.
        self.assertEqual(self._points("mcp.handshake"), [])


if __name__ == "__main__":
    unittest.main()
