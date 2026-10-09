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

The parties around the server are real processes on localhost too:

* :class:`Appwrite` plays Appwrite's webhooks worker: it signs and posts
  deliveries exactly as ``src/Appwrite/Platform/Workers/Webhooks.php`` does.
* :class:`Receiver` plays ChatGPT's webhook endpoint: an HTTPS server with a
  self-signed certificate that checks every delivery with the official
  ``standardwebhooks`` library and echoes verification challenges.
* :class:`Collector` is an OTLP/HTTP metrics endpoint. Every server exports to
  it, so tests read the counters the server really emits.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import itertools
import json
import os
import secrets
import socket
import ssl
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
from urllib.parse import urlsplit

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
from standardwebhooks import Webhook as StandardWebhook
from standardwebhooks.webhooks import WebhookVerificationError

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
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE",
    "OTEL_EXPORTER_OTLP_COMPRESSION",
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
        self._environment = {
            "MCP_PUBLIC_URL": PUBLIC_URL,
            "OTEL_EXPORTER_OTLP_ENDPOINT": collector().url,
            **(environment or {}),
        }
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
        config = uvicorn.Config(app, log_level="warning", access_log=False, ws="none")
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
