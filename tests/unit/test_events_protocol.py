"""MCP Events advertisement and ``events/list``, asserted on raw HTTP through the
real hosted Starlette app: the SDK's own client models drop the unknown
``events`` capability, so only the wire shows what clients actually receive."""

import asyncio
import json
import os
import unittest
from typing import Any
from unittest import mock

from mcp.server import Server
from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from mcp_server_appwrite import auth, flags
from mcp_server_appwrite.events import protocol
from mcp_server_appwrite.events.catalog import EVENTS
from mcp_server_appwrite.http_app import build_app

PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"
EXTENSION = "io.modelcontextprotocol/events"


def _request(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": {
            **(params or {}),
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }


def _message(response) -> dict[str, Any]:
    """The JSON-RPC message from a JSON or single-event SSE response."""
    text = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:] for line in text.splitlines() if line.startswith("data:")]
        text = data[-1]
    return json.loads(text)


class HostedEventsTests(unittest.TestCase):
    HEADERS = {
        "Authorization": "Bearer test-token",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    def setUp(self):
        from mcp_server_appwrite import server as server_module

        original_transport = server_module._UPLOAD_TRANSPORT
        self.addCleanup(setattr, server_module, "_UPLOAD_TRANSPORT", original_transport)

        async def verify_token(_verifier, token):
            return AccessToken(token=token, client_id="test-client", scopes=[])

        patcher = mock.patch.object(
            auth.AppwriteTokenVerifier, "verify_token", verify_token
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _flag(self, value: str | None):
        environment = dict(os.environ)
        environment.pop(flags.EVENTS.env, None)
        if value is not None:
            environment[flags.EVENTS.env] = value
        return mock.patch.dict(os.environ, environment, clear=True)

    def _post(self, client, method: str, params=None, path: str = "/"):
        headers = {
            **self.HEADERS,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        response = client.post(path, json=_request(method, params), headers=headers)
        return response.status_code, _message(response)

    def _initialize(self, client) -> dict[str, Any]:
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": LEGACY_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
        response = client.post("/mcp", json=body, headers=self.HEADERS)
        self.assertEqual(response.status_code, 200, response.text)
        return _message(response)["result"]

    def test_discover_advertises_both_capability_shapes(self):
        with self._flag("1"), TestClient(build_app()) as client:
            for path in ("/", "/mcp"):
                with self.subTest(path=path):
                    status, message = self._post(client, "server/discover", path=path)
                    self.assertEqual(status, 200, message)
                    capabilities = message["result"]["capabilities"]
                    self.assertEqual(capabilities["events"], {})
                    self.assertEqual(
                        capabilities["extensions"][EXTENSION], {"listChanged": False}
                    )
                    # The SDK-derived capabilities survive the patch.
                    self.assertIn("tools", capabilities)
                    self.assertIn("resources", capabilities)

    def test_legacy_initialize_advertises_events_too(self):
        with self._flag("1"), TestClient(build_app()) as client:
            capabilities = self._initialize(client)["capabilities"]
        self.assertEqual(capabilities["events"], {})
        self.assertEqual(capabilities["extensions"][EXTENSION], {"listChanged": False})

    def test_events_list_serves_the_catalog(self):
        with self._flag("1"), TestClient(build_app()) as client:
            status, message = self._post(client, "events/list")
        self.assertEqual(status, 200, message)
        result = message["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertNotIn("nextCursor", result)
        self.assertEqual(
            [event["name"] for event in result["events"]],
            [event.name for event in EVENTS],
        )
        for entry, event in zip(result["events"], EVENTS, strict=True):
            with self.subTest(event=event.name):
                self.assertEqual(
                    set(entry),
                    {"name", "description", "delivery", "inputSchema", "payloadSchema"},
                )
                self.assertEqual(entry["delivery"], ["webhook"])
                self.assertEqual(entry["description"], event.description)
                self.assertEqual(entry["inputSchema"], event.input_schema)
                self.assertEqual(entry["payloadSchema"], event.payload_schema)

    def test_events_list_rejects_a_cursor_it_never_issued(self):
        with self._flag("1"), TestClient(build_app()) as client:
            status, message = self._post(client, "events/list", {"cursor": "abc"})
        self.assertEqual(status, 400)
        self.assertEqual(message["error"]["code"], -32602)

    def test_subscribe_and_unsubscribe_are_not_served_yet(self):
        with self._flag("1"), TestClient(build_app()) as client:
            for method in ("events/subscribe", "events/unsubscribe"):
                with self.subTest(method=method):
                    _, message = self._post(client, method)
                    self.assertEqual(message["error"]["code"], -32601)

    def test_nothing_is_advertised_when_the_flag_is_off(self):
        for value in (None, "", "0", "false"):
            with (
                self.subTest(flag=value),
                self._flag(value),
                TestClient(build_app()) as client,
            ):
                status, message = self._post(client, "server/discover")
                self.assertEqual(status, 200, message)
                capabilities = message["result"]["capabilities"]
                self.assertNotIn("events", capabilities)
                self.assertNotIn(EXTENSION, capabilities.get("extensions") or {})
                self.assertNotIn("events", self._initialize(client)["capabilities"])

                _, message = self._post(client, "events/list")
                self.assertEqual(message["error"]["code"], -32601)


class EnabledTests(unittest.TestCase):
    def test_requires_flag_and_http_transport(self):
        cases = [
            ("http", "1", True),
            ("http", "true", True),
            ("http", "ON", True),
            ("http", "0", False),
            ("http", None, False),
            ("stdio", "1", False),
        ]
        for transport, value, expected in cases:
            environment = {} if value is None else {flags.EVENTS.env: value}
            with (
                self.subTest(transport=transport, flag=value),
                mock.patch.dict(os.environ, environment, clear=True),
            ):
                self.assertIs(protocol.enabled(transport), expected)


class CapabilityMiddlewareTests(unittest.TestCase):
    def _run(self, method: str, result: Any) -> Any:
        context = mock.Mock(method=method)

        async def call_next(_context):
            return result

        return asyncio.run(protocol.CapabilityMiddleware()(context, call_next))

    def test_keeps_existing_extensions(self):
        result = {
            "capabilities": {
                "tools": {},
                "extensions": {"io.example/other": {"enabled": True}},
            }
        }
        patched = self._run("server/discover", result)
        self.assertEqual(
            patched["capabilities"]["extensions"],
            {
                "io.example/other": {"enabled": True},
                EXTENSION: {"listChanged": False},
            },
        )
        self.assertEqual(patched["capabilities"]["tools"], {})
        # The handler's result is not mutated in place.
        self.assertNotIn("events", result["capabilities"])

    def test_other_methods_pass_through(self):
        result = {"tools": []}
        self.assertIs(self._run("tools/list", result), result)

    def test_register_wires_handler_and_middleware(self):
        server = Server("test")
        protocol.register(server)
        self.assertIsNotNone(server.get_request_handler("events/list"))
        self.assertIsNone(server.get_request_handler("events/subscribe"))
        self.assertTrue(
            any(
                isinstance(middleware, protocol.CapabilityMiddleware)
                for middleware in server.middleware
            )
        )


if __name__ == "__main__":
    unittest.main()
