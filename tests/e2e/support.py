"""End-to-end harness: the real hosted server, driven over real HTTP.

:class:`Server` boots the production Starlette app (``http_app.build_app``)
under uvicorn on a random localhost port, in a background thread of the test
process. :class:`Client` talks to it the way ChatGPT does: raw JSON-RPC over
Streamable HTTP with ``MCP-Protocol-Version: 2026-07-28``, ``Mcp-Method`` and
the request ``_meta``, asserting on the wire rather than on SDK models (the SDK
client drops capabilities it does not model, such as ``events``).

The one stub is the OAuth token verifier: Cloud OAuth is not reachable from CI,
so a fixed bearer token is accepted in place of an Appwrite access token, the
same seam the unit tests of ``http_app`` use. Everything behind it (routing,
auth middleware, the MCP session manager, the low-level server and its
middleware) is the production code path.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Callable, Mapping
from types import TracebackType
from typing import Any
from unittest import mock

import httpx
import uvicorn
from mcp.server.auth.provider import AccessToken

from mcp_server_appwrite import auth
from mcp_server_appwrite.http_app import build_app

TOKEN = "e2e-access-token"
"""The bearer token the stubbed verifier accepts."""

CLIENT_ID = "e2e-client"

PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"

PUBLIC_URL = "https://mcp.e2e.test"
"""``MCP_PUBLIC_URL`` of every server: the public origin a load balancer would
serve, deliberately not the address the server listens on."""

STARTUP_SECONDS = 15.0

CONTROLLED = (
    "MCP_EVENTS",
    "MCP_EVENTS_SEALING_KEYS",
    "MCP_PUBLIC_URL",
    "MCP_CONSOLE_URL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "SENTRY_DSN",
)
"""Variables a server never inherits from the shell running the tests."""


async def _verify_token(_verifier: Any, token: str) -> AccessToken | None:
    if token != TOKEN:
        return None
    return AccessToken(token=token, client_id=CLIENT_ID, scopes=[])


class Server:
    """The hosted app on ``127.0.0.1:<random port>``.

    ``environment`` is applied on top of the test process environment (minus
    :data:`CONTROLLED`) for the server's whole lifetime; a ``None`` value
    removes a variable. ``build`` returns the keyword arguments for
    ``build_app``; it runs with that environment in place.
    """

    def __init__(
        self,
        environment: Mapping[str, str | None] | None = None,
        build: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self._environment = {"MCP_PUBLIC_URL": PUBLIC_URL, **(environment or {})}
        self._build = build
        self._patches: list[Any] = []
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    def __enter__(self) -> Server:
        environment = {
            key: value for key, value in os.environ.items() if key not in CONTROLLED
        }
        for key, value in self._environment.items():
            if value is None:
                environment.pop(key, None)
            else:
                environment[key] = value
        self._patches = [
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(
                auth.AppwriteTokenVerifier, "verify_token", _verify_token
            ),
        ]
        for patch in self._patches:
            patch.start()
        try:
            self._start()
        except BaseException:
            self._stop_patches()
            raise
        return self

    def _start(self) -> None:
        app = build_app(**(self._build() if self._build else {}))
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        config = uvicorn.Config(app, log_level="warning", access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._server.run, kwargs={"sockets": [listener]}, daemon=True
        )
        self._thread.start()
        deadline = time.monotonic() + STARTUP_SECONDS
        while not self._server.started:
            if not self._thread.is_alive() or time.monotonic() > deadline:
                self._shutdown()
                raise RuntimeError("the hosted server did not start")
            time.sleep(0.01)
        self.url = f"http://127.0.0.1:{port}"

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._shutdown()
        self._stop_patches()

    def _shutdown(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(STARTUP_SECONDS)
        self._server = None
        self._thread = None

    def _stop_patches(self) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        self._patches = []

    def client(self) -> Client:
        return Client(self.url)


def message(response: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC message of a JSON or single-event SSE response."""
    text = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:] for line in text.splitlines() if line.startswith("data:")]
        text = data[-1]
    return json.loads(text)


class Client:
    """A Streamable-HTTP MCP client speaking raw JSON-RPC, like ChatGPT."""

    def __init__(self, url: str, token: str = TOKEN) -> None:
        self._http = httpx.Client(base_url=url, timeout=30.0)
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        self._ids = iter(range(1, 1_000_000))

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._http.close()

    def call(
        self, method: str, params: Mapping[str, Any] | None = None, *, path: str = "/"
    ) -> tuple[int, dict[str, Any]]:
        """One 2026-07-28 request: protocol version and method in the headers,
        client identity in ``params._meta``."""
        body = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": {
                **(params or {}),
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                    "io.modelcontextprotocol/clientInfo": {
                        "name": "e2e",
                        "version": "1",
                    },
                    "io.modelcontextprotocol/clientCapabilities": {},
                },
            },
        }
        headers = {
            **self._headers,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        response = self._http.post(path, json=body, headers=headers)
        return response.status_code, message(response)

    def initialize(self, *, path: str = "/") -> tuple[int, dict[str, Any]]:
        """A 2025-11-25 ``initialize`` handshake."""
        body = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": "initialize",
            "params": {
                "protocolVersion": LEGACY_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "e2e", "version": "1"},
            },
        }
        response = self._http.post(path, json=body, headers=self._headers)
        return response.status_code, message(response)
