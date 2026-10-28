"""End-to-end harness: the real hosted server, driven over real HTTP.

:class:`Server` boots the production Starlette app (``http_app.build_app``)
under uvicorn on a random localhost port, in a background thread of the test
process. :class:`Client` talks to it the way ChatGPT does: raw JSON-RPC over
Streamable HTTP with ``MCP-Protocol-Version: 2026-07-28``, ``Mcp-Method`` and
the request ``_meta``, asserting on the wire rather than on SDK models (the SDK
client drops capabilities it does not model, such as ``events``).

The one stub is the OAuth token verifier: Cloud OAuth is not reachable from CI,
so a few fixed bearer tokens (:data:`SUBJECTS`) are accepted in place of
Appwrite access tokens, with the claims the real verifier produces. This is the
same seam the unit tests of ``http_app`` use. Everything behind it (routing,
auth middleware, the MCP session manager, the low-level server and its
middleware) is the production code path.

The parties around the server are real servers on localhost too:

* :class:`Cloud` plays the Appwrite REST API the server calls with the
  caller's token (``APPWRITE_ENDPOINT``): resource reads and ``/v1/webhooks``,
  with Appwrite-shaped responses, scopes, plan limits and injectable failures.
* :class:`Appwrite` plays Appwrite's webhooks worker: it signs and posts
  deliveries exactly as ``src/Appwrite/Platform/Workers/Webhooks.php`` does,
  either for a given webhook or, with :meth:`Appwrite.fire`, for every
  webhook stored in :class:`Cloud` that listens to the event.
* :class:`Receiver` plays ChatGPT's webhook endpoint: an HTTPS server with a
  self-signed certificate that checks every delivery with the official
  ``standardwebhooks`` library and echoes verification challenges.
* :class:`Collector` is an OTLP/HTTP metrics endpoint. Every server exports to
  it, so tests read the counters the server really emits.
* :class:`Redis` is a real Dragonfly (production's bus), or ``redis-server``
  where Dragonfly has no build, on a random port: the bus that push streams
  share across server instances. :meth:`Client.stream` opens an
  ``events/stream`` and reads its SSE frames as they arrive.
"""

from __future__ import annotations

import atexit
import base64
import datetime
import hashlib
import hmac
import itertools
import json
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from mcp.server.auth.provider import AccessToken
from opentelemetry import metrics
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest,
)
from sse_starlette.sse import AppStatus
from standardwebhooks import Webhook as StandardWebhook
from standardwebhooks.webhooks import WebhookVerificationError

from mcp_server_appwrite import auth
from mcp_server_appwrite.http_app import Uvicorn, build_app

TOKEN = "e2e-access-token"
"""The bearer token the stubbed verifier accepts, for :data:`SUBJECT`."""

OTHER_TOKEN = "e2e-other-access-token"
"""A token for a second user, :data:`OTHER_SUBJECT`."""

SUBJECTLESS_TOKEN = "e2e-subjectless-access-token"
"""A token that names no user (no ``sub``), like a client-credentials token."""

CLIENT_ID = "e2e-client"
ISSUER = "https://cloud.appwrite.io/v1/oauth2/console"
SUBJECT = "66b1c2d3e4f5a6b7c8d9"
OTHER_SUBJECT = "77c2d3e4f5a6b7c8d9e0"

SUBJECTS: dict[str, str | None] = {
    TOKEN: SUBJECT,
    OTHER_TOKEN: OTHER_SUBJECT,
    SUBJECTLESS_TOKEN: None,
}
"""Every token the stubbed verifier accepts, and the user it names."""

GRANTED: dict[str, list[str]] = {}
"""The OAuth ``scope`` claim of a token, for tests that set one; others carry
none."""

EXPIRES: dict[str, int] = {}
"""The ``exp`` claim (Unix seconds) of a token, for tests that set one; others
never expire."""

PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSION = "2025-11-25"

PUBLIC_URL = "https://mcp.e2e.test"
"""``MCP_PUBLIC_URL`` of every server: the public origin a load balancer would
serve, deliberately not the address the server listens on."""

STARTUP_SECONDS = 15.0

CONTROLLED = (
    "APPWRITE_ENDPOINT",
    "MCP_APPS",
    "MCP_EVENTS",
    "MCP_EVENTS_PUSH",
    "MCP_EVENTS_REDIS_URL",
    "MCP_EVENTS_SEALING_KEYS",
    "MCP_PUBLIC_URL",
    "MCP_CONSOLE_URL",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE",
    "OTEL_EXPORTER_OTLP_COMPRESSION",
    "SENTRY_DSN",
)
"""Variables a server never inherits from the shell running the tests."""


async def _verify_token(_verifier: Any, token: str) -> AccessToken | None:
    """What ``AppwriteTokenVerifier`` returns for a valid Cloud token: the
    console project in ``project_id`` and the issuer, user and client that
    make up the events principal."""
    if token not in SUBJECTS:
        return None
    subject = SUBJECTS[token]
    claims: dict[str, Any] = {"iss": ISSUER, "project_id": "console"}
    if subject is not None:
        claims["sub"] = subject
    return AccessToken(
        token=token,
        client_id=CLIENT_ID,
        scopes=list(GRANTED.get(token, [])),
        expires_at=EXPIRES.get(token),
        subject=subject,
        claims=claims,
    )


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
        self._environment = {
            "MCP_PUBLIC_URL": PUBLIC_URL,
            "OTEL_EXPORTER_OTLP_ENDPOINT": collector().url,
            **(environment or {}),
        }
        self._build = build
        self._patches: list[Any] = []
        self._server: Uvicorn | None = None
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
        config = uvicorn.Config(app, log_level="warning", access_log=False, ws="none")
        self._server = Uvicorn(config, app)
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
        """Stop the way a pod is stopped: uvicorn's SIGTERM handling."""
        if self._server is not None:
            self._server.handle_exit(signal.SIGTERM, None)
        if self._thread is not None:
            self._thread.join(STARTUP_SECONDS)
        self._server = None
        self._thread = None
        # sse_starlette, which serves the SDK's 2025-era SSE responses, patches
        # uvicorn's exit handler to drain every SSE stream in the process. A
        # pod runs one server; here the next server would inherit the drain.
        AppStatus.should_exit = False

    def _stop_patches(self) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        self._patches = []

    def client(
        self, token: str = TOKEN, capabilities: Mapping[str, Any] | None = None
    ) -> Client:
        return Client(self.url, token, capabilities)


NAMED = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}
"""The param a 2026-07-28 request repeats in its ``Mcp-Name`` header (SEP-2243)."""


def message(response: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC message of a JSON or single-event SSE response."""
    text = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:] for line in text.splitlines() if line.startswith("data:")]
        text = data[-1]
    return json.loads(text)


class Client:
    """A Streamable-HTTP MCP client speaking raw JSON-RPC, like ChatGPT.
    ``capabilities`` are the client capabilities each request declares."""

    def __init__(
        self,
        url: str,
        token: str = TOKEN,
        capabilities: Mapping[str, Any] | None = None,
    ) -> None:
        self._capabilities = dict(capabilities or {})
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
                    "io.modelcontextprotocol/clientCapabilities": self._capabilities,
                },
            },
        }
        headers = {
            **self._headers,
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if method in NAMED:
            headers["Mcp-Name"] = str((params or {})[NAMED[method]])
        response = self._http.post(path, json=body, headers=headers)
        return response.status_code, message(response)

    def stream(
        self, params: Mapping[str, Any], *, legacy: bool = False, path: str = "/"
    ) -> Stream:
        """Open an ``events/stream``: as a 2026-07-28 request, or with
        ``legacy`` as a 2025-11-25 one, the way handshake-era clients send it
        after ``initialize``."""
        id = next(self._ids)
        body: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": id,
            "method": "events/stream",
            "params": dict(params),
        }
        headers = {**self._headers}
        if legacy:
            headers["MCP-Protocol-Version"] = LEGACY_PROTOCOL_VERSION
        else:
            body["params"]["_meta"] = {
                "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
                "io.modelcontextprotocol/clientInfo": {"name": "e2e", "version": "1"},
                "io.modelcontextprotocol/clientCapabilities": self._capabilities,
            }
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
            headers["Mcp-Method"] = "events/stream"
        return Stream(self._http.base_url, path, id, body, headers)

    def initialize(self, *, path: str = "/") -> tuple[int, dict[str, Any]]:
        """A 2025-11-25 ``initialize`` handshake."""
        return self.legacy("initialize", path=path)

    def legacy(
        self, method: str, params: Mapping[str, Any] | None = None, *, path: str = "/"
    ) -> tuple[int, dict[str, Any]]:
        """A 2025-11-25 request: ``initialize`` carries the handshake params,
        any other method arrives the way the stateless transport sees it,
        without a session or client capabilities."""
        handshake = {
            "protocolVersion": LEGACY_PROTOCOL_VERSION,
            "capabilities": self._capabilities,
            "clientInfo": {"name": "e2e", "version": "1"},
        }
        body = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": handshake if method == "initialize" else dict(params or {}),
        }
        headers = {**self._headers, "MCP-Protocol-Version": LEGACY_PROTOCOL_VERSION}
        if method == "initialize":
            headers = self._headers
        response = self._http.post(path, json=body, headers=headers)
        return response.status_code, message(response)


_END = object()


class Stream:
    """One open ``events/stream``, read on a thread of its own.

    :meth:`next` returns its JSON-RPC messages in order: the notifications,
    then the final result or error. A response that is not an SSE stream (an
    immediate error) is one message. :meth:`close` aborts the request, which is
    how a client cancels a stream."""

    def __init__(
        self,
        base: httpx.URL,
        path: str,
        id: int,
        body: Mapping[str, Any],
        headers: Mapping[str, str],
    ) -> None:
        self.id = id
        self.status = 0
        self.content_type = ""
        self._http = httpx.Client(base_url=base, timeout=httpx.Timeout(30.0, read=None))
        self._messages: queue.Queue[Any] = queue.Queue()
        self._opened = threading.Event()
        self._thread = threading.Thread(
            target=self._read, args=(path, body, headers), daemon=True
        )
        self._thread.start()

    def _read(
        self, path: str, body: Mapping[str, Any], headers: Mapping[str, str]
    ) -> None:
        try:
            with self._http.stream(
                "POST", path, json=body, headers=headers
            ) as response:
                self.status = response.status_code
                self.content_type = response.headers.get("content-type", "")
                self._opened.set()
                if not self.content_type.startswith("text/event-stream"):
                    self._messages.put(json.loads(response.read()))
                    return
                for line in response.iter_lines():
                    if line.startswith("data:"):
                        self._messages.put(json.loads(line[5:]))
        except (httpx.HTTPError, RuntimeError, ValueError):
            pass
        finally:
            self._opened.set()
            self._messages.put(_END)

    def next(self, timeout: float = 10.0) -> dict[str, Any]:
        """The next message; fails when the stream ended or ``timeout`` passed."""
        try:
            message = self._messages.get(timeout=timeout)
        except queue.Empty:
            raise AssertionError(f"no message within {timeout}s") from None
        if message is _END:
            self._messages.put(_END)
            raise AssertionError("the stream ended")
        return message

    def until(self, method: str, timeout: float = 10.0) -> dict[str, Any]:
        """Skip heartbeats until a ``method`` notification arrives."""
        deadline = time.monotonic() + timeout
        while True:
            message = self.next(max(deadline - time.monotonic(), 0.01))
            if message.get("method") == method:
                return message
            if message.get("method") != "notifications/events/heartbeat":
                raise AssertionError(f"expected {method}, got {message}")

    def ended(self, timeout: float = 10.0) -> bool:
        """Whether the response finished within ``timeout``."""
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def close(self) -> None:
        self._http.close()
        self._thread.join(5.0)

    def __enter__(self) -> Stream:
        self._opened.wait(30.0)
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class Redis:
    """A Redis-protocol server on ``127.0.0.1:<random port>``, without
    persistence: ``dragonfly`` when it is on ``PATH``, as in CI, otherwise
    ``redis-server``.

    :meth:`stop` and :meth:`start` take it down and bring it back on the same
    port, for outages."""

    def __init__(self) -> None:
        dragonfly = shutil.which("dragonfly")
        redis = shutil.which("redis-server")
        if dragonfly is not None:
            self._arguments = [dragonfly, "--dbfilename=", "--proactor_threads=2"]
        elif redis is not None:
            self._arguments = [redis, "--save", "", "--appendonly", "no"]
        else:
            raise RuntimeError(
                "push streams need dragonfly or redis-server on PATH: "
                "`brew install redis` or `apt-get install redis-server`"
            )
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        self.port = listener.getsockname()[1]
        listener.close()
        self.url = f"redis://127.0.0.1:{self.port}/0"
        self._process: subprocess.Popen[bytes] | None = None
        self.start()

    def start(self) -> None:
        self._process = subprocess.Popen(
            [*self._arguments, "--port", str(self.port), "--bind", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + STARTUP_SECONDS
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), 0.2) as probe:
                    probe.sendall(b"PING\r\n")
                    if probe.recv(16).startswith(b"+PONG"):
                        return
            except OSError:
                time.sleep(0.02)
        raise RuntimeError(f"{self._arguments[0]} did not start")

    def stop(self) -> None:
        if self._process is not None:
            self._process.kill()
            self._process.wait()
            self._process = None

    def pause(self) -> None:
        """Freeze the server with its connections open: a peer that is gone
        without a reset, as after a node loss."""
        if self._process is not None:
            self._process.send_signal(signal.SIGSTOP)

    def resume(self) -> None:
        if self._process is not None:
            self._process.send_signal(signal.SIGCONT)


_redis: Redis | None = None


def redis() -> Redis:
    """The process-wide Redis every push server shares."""
    global _redis
    with _collector_lock:
        if _redis is None:
            _redis = Redis()
            atexit.register(_redis.stop)
        return _redis


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Clients that time out or hang up mid-response are expected."""


class Collector:
    """An OTLP/HTTP metrics endpoint on localhost.

    The server's ``telemetry`` module exports to it through the real OTLP
    exporter. :meth:`count` forces an export and reads the latest cumulative
    value of a counter, summed over the points whose attributes include the
    given ones."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._points: dict[tuple[str, frozenset[tuple[str, str]]], int] = {}
        collector = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                collector._ingest(self.rfile.read(length))
                self.send_response(200)
                self.send_header("Content-Type", "application/x-protobuf")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def _ingest(self, body: bytes) -> None:
        request = ExportMetricsServiceRequest()
        request.ParseFromString(body)
        with self._lock:
            for resource in request.resource_metrics:
                for scope in resource.scope_metrics:
                    for metric in scope.metrics:
                        if not metric.HasField("sum"):
                            continue
                        for point in metric.sum.data_points:
                            attributes = frozenset(
                                (pair.key, pair.value.string_value)
                                for pair in point.attributes
                            )
                            self._points[(metric.name, attributes)] = point.as_int

    def count(self, name: str, **attributes: str) -> int:
        flush = getattr(metrics.get_meter_provider(), "force_flush", None)
        if flush is not None:
            flush()
        wanted = set(attributes.items())
        with self._lock:
            return sum(
                value
                for (metric, labels), value in self._points.items()
                if metric == name and wanted <= labels
            )

    def wait(
        self, name: str, expected: int, timeout: float = 15.0, **attributes: str
    ) -> int:
        """Poll until the counter reaches ``expected``; return its value."""
        deadline = time.monotonic() + timeout
        value = self.count(name, **attributes)
        while value < expected and time.monotonic() < deadline:
            time.sleep(0.05)
            value = self.count(name, **attributes)
        return value


_collector: Collector | None = None
_collector_lock = threading.Lock()


def collector() -> Collector:
    """The process-wide collector. Telemetry initializes once per process, so
    every server exports to the same endpoint."""
    global _collector
    with _collector_lock:
        if _collector is None:
            _collector = Collector()
        return _collector


def certificate(directory: Path, hostname: str) -> tuple[Path, Path]:
    """A self-signed certificate for ``hostname`` (DNS name only, no IP)."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.UTC)
    public = key.public_key()
    built = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(public)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(public), False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / "certificate.pem"
    key_path = directory / "key.pem"
    certificate_path.write_bytes(built.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


@dataclass(frozen=True)
class Reply:
    """How the receiver answers one request."""

    status: int = 200
    delay: float = 0.0
    """Seconds to wait before answering; longer than the egress timeout makes
    the request time out."""
    location: str | None = None


@dataclass(frozen=True)
class Received:
    """One request as the receiver saw it."""

    path: str
    headers: dict[str, str]
    """Lower-cased header names."""
    body: bytes
    server_name: str | None
    """TLS SNI the client sent."""
    at: float
    verified: tuple[bool, ...]
    """Per configured secret: whether ``standardwebhooks`` accepted the request."""

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass
class _Endpoint:
    secrets: tuple[str, ...]
    replies: list[Reply]
    received: list[Received] = field(default_factory=list)
    active: int = 0
    peak: int = 0


class Receiver:
    """A subscriber's webhook endpoint at ``https://localhost:<port>``.

    Each endpoint path has the ``whsec_`` secrets its subscription was made
    with and a script of replies (the last one repeats). Every request is
    checked with the official ``standardwebhooks`` library against each
    secret. A signed ``{"type": "verification"}`` request has its challenge
    echoed, as ChatGPT does.
    """

    HOSTNAME = "localhost"

    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.certificate, key = certificate(Path(self._directory.name), self.HOSTNAME)
        self._context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self._context.load_cert_chain(self.certificate, key)
        self._context.sni_callback = self._record_sni
        self._lock = threading.Condition()
        self._endpoints: dict[str, _Endpoint] = {}
        self.handshake_failures = 0
        receiver = self

        class Server(_QuietServer):
            def finish_request(self, request: Any, client_address: Any) -> None:
                try:
                    connection = receiver._context.wrap_socket(
                        request, server_side=True
                    )
                except (ssl.SSLError, OSError):
                    with receiver._lock:
                        receiver.handshake_failures += 1
                    return
                try:
                    self.RequestHandlerClass(connection, client_address, self)
                finally:
                    connection.close()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                headers = {name.lower(): value for name, value in self.headers.items()}
                sni = getattr(self.connection, "e2e_server_name", None)
                status, extra, content = receiver._receive(
                    self.path, headers, body, sni
                )
                self.send_response(status)
                for name, value in extra.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(content)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _record_sni(
        self, connection: ssl.SSLSocket, server_name: str | None, context: Any
    ) -> None:
        setattr(connection, "e2e_server_name", server_name)

    def trust(self) -> ssl.SSLContext:
        """A client context that trusts only this receiver's certificate."""
        return ssl.create_default_context(cafile=str(self.certificate))

    def endpoint(self, secrets: Sequence[str], replies: Iterable[Reply] = ()) -> str:
        """Register an endpoint and return its URL."""
        path = f"/hooks/{token()}"
        with self._lock:
            self._endpoints[path] = _Endpoint(
                tuple(secrets), list(replies) or [Reply()]
            )
        return f"https://{self.HOSTNAME}:{self.port}{path}"

    def _receive(
        self, path: str, headers: dict[str, str], body: bytes, sni: str | None
    ) -> tuple[int, dict[str, str], bytes]:
        with self._lock:
            endpoint = self._endpoints.get(path)
            if endpoint is None:
                return 404, {}, b""
            verified = tuple(
                _standard_webhook(secret, body, headers) for secret in endpoint.secrets
            )
            endpoint.received.append(
                Received(path, headers, body, sni, time.time(), verified)
            )
            replies = endpoint.replies
            reply = replies.pop(0) if len(replies) > 1 else replies[0]
            endpoint.active += 1
            endpoint.peak = max(endpoint.peak, endpoint.active)
            self._lock.notify_all()
        try:
            if reply.delay:
                time.sleep(reply.delay)
            extra = {"Location": reply.location} if reply.location else {}
            content = b'{"ok":true}'
            payload = _json_object(body)
            if payload.get("type") == "verification" and any(verified):
                content = json.dumps({"challenge": payload.get("challenge")}).encode()
            return reply.status, extra, content
        finally:
            with self._lock:
                endpoint.active -= 1

    def received(self, url: str) -> list[Received]:
        with self._lock:
            return list(self._endpoints[urlsplit(url).path].received)

    def peak(self, url: str) -> int:
        """Most requests this endpoint handled at the same time."""
        with self._lock:
            return self._endpoints[urlsplit(url).path].peak

    def wait(self, url: str, count: int, timeout: float = 10.0) -> list[Received]:
        """Wait until the endpoint has received ``count`` requests."""
        path = urlsplit(url).path
        with self._lock:
            self._lock.wait_for(
                lambda: len(self._endpoints[path].received) >= count, timeout
            )
            received = list(self._endpoints[path].received)
        if len(received) < count:
            raise AssertionError(f"{path} received {len(received)} of {count}")
        return received

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._directory.cleanup()


def token() -> str:
    return secrets.token_hex(8)


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _standard_webhook(secret: str, body: bytes, headers: Mapping[str, str]) -> bool:
    try:
        StandardWebhook(secret).verify(body, dict(headers), json_parse=False)
    except WebhookVerificationError:
        return False
    return True


def whsec(size: int = 32) -> str:
    """A fresh ``whsec_`` secret, as a client generates one."""
    return "whsec_" + base64.b64encode(os.urandom(size)).decode()


@dataclass(frozen=True)
class Webhook:
    """An Appwrite project webhook, as Appwrite stores it."""

    id: str
    url: str
    secret: str
    """Appwrite's signing key for this webhook."""
    events: tuple[str, ...]
    project: str
    user: str
    password: str
    name: str = "MCP events"
    enabled: bool = True


def fired(event: str) -> list[str]:
    """Every form Appwrite lists in ``X-Appwrite-Webhook-Events`` for a
    concrete event: each ID replaced by ``*`` or not, with and without the
    action (``Event::generateEvents``)."""
    parts = event.split(".")
    action, resources = parts[-1], parts[:-1]
    positions = range(1, len(resources), 2)
    forms: list[str] = []
    for mask in itertools.product((False, True), repeat=len(positions)):
        names = list(resources)
        for position, wildcard in zip(positions, mask, strict=True):
            if wildcard:
                names[position] = "*"
        forms.append(".".join([*names, action]))
        forms.append(".".join(names))
    return list(dict.fromkeys(forms))


class Appwrite:
    """Appwrite's webhooks worker, posting to one server.

    Requests are built the way ``src/Appwrite/Platform/Workers/Webhooks.php``
    builds them: the ``X-Appwrite-Webhook-*`` headers, Basic auth only when the
    webhook has both a username and a password, and
    ``X-Appwrite-Webhook-Signature = base64(HMAC-SHA1(url . body, secret))``
    over the webhook's configured URL. The request is addressed to that URL's
    path on the server, the way a load balancer in front of ``MCP_PUBLIC_URL``
    forwards it. Unlike the real worker it does not filter by the webhook's
    events, so tests can send what an edited webhook would.
    """

    USER_AGENT = "Appwrite-Server v1.8.0. Please report abuse at security@appwrite.io"

    def __init__(self, server: Server) -> None:
        self._http = httpx.Client(base_url=server.url, timeout=30.0)

    def close(self) -> None:
        self._http.close()

    @staticmethod
    def sign(url: str, body: bytes, secret: str) -> str:
        digest = hmac.new(secret.encode(), url.encode() + body, hashlib.sha1)
        return base64.b64encode(digest.digest()).decode()

    def deliver(
        self,
        webhook: Webhook,
        event: str,
        payload: Mapping[str, Any] | bytes,
        *,
        delivery: str | None = None,
        events: Sequence[str] | None = None,
        signed_url: str | None = None,
        headers: Mapping[str, str] | None = None,
        method: str = "POST",
    ) -> httpx.Response:
        """Deliver one occurrence of ``event``. Pass the same ``delivery`` id
        to replay an Appwrite retry of the same occurrence."""
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        delivery = (
            delivery or hashlib.md5(f"{token()}:{webhook.id}".encode()).hexdigest()
        )
        request_headers = {
            "Content-Type": "application/json",
            "User-Agent": self.USER_AGENT,
            "X-Appwrite-Webhook-Id": webhook.id,
            "X-Appwrite-Webhook-Events": ",".join(
                fired(event) if events is None else events
            ),
            "X-Appwrite-Webhook-Name": webhook.name,
            "X-Appwrite-Webhook-User-Id": "",
            "X-Appwrite-Webhook-Project-Id": webhook.project,
            "X-Appwrite-Webhook-Delivery-Id": delivery,
            "X-Appwrite-Webhook-Signature": self.sign(
                signed_url or webhook.url, body, webhook.secret
            ),
        }
        if webhook.user and webhook.password:
            credentials = f"{webhook.user}:{webhook.password}".encode()
            request_headers["Authorization"] = (
                "Basic " + base64.b64encode(credentials).decode()
            )
        request_headers.update(headers or {})
        return self._http.request(
            method, urlsplit(webhook.url).path, content=body, headers=request_headers
        )

    def fire(
        self,
        cloud: Cloud,
        project: str,
        event: str,
        payload: Mapping[str, Any] | bytes,
    ) -> list[httpx.Response]:
        """What Appwrite does when ``event`` happens in ``project``: one
        delivery to every enabled webhook stored in ``cloud`` whose events
        include one of the forms Appwrite generates for it."""
        forms = set(fired(event))
        return [
            self.deliver(webhook, event, payload)
            for webhook in cloud.webhooks(project)
            if webhook.enabled and forms & set(webhook.events)
        ]


SCOPES = frozenset(
    {
        "functions.read",
        "sites.read",
        "tables.read",
        "buckets.read",
        "users.read",
        "webhooks.read",
        "webhooks.write",
    }
)
"""Every Appwrite scope the events methods need."""

APPS_SCOPES = frozenset(
    {
        "rows.read",
        "rows.write",
        "functions.write",
        "sites.write",
        "execution.read",
        "files.read",
        "users.write",
    }
)
"""The scopes the MCP Apps views need on top of :data:`SCOPES`."""

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
"""A 1x1 PNG: what Appwrite's image previews return, in miniature."""

CUSTOM_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,35}")

AUTH_PASSWORD_MAX = 2048
"""What appwrite/appwrite#14293 must allow for ``authPassword`` (today 256)."""


@dataclass
class Project:
    """One Appwrite project in :class:`Cloud`."""

    id: str
    functions: set[str] = field(default_factory=set)
    sites: set[str] = field(default_factory=set)
    tables: set[tuple[str, str]] = field(default_factory=set)
    """``(database id, table id)`` pairs."""
    buckets: set[str] = field(default_factory=set)
    webhook_limit: int | None = None
    """Webhooks the plan allows (Free: 2); ``None`` for unlimited."""
    webhooks: dict[str, dict[str, Any]] = field(default_factory=dict)
    """Stored webhook documents, write-only fields included."""
    columns: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    """Column models per ``(database id, table id)``."""
    rows: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)
    """Row documents per ``(database id, table id)``, oldest first."""
    deployments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    """Deployment models per Function or Site ID, oldest first."""
    executions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    """Execution models per Function ID, oldest first."""
    files: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    """File models per bucket ID, oldest first."""
    users: list[dict[str, Any]] = field(default_factory=list)
    """User documents, oldest first, password hash included."""
    sessions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    """Session models per user ID, oldest first."""
    usage: dict[str, dict[str, int]] = field(default_factory=dict)
    """Usage values per metric, keyed by interval start (ISO 8601), for both
    the events and the gauges API."""


@dataclass
class Failure:
    """An injected Appwrite error for the next ``times`` matching requests."""

    method: str
    path: re.Pattern[str]
    status: int
    type: str
    times: int


@dataclass
class Interleaving:
    """Another client's work, run once before the next matching request."""

    method: str
    path: re.Pattern[str]
    action: Callable[[], None]


class _Refusal(Exception):
    def __init__(self, status: int, type: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.type = type
        self.message = message


def _stamp() -> str:
    """Now, in Appwrite's response format (``2026-10-09T10:00:00.250+00:00``)."""
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds")


class Cloud:
    """The Appwrite REST API at ``http://127.0.0.1:<port>/v1``, as the server
    reaches it with a caller's OAuth token, or with the project API key the
    stdio transport sends as ``X-Appwrite-Key`` (granted like a token).

    Only what ``events/subscribe`` and ``events/unsubscribe`` call is served:
    the console project lookup ``resolve_client`` makes for regions, the
    resource reads that authorize a subscription, and ``/v1/webhooks``. Each
    request is checked the way Appwrite checks it: an unknown project is
    ``404 project_not_found``, a token without access to the project is
    ``401 user_unauthorized``, a missing scope is
    ``401 general_unauthorized_scope``. Webhooks follow the current API: the
    password is write-only, the secret is returned only by create and by the
    secret update, an update keeps the password unless it sends one, and a
    project at its plan limit refuses a create with
    ``403 additional_resource_not_allowed``.
    """

    VERSION = "1.8.0"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._grants: dict[str, tuple[frozenset[str], frozenset[str]]] = {}
        self._projects: dict[str, Project] = {}
        self._failures: list[Failure] = []
        self._interleavings: list[Interleaving] = []
        self.requests: list[tuple[str, str, str | None]] = []
        """``(method, path, bearer token)`` of every request, in order."""
        cloud = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = self.rfile.read(length) if length else b""
                # A header sent twice reaches Appwrite as one comma-joined
                # value, as the proxies in front of it combine them.
                headers: dict[str, str] = {}
                for name, value in self.headers.items():
                    name = name.lower()
                    headers[name] = (
                        f"{headers[name]}, {value}" if name in headers else value
                    )
                status, payload = cloud._handle(self.command, self.path, headers, body)
                if isinstance(payload, bytes):
                    content, kind = payload, "image/png"
                else:
                    content = b"" if payload is None else json.dumps(payload).encode()
                    # Appwrite answers 204 with an HTML content type, which the
                    # Python SDK returns as raw bytes.
                    kind = (
                        "text/html; charset=UTF-8"
                        if payload is None
                        else "application/json"
                    )
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = _QuietServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def add(self, project: Project) -> Project:
        with self._lock:
            self._projects[project.id] = project
        return project

    def grant(
        self, token: str, projects: Iterable[str], scopes: Iterable[str] = SCOPES
    ) -> None:
        """Let ``token`` (or API key) reach ``projects`` with ``scopes``."""
        with self._lock:
            self._grants[token] = (frozenset(projects), frozenset(scopes))

    def fail(
        self,
        method: str,
        path: str,
        status: int = 500,
        type: str = "general_server_error",
        *,
        times: int = 1,
    ) -> None:
        """Answer the next ``times`` requests matching ``method`` and the
        ``path`` regular expression with an Appwrite error."""
        with self._lock:
            self._failures.append(
                Failure(method, re.compile(path), status, type, times)
            )

    def before(self, method: str, path: str, action: Callable[[], None]) -> None:
        """Run ``action`` once, right before the next request matching
        ``method`` and the ``path`` expression is handled, while that request
        waits: how a concurrent client's calls land between two of a
        server's. ``action`` may call this API itself."""
        with self._lock:
            self._interleavings.append(Interleaving(method, re.compile(path), action))

    def store(self, project: str, document: Mapping[str, Any]) -> None:
        """Put a webhook straight into the project, as a person in the Console
        (or an earlier subscribe) would have left it."""
        stamp = _stamp()
        with self._lock:
            self._projects[project].webhooks[document["$id"]] = {
                "$createdAt": stamp,
                "$updatedAt": stamp,
                "events": [],
                "tls": True,
                "authUsername": "",
                "httpPass": "",
                "signatureKey": secrets.token_hex(64),
                "enabled": True,
                "logs": "",
                "attempts": 0,
                **document,
            }

    def remove(self, project: str, id: str) -> None:
        """Delete a webhook, as a person in the Console would."""
        with self._lock:
            del self._projects[project].webhooks[id]

    def documents(self, project: str) -> dict[str, dict[str, Any]]:
        """The stored webhook documents, write-only fields included."""
        with self._lock:
            return {
                id: dict(document)
                for id, document in self._projects[project].webhooks.items()
            }

    def webhooks(self, project: str) -> list[Webhook]:
        """The project's webhooks as Appwrite's worker reads them."""
        return [
            Webhook(
                id=id,
                url=document["url"],
                secret=document["signatureKey"],
                events=tuple(document["events"]),
                project=project,
                user=document["authUsername"],
                password=document["httpPass"],
                name=document["name"],
                enabled=document["enabled"],
            )
            for id, document in self.documents(project).items()
        ]

    def calls(self, method: str, path: str) -> int:
        """How many requests matched ``method`` and the ``path`` expression."""
        pattern = re.compile(path)
        with self._lock:
            return sum(
                1
                for (made, requested, _) in self.requests
                if made == method and pattern.fullmatch(requested)
            )

    def _handle(
        self, method: str, target: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, Any]:
        parts = urlsplit(target)
        path = parts.path
        query = parse_qs(parts.query)
        authorization = headers.get("authorization", "")
        token = (
            authorization[7:]
            if authorization.startswith("Bearer ")
            else headers.get("x-appwrite-key")
        )
        with self._lock:
            interleaving = next(
                (
                    each
                    for each in self._interleavings
                    if each.method == method and each.path.fullmatch(path)
                ),
                None,
            )
            if interleaving is not None:
                self._interleavings.remove(interleaving)
        if interleaving is not None:
            interleaving.action()
        with self._lock:
            self.requests.append((method, path, token))
            try:
                for failure in self._failures:
                    if failure.method == method and failure.path.fullmatch(path):
                        failure.times -= 1
                        if failure.times <= 0:
                            self._failures.remove(failure)
                        raise _Refusal(failure.status, failure.type, "Injected")
                payload = json.loads(body) if body else {}
                return self._route(method, path, query, headers, token, payload)
            except _Refusal as refusal:
                return refusal.status, {
                    "message": refusal.message,
                    "code": refusal.status,
                    "type": refusal.type,
                    "version": self.VERSION,
                }

    def _route(
        self,
        method: str,
        path: str,
        query: Mapping[str, list[str]],
        headers: Mapping[str, str],
        token: str | None,
        payload: Mapping[str, Any],
    ) -> tuple[int, Any]:
        grant = self._grants.get(token or "")
        region = re.fullmatch(r"/v1/projects/([^/]+)", path)
        if method == "GET" and region:
            if grant is None or region.group(1) not in grant[0]:
                raise _Refusal(404, "project_not_found", "Project not found")
            return 200, {"$id": region.group(1), "region": "default"}

        project = self._projects.get(headers.get("x-appwrite-project", ""))
        if project is None:
            raise _Refusal(404, "project_not_found", "Project not found")
        if grant is None or project.id not in grant[0]:
            raise _Refusal(
                401, "user_unauthorized", "The current user is not authorized"
            )
        scopes = grant[1]

        def need(scope: str) -> None:
            if scope not in scopes:
                raise _Refusal(
                    401,
                    "general_unauthorized_scope",
                    f'jane@example.com (role: owner) missing scopes (["{scope}"])',
                )

        if match := re.fullmatch(
            r"/v1/(functions|sites)/([^/]+)/deployments(?:/([^/]+))?(/status)?",
            path,
        ):
            service, resource, deployment, status = match.groups()
            need(f"{service}.write" if status else f"{service}.read")
            if resource not in (
                project.functions if service == "functions" else project.sites
            ):
                raise _Refusal(404, f"{service[:-1]}_not_found", "Not found")
            deployments = project.deployments.get(resource, [])
            if deployment is None:
                page = _page(list(reversed(deployments)), query)
                return 200, {"total": len(deployments), "deployments": page}
            found = next((d for d in deployments if d["$id"] == deployment), None)
            if found is None:
                raise _Refusal(404, "deployment_not_found", "Deployment not found")
            if status and method == "PATCH":
                found["status"] = "canceled"
                found["$updatedAt"] = _stamp()
            return 200, found
        if match := re.fullmatch(
            r"/v1/functions/([^/]+)/executions(?:/([^/]+))?", path
        ):
            need("execution.read")
            resource, id = match.groups()
            if resource not in project.functions:
                raise _Refusal(404, "function_not_found", "Function not found")
            executions = project.executions.get(resource, [])
            if id is None:
                matching = _filter(list(reversed(executions)), query)
                return 200, {
                    "total": len(matching),
                    "executions": _page(matching, query),
                }
            found = next((each for each in executions if each["$id"] == id), None)
            if found is None:
                raise _Refusal(404, "execution_not_found", "Execution not found")
            return 200, found
        if match := re.fullmatch(
            r"/v1/tablesdb/([^/]+)/tables/([^/]+)/rows(?:/([^/]+))?", path
        ):
            database, table, row = match.groups()
            if (database, table) not in project.tables:
                raise _Refusal(404, "table_not_found", "Table not found")
            rows = project.rows.setdefault((database, table), [])
            if row is None:
                need("rows.read")
                return 200, {"total": len(rows), "rows": _page(rows[::-1], query)}
            found = next((each for each in rows if each["$id"] == row), None)
            if found is None:
                raise _Refusal(404, "row_not_found", "Row not found")
            if method == "PATCH":
                need("rows.write")
                found.update(payload.get("data") or {})
                found["$updatedAt"] = _stamp()
            else:
                need("rows.read")
            return 200, found
        if match := re.fullmatch(r"/v1/usage/(events|gauges)", path):
            start = (query.get("startAt") or [""])[0]
            metrics = [
                value
                for key, values in query.items()
                if key.startswith("metrics")
                for value in values
            ]
            return 200, {
                "interval": (query.get("interval") or [""])[0],
                "metrics": [
                    {
                        "metric": metric,
                        "points": [
                            {"time": time, "value": value}
                            for time, value in sorted(
                                project.usage.get(metric, {}).items()
                            )
                            if not start or _instant(time) >= _instant(start)
                        ],
                    }
                    for metric in metrics
                ],
            }
        if match := re.fullmatch(r"/v1/functions/([^/]+)", path):
            need("functions.read")
            if match.group(1) not in project.functions:
                raise _Refusal(404, "function_not_found", "Function not found")
            return 200, function(match.group(1))
        if match := re.fullmatch(r"/v1/sites/([^/]+)", path):
            need("sites.read")
            if match.group(1) not in project.sites:
                raise _Refusal(404, "site_not_found", "Site not found")
            return 200, {"$id": match.group(1)}
        if match := re.fullmatch(r"/v1/tablesdb/([^/]+)/tables/([^/]+)", path):
            need("tables.read")
            database, table = match.groups()
            if database not in {db for db, _ in project.tables}:
                raise _Refusal(404, "database_not_found", "Database not found")
            if (database, table) not in project.tables:
                raise _Refusal(404, "table_not_found", "Table not found")
            stamp = _stamp()
            return 200, {
                "$id": table,
                "$createdAt": stamp,
                "$updatedAt": stamp,
                "$permissions": [],
                "databaseId": database,
                "name": table,
                "enabled": True,
                "rowSecurity": False,
                "columns": project.columns.get((database, table), []),
                "indexes": [],
                "bytesMax": 0,
                "bytesUsed": 0,
            }
        if match := re.fullmatch(
            r"/v1/storage/buckets/([^/]+)/files(?:/([^/]+))?(/preview)?", path
        ):
            need("files.read")
            id, file_id, preview = match.groups()
            if id not in project.buckets:
                raise _Refusal(404, "storage_bucket_not_found", "Bucket not found")
            files = project.files.get(id, [])
            if file_id is None:
                found = files[::-1]
                if term := (query.get("search") or [""])[0].lower():
                    found = [each for each in found if term in each["name"].lower()]
                return 200, {"total": len(found), "files": _page(found, query)}
            if preview:
                # Appwrite's image transformations refuse what is not an image.
                stored = next((each for each in files if each["$id"] == file_id), None)
                if stored is not None and not stored["mimeType"].startswith("image/"):
                    raise _Refusal(400, "storage_file_type_unsupported", "Not an image")
                return 200, PNG
            stored = next((each for each in files if each["$id"] == file_id), None)
            if stored is None:
                raise _Refusal(404, "storage_file_not_found", "File not found")
            return 200, stored
        if match := re.fullmatch(r"/v1/storage/buckets/([^/]+)", path):
            need("buckets.read")
            if match.group(1) not in project.buckets:
                raise _Refusal(404, "storage_bucket_not_found", "Bucket not found")
            return 200, bucket(match.group(1))
        if path == "/v1/users" and method == "GET":
            need("users.read")
            found = project.users[::-1]
            if term := (query.get("search") or [""])[0].lower():
                found = [
                    each
                    for each in found
                    if any(
                        term in str(each.get(key, "")).lower()
                        for key in ("$id", "name", "email", "phone")
                    )
                ]
            return 200, {"total": len(found), "users": _page(found, query)}
        if match := re.fullmatch(
            r"/v1/users/([^/]+)(/status|/labels|/verification|/sessions)?", path
        ):
            id, action = match.groups()
            found = next((each for each in project.users if each["$id"] == id), None)
            if found is None:
                raise _Refusal(404, "user_not_found", "User not found")
            if method == "GET":
                need("users.read")
                if action == "/sessions":
                    sessions = project.sessions.get(id, [])
                    return 200, {"total": len(sessions), "sessions": sessions}
                return 200, found
            need("users.write")
            if action == "/status":
                found["status"] = payload["status"]
            elif action == "/labels":
                found["labels"] = list(payload["labels"])
            elif action == "/verification":
                found["emailVerification"] = payload["emailVerification"]
            found["$updatedAt"] = _stamp()
            return 200, found
        if path == "/v1/webhooks":
            if method == "GET":
                need("webhooks.read")
                return 200, self._list(project, query)
            need("webhooks.write")
            return 201, self._create(project, payload)
        if match := re.fullmatch(r"/v1/webhooks/([^/]+)(/secret)?", path):
            id, secret = match.groups()
            document = project.webhooks.get(id)
            need("webhooks.read" if method == "GET" else "webhooks.write")
            if document is None:
                raise _Refusal(404, "webhook_not_found", "Webhook not found")
            if method == "GET" and not secret:
                return 200, _model(id, document)
            if method == "PUT" and not secret:
                return 200, self._update(document, payload, id)
            if method == "PATCH" and secret:
                document["signatureKey"] = payload.get("secret") or secrets.token_hex(
                    64
                )
                document["$updatedAt"] = _stamp()
                return 200, _model(id, document, secret=True)
            if method == "DELETE" and not secret:
                del project.webhooks[id]
                return 204, None
        raise _Refusal(404, "general_route_not_found", f"No route for {path}")

    def _list(self, project: Project, query: Mapping[str, list[str]]) -> Any:
        limit, offset = 25, 0
        for key, values in query.items():
            if key.startswith("queries["):
                parsed = json.loads(values[0])
                if parsed["method"] == "limit":
                    limit = parsed["values"][0]
                elif parsed["method"] == "offset":
                    offset = parsed["values"][0]
        documents = list(project.webhooks.items())
        page = documents[offset : offset + limit]
        return {
            "total": len(documents),
            "webhooks": [_model(id, document) for id, document in page],
        }

    def _create(self, project: Project, payload: Mapping[str, Any]) -> Any:
        id = payload.get("webhookId", "")
        if not CUSTOM_ID.fullmatch(id):
            raise _Refusal(400, "general_argument_invalid", "Invalid webhookId")
        if id in project.webhooks:
            raise _Refusal(409, "webhook_already_exists", "Webhook already exists")
        if (
            project.webhook_limit is not None
            and len(project.webhooks) >= project.webhook_limit
        ):
            raise _Refusal(
                403,
                "additional_resource_not_allowed",
                "The maximum number of webhooks allowed for the selected plan "
                "has reached. Upgrade to increase the limit.",
            )
        secret = payload.get("secret") or secrets.token_hex(64)
        _check(payload, secret)
        stamp = _stamp()
        document = {
            "$createdAt": stamp,
            "$updatedAt": stamp,
            "name": payload["name"],
            "url": payload["url"],
            "events": list(payload["events"]),
            "tls": payload.get("tls", False),
            "authUsername": payload.get("authUsername", ""),
            "httpPass": payload.get("authPassword", ""),
            "signatureKey": secret,
            "enabled": payload.get("enabled", True),
            "logs": "",
            "attempts": 0,
        }
        project.webhooks[id] = document
        return _model(id, document, secret=True)

    def _update(
        self, document: dict[str, Any], payload: Mapping[str, Any], id: str
    ) -> Any:
        _check(payload, None)
        same = payload["url"] == document["url"] and (
            payload.get("tls", False) or not document["tls"]
        )
        password = payload.get("authPassword")
        if password is not None or not same:
            document["httpPass"] = password or ""
        enabled = payload.get("enabled", True)
        document.update(
            name=payload["name"],
            url=payload["url"],
            events=list(payload["events"]),
            tls=payload.get("tls", False),
            authUsername=payload.get("authUsername", ""),
            enabled=enabled,
        )
        if enabled:
            document["attempts"] = 0
        document["$updatedAt"] = _stamp()
        return _model(id, document)


def _page(
    documents: list[dict[str, Any]], query: Mapping[str, list[str]]
) -> list[dict[str, Any]]:
    """``documents`` (newest first) narrowed by ``limit`` and ``cursorAfter``
    queries; the views only ask for the newest first."""
    limit = 25
    for key, values in query.items():
        if not key.startswith("queries["):
            continue
        parsed = json.loads(values[0])
        if parsed["method"] == "limit":
            limit = parsed["values"][0]
        elif parsed["method"] == "cursorAfter":
            ids = [document["$id"] for document in documents]
            if parsed["values"][0] not in ids:
                raise _Refusal(400, "general_cursor_not_found", "Cursor not found")
            documents = documents[ids.index(parsed["values"][0]) + 1 :]
    return documents[:limit]


def _filter(
    documents: list[dict[str, Any]], query: Mapping[str, list[str]]
) -> list[dict[str, Any]]:
    """``documents`` narrowed by ``equal`` queries."""
    for key, values in query.items():
        if not key.startswith("queries["):
            continue
        parsed = json.loads(values[0])
        if parsed["method"] == "equal":
            documents = [
                document
                for document in documents
                if document.get(parsed["attribute"]) in parsed["values"]
            ]
    return documents


def _instant(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value)


def column(key: str, type: str) -> dict[str, Any]:
    """A ``Column`` model."""
    stamp = _stamp()
    return {
        "key": key,
        "type": type,
        "status": "available",
        "error": "",
        "required": False,
        "array": False,
        "$createdAt": stamp,
        "$updatedAt": stamp,
        **({"size": 255} if type == "string" else {}),
    }


def row(id: str, database: str, table: str, sequence: int, **values: Any) -> Any:
    """A ``Row`` model holding ``values``."""
    stamp = _stamp()
    return {
        "$id": id,
        "$sequence": str(sequence),
        "$tableId": table,
        "$databaseId": database,
        "$createdAt": stamp,
        "$updatedAt": stamp,
        "$permissions": [],
        **values,
    }


def deployment(
    id: str, resource: str, status: str, logs: str = "", **values: Any
) -> dict[str, Any]:
    """A ``Deployment`` model of a Function."""
    stamp = _stamp()
    return {
        "$id": id,
        "$createdAt": stamp,
        "$updatedAt": stamp,
        "type": "vcs",
        "resourceId": resource,
        "resourceType": "functions",
        "entrypoint": "index.js",
        "sourceSize": 1843,
        "buildSize": 0,
        "totalSize": 1843,
        "buildId": "",
        "activate": True,
        "screenshotLight": "",
        "screenshotDark": "",
        "status": status,
        "buildLogs": logs,
        "buildDuration": 0,
        "providerRepositoryName": "",
        "providerRepositoryOwner": "",
        "providerRepositoryUrl": "",
        "providerCommitHash": "",
        "providerCommitAuthorUrl": "",
        "providerCommitAuthor": "",
        "providerCommitMessage": "",
        "providerCommitUrl": "",
        "providerBranch": "",
        "providerBranchUrl": "",
        **values,
    }


def function(id: str) -> dict[str, Any]:
    """A ``Function`` model."""
    stamp = _stamp()
    return {
        "$id": id,
        "$createdAt": stamp,
        "$updatedAt": stamp,
        "execute": ["any"],
        "name": id.replace("-", " ").title(),
        "enabled": True,
        "live": True,
        "logging": True,
        "runtime": "node-22",
        "deploymentRetention": 0,
        "deploymentId": "",
        "deploymentCreatedAt": "",
        "latestDeploymentId": "",
        "latestDeploymentCreatedAt": "",
        "latestDeploymentStatus": "",
        "scopes": [],
        "vars": [],
        "events": [],
        "schedule": "",
        "timeout": 15,
        "entrypoint": "index.js",
        "commands": "",
        "version": "v5",
        "installationId": "",
        "providerRepositoryId": "",
        "providerBranch": "",
        "providerRootDirectory": "",
        "providerSilentMode": False,
        "providerBranches": [],
        "providerPaths": [],
        "buildSpecification": "s-1vcpu-512mb",
        "runtimeSpecification": "s-1vcpu-512mb",
    }


def execution(
    id: str, function: str, status: str = "completed", **values: Any
) -> dict[str, Any]:
    """An ``Execution`` model of a Function."""
    stamp = _stamp()
    return {
        "$id": id,
        "$createdAt": stamp,
        "$updatedAt": stamp,
        "$permissions": [],
        "resourceId": function,
        "resourceType": "functions",
        "deploymentId": "dep_new",
        "trigger": "http",
        "status": status,
        "requestMethod": "GET",
        "requestPath": "/",
        "requestHeaders": [],
        "responseStatusCode": 200 if status == "completed" else 0,
        "responseBody": "",
        "responseHeaders": [],
        "logs": "",
        "errors": "",
        "duration": 0.25 if status in ("completed", "failed") else 0,
        **values,
    }


def user(id: str, name: str, email: str, **values: Any) -> dict[str, Any]:
    """A ``User`` document as the Users API returns it, password hash, prefs
    and targets included."""
    stamp = _stamp()
    return {
        "$id": id,
        "$createdAt": stamp,
        "$updatedAt": stamp,
        "name": name,
        "password": "$argon2id$v=19$m=7168,t=5,p=1$c2FsdA$aGFzaA",
        "hash": "argon2",
        "hashOptions": {"type": "argon2", "memory_cost": 7168},
        "registration": stamp,
        "status": True,
        "labels": [],
        "passwordUpdate": stamp,
        "email": email,
        "phone": "",
        "emailVerification": False,
        "phoneVerification": False,
        "mfa": False,
        "prefs": {"theme": "dark"},
        "targets": [
            {
                "$id": "target",
                "$createdAt": stamp,
                "$updatedAt": stamp,
                "name": "",
                "userId": id,
                "providerId": None,
                "providerType": "email",
                "identifier": email,
                "expired": False,
            }
        ],
        "accessedAt": "",
        **values,
    }


def bucket(id: str) -> dict[str, Any]:
    """A ``Bucket`` model."""
    stamp = _stamp()
    return {
        "$id": id,
        "$createdAt": stamp,
        "$updatedAt": stamp,
        "$permissions": [],
        "fileSecurity": False,
        "name": id.title(),
        "enabled": True,
        "maximumFileSize": 5_000_000,
        "allowedFileExtensions": ["jpg", "png", "pdf"],
        "compression": "none",
        "encryption": True,
        "antivirus": True,
        "transformations": True,
        "totalSize": 0,
    }


def file(id: str, bucket: str, name: str, mime: str, size: int) -> dict[str, Any]:
    """A ``File`` model."""
    stamp = _stamp()
    return {
        "$id": id,
        "bucketId": bucket,
        "$createdAt": stamp,
        "$updatedAt": stamp,
        "$permissions": ['read("any")'],
        "name": name,
        "folder": "",
        "key": name,
        "signature": "5d41402abc4b2a76b9719d911017c592",
        "mimeType": mime,
        "sizeOriginal": size,
        "sizeActual": size,
        "chunksTotal": 1,
        "chunksUploaded": 1,
        "encryption": True,
        "compression": "none",
    }


def session(id: str, user: str, **values: Any) -> dict[str, Any]:
    """A ``Session`` model, provider tokens and secret included."""
    stamp = _stamp()
    return {
        "$id": id,
        "$createdAt": stamp,
        "$updatedAt": stamp,
        "userId": user,
        "expire": stamp,
        "provider": "email",
        "providerUid": "",
        "providerAccessToken": "provider-access-token",
        "providerAccessTokenExpiry": "",
        "providerRefreshToken": "provider-refresh-token",
        "ip": "203.0.113.7",
        "osCode": "MAC",
        "osName": "Mac",
        "osVersion": "15.1",
        "clientType": "browser",
        "clientCode": "CH",
        "clientName": "Chrome",
        "clientVersion": "131.0",
        "clientEngine": "Blink",
        "clientEngineVersion": "131.0",
        "deviceName": "desktop",
        "deviceBrand": "Apple",
        "deviceModel": "",
        "countryCode": "de",
        "countryName": "Germany",
        "current": False,
        "factors": ["password"],
        "secret": "session-secret",
        "mfaUpdatedAt": "",
        **values,
    }


def _check(payload: Mapping[str, Any], secret: str | None) -> None:
    """Appwrite's parameter validators for a webhook write."""
    if len(payload.get("name", "")) > 128:
        raise _Refusal(400, "general_argument_invalid", "Invalid name")
    if len(payload.get("authPassword") or "") > AUTH_PASSWORD_MAX:
        raise _Refusal(400, "general_argument_invalid", "Invalid authPassword")
    if secret is not None and not 8 <= len(secret) <= 256:
        raise _Refusal(400, "general_argument_invalid", "Invalid secret")


def _model(id: str, document: Mapping[str, Any], *, secret: bool = False) -> Any:
    """The Webhook response model: the password is never returned, the
    signing key only by create and the secret update."""
    return {
        "$id": id,
        "$createdAt": document["$createdAt"],
        "$updatedAt": document["$updatedAt"],
        "name": document["name"],
        "url": document["url"],
        "events": document["events"],
        "tls": document["tls"],
        "authUsername": document["authUsername"],
        "authPassword": "",
        "secret": document["signatureKey"] if secret else "",
        "enabled": document["enabled"],
        "logs": document["logs"],
        "attempts": document["attempts"],
    }
