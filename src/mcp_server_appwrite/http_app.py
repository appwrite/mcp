"""Hosted Streamable-HTTP transport for the Appwrite MCP.

Builds a single-tenant Starlette ASGI app for the served Appwrite project:

* ``/`` — the primary MCP Streamable-HTTP endpoint, gated by a bearer-token check
  that returns an RFC 9728 ``WWW-Authenticate`` challenge when unauthenticated.
* ``/mcp`` — a conventional direct alias for the same MCP endpoint.
* ``/.well-known/oauth-protected-resource`` and its ``/mcp`` path-insertion
  variant — RFC 9728 protected resource metadata for their matching endpoints.
* ``/.well-known/oauth-authorization-server`` — backwards-compatibility shim for
  clients on the 2025-03-26 MCP authorization spec (e.g. Raycast), which expect
  authorization-server metadata at the MCP server's own origin instead of
  following ``resource_metadata`` from the 401 challenge. Mirrors the Appwrite
  authorization server's discovery document verbatim.
* ``/healthz`` — liveness probe.

Auth uses the SDK primitives (``BearerAuthBackend`` + ``AuthContextMiddleware``) so the
validated token is reachable from tool handlers via ``get_access_token()``.
"""

from __future__ import annotations

import contextlib
import copy
import itertools
import json
import logging
import re
import sys
from importlib.resources import files
from urllib.parse import urlsplit

import httpx
from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser, BearerAuthBackend
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import error_monitoring, telemetry
from .auth import (
    AppwriteTokenVerifier,
    authorization_server_metadata,
    canonical_resource,
    console_url,
    mcp_path_resource,
    protected_resource_metadata,
    proxied_authorization_server_metadata,
    public_base_url,
    resource_metadata_url,
)
from .constants import CORS_HEADERS, SERVER_VERSION
from .server import (
    build_catalog_tools_manager,
    build_mcp_server,
    build_operator,
)


class HealthzAccessLogFilter(logging.Filter):
    """Drop noisy load-balancer health probes from uvicorn access logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            method = args[1]
            path = str(args[2]).split("?", 1)[0]
            return not (method == "GET" and path == "/healthz")

        return "GET /healthz HTTP/" not in record.getMessage()


def _log(message: str) -> None:
    print(f"[appwrite-mcp][http] {message}", file=sys.stderr, flush=True)


def _is_valid_scope_token(scope: str) -> bool:
    """RFC 6749 §3.3 scope-token grammar: 1*( %x21 / %x23-5B / %x5D-7E ) —
    printable ASCII minus space, double quote, and backslash."""
    return bool(scope) and all(
        char == "\x21" or "\x23" <= char <= "\x5b" or "\x5d" <= char <= "\x7e"
        for char in scope
    )


def _icon_url() -> str:
    return f"{public_base_url()}/favicon.svg"


def _icon_link_header() -> str:
    return f'<{_icon_url()}>; rel="icon"; type="image/svg+xml"'


def _icon_svg() -> bytes:
    return files("mcp_server_appwrite.assets").joinpath("favicon.svg").read_bytes()


async def _send_401(send: Send, resource: str | None = None) -> None:
    """RFC 9728 §5.1 — 401 with a WWW-Authenticate pointing to resource metadata."""
    resource = resource or canonical_resource()
    metadata_url = resource_metadata_url(resource)
    parts = [
        'error="invalid_token"',
        'error_description="Authentication required"',
        f'resource_metadata="{metadata_url}"',
    ]
    # MCP spec 2025-11-25 (SEP-835): clients treat the `scope` parameter as
    # authoritative for what to request. Sourced from the same discovery-backed
    # metadata as /.well-known; omitted entirely if discovery is unavailable —
    # an unauthenticated 401 must never turn into a 500. The values land inside
    # a quoted-string header, so anything outside the RFC 6749 §3.3 scope-token
    # grammar (quotes, backslashes, control characters such as CRLF) is dropped
    # rather than injected into the response head.
    try:
        scopes = (await protected_resource_metadata(resource=resource)).get(
            "scopes_supported"
        ) or []
        scopes = [
            scope
            for scope in scopes
            if isinstance(scope, str) and _is_valid_scope_token(scope)
        ]
        if scopes:
            parts.append(f'scope="{" ".join(scopes)}"')
    except Exception:
        pass
    www_authenticate = f"Bearer {', '.join(parts)}"

    body = json.dumps(
        {"error": "invalid_token", "error_description": "Authentication required"}
    ).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"www-authenticate", www_authenticate.encode()),
        (b"link", _icon_link_header().encode()),
    ]
    await send({"type": "http.response.start", "status": 401, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class RequireBearer:
    """ASGI gate: require an authenticated user for MCP endpoint requests.

    Scope enforcement is delegated to the Appwrite REST API (per-route scope checks
    against the token's granted scopes), so the gate only requires a valid token.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover
            await self.app(scope, receive, send)
            return

        if scope.get("method") == "OPTIONS":
            await self._preflight(send)
            return

        user = scope.get("user")
        if not isinstance(user, AuthenticatedUser):
            # A token was presented and rejected — this session never completes
            # its MCP handshake. Unauthenticated discovery probes (no token at
            # all) are part of the normal OAuth flow and are not counted as
            # handshake failures.
            if _has_authorization_header(scope):
                telemetry.record_handshake_failure(reason="invalid_token")
            resource = (
                mcp_path_resource()
                if scope.get("path") == "/mcp"
                else canonical_resource()
            )
            await _send_401(send, resource)
            return

        await self.app(scope, receive, send)

    async def _preflight(self, send: Send) -> None:
        headers = [(k.lower().encode(), v.encode()) for k, v in CORS_HEADERS.items()]
        await send({"type": "http.response.start", "status": 204, "headers": headers})
        await send({"type": "http.response.body", "body": b""})


def _has_authorization_header(scope: Scope) -> bool:
    for name, _value in scope.get("headers", []):
        if name == b"authorization":
            return True
    return False


_MAX_SNIFF_BYTES = 2 * 1024 * 1024
_UA_PRODUCT = re.compile(r"^[A-Za-z0-9._-]{1,32}")
_connection_counter = itertools.count(1)


def _client_from_user_agent(user_agent: str | None) -> str | None:
    """First product token of the User-Agent, e.g. ``claude-code/2.0`` ->
    ``claude-code``. Only authenticated MCP clients reach this path, so the
    value set stays small."""
    if not user_agent:
        return None
    match = _UA_PRODUCT.match(user_agent.split("/", 1)[0].strip())
    return match.group(0).lower() if match else None


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value.decode("latin-1")
    return None


def _subject_from_scope(scope: Scope) -> str | None:
    user = scope.get("user")
    token = getattr(user, "access_token", None)
    claims = getattr(token, "claims", None) or {}
    return getattr(token, "subject", None) or claims.get("sub")


def _find_initialize_params(body: bytes) -> dict | None:
    try:
        payload = json.loads(body)
    except Exception:
        return None
    messages = payload if isinstance(payload, list) else [payload]
    for message in messages:
        if isinstance(message, dict) and message.get("method") == "initialize":
            params = message.get("params")
            return params if isinstance(params, dict) else {}
    return None


class MCPIdentityMiddleware:
    """Bind client/user identity for every authenticated MCP request.

    The hosted transport is stateless: each POST is its own MCP session, so
    ``session.client_params`` is only populated on the request that carries the
    ``initialize`` message — which the SDK answers internally, before any of our
    handlers run. This middleware peeks at the JSON-RPC body instead: an
    ``initialize`` request is counted as a connection/handshake (with clientInfo),
    and every other request binds identity from the ``mcp-protocol-version``
    header and User-Agent so tool metrics carry a real ``client_id``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        chunks: list[Message] = []
        body = b""
        while True:
            message = await receive()
            chunks.append(message)
            if message["type"] != "http.request":  # pragma: no cover - disconnect
                break
            body += message.get("body", b"")
            if len(body) > _MAX_SNIFF_BYTES or not message.get("more_body", False):
                break

        try:
            self._bind_identity(scope, body)
        except Exception:  # pragma: no cover - telemetry must never break requests
            pass

        replayed = iter(chunks)

        async def replay() -> Message:
            try:
                return next(replayed)
            except StopIteration:
                return await receive()

        await self.app(scope, replay, send)

    def _bind_identity(self, scope: Scope, body: bytes) -> None:
        subject = _subject_from_scope(scope)
        params = _find_initialize_params(body) if body else None
        if params is not None:
            client_info = params.get("clientInfo") or {}
            telemetry.record_connection(
                session_id=next(_connection_counter),
                client_name=client_info.get("name")
                or _client_from_user_agent(_header(scope, b"user-agent")),
                protocol_version=params.get("protocolVersion"),
                subject=subject,
            )
            return
        telemetry.set_request_identity(
            client_name=_client_from_user_agent(_header(scope, b"user-agent")),
            subject=subject,
            protocol_version=_header(scope, b"mcp-protocol-version"),
        )


async def protected_resource_metadata_endpoint(request: Request) -> JSONResponse:
    metadata = await protected_resource_metadata(resource=canonical_resource())
    headers = {**CORS_HEADERS, "Link": _icon_link_header()}
    return JSONResponse(metadata, headers=headers)


async def mcp_path_protected_resource_metadata_endpoint(
    request: Request,
) -> JSONResponse:
    metadata = await protected_resource_metadata(resource=mcp_path_resource())
    headers = {**CORS_HEADERS, "Link": _icon_link_header()}
    return JSONResponse(metadata, headers=headers)


async def authorization_server_metadata_endpoint(request: Request) -> JSONResponse:
    """Legacy discovery shim (MCP authorization spec 2025-03-26).

    Older clients fetch authorization-server metadata from the MCP server's own
    origin rather than the ``authorization_servers`` entry in the protected
    resource metadata. Serve the Appwrite authorization server's discovery
    document verbatim so those clients find the real authorize/token/register
    endpoints (the ``issuer`` inside points at the Appwrite endpoint, not here).

    With a console override active (``MCP_CONSOLE_URL``), this document is also
    the primary discovery path: the protected resource metadata names this MCP
    server as the authorization server, and the mirrored document points the
    authorize step at the local proxy so login/consent happens on the override
    console.
    """
    metadata = await authorization_server_metadata()
    if console_url():
        metadata = proxied_authorization_server_metadata(metadata)
    return JSONResponse(metadata, headers=dict(CORS_HEADERS))


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


async def oauth_authorize_proxy_endpoint(request: Request) -> Response:
    """Authorize proxy for the console override (``MCP_CONSOLE_URL``).

    Forwards the OAuth authorize request to the real Appwrite authorization
    server (which performs client/redirect-URI validation) and rewrites its
    consent-page redirect to the override console, which serves the same
    login/consent flow at ``/oauth2/consent``. Error redirects back to the
    client's ``redirect_uri`` and non-redirect responses pass through verbatim.
    """
    if not console_url():
        return PlainTextResponse("Not Found", status_code=404)

    metadata = await authorization_server_metadata()
    upstream = metadata["authorization_endpoint"]
    if request.url.query:
        upstream = f"{upstream}?{request.url.query}"

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        resp = await client.get(upstream)

    location = resp.headers.get("location")
    if resp.status_code in _REDIRECT_STATUSES and location:
        parts = urlsplit(location)
        if parts.path.rstrip("/").endswith("/oauth2/consent"):
            location = f"{console_url()}/oauth2/consent?{parts.query}"
        return RedirectResponse(location, status_code=303)

    return Response(
        resp.content,
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
    )


async def health_endpoint(request: Request) -> PlainTextResponse:
    return PlainTextResponse(f"appwrite-mcp {SERVER_VERSION} ok")


async def favicon_svg_endpoint(request: Request) -> Response:
    return Response(
        _icon_svg(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def favicon_ico_endpoint(request: Request) -> RedirectResponse:
    return RedirectResponse("/favicon.svg", status_code=307)


def build_app() -> Starlette:
    error_monitoring.init_error_monitoring("http", SERVER_VERSION)
    telemetry.init_telemetry("http", SERVER_VERSION)
    tools_manager = build_catalog_tools_manager()
    operator = build_operator(tools_manager, store_results=False)
    server = build_mcp_server(operator, transport="http")

    # Streamable HTTP with SSE responses (the MCP SDK/ecosystem default). Stateless,
    # so each request opens and closes its own short-lived stream — no session to pin.
    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=True,
    )

    async def handle_mcp(scope: Scope, receive: Receive, send: Send) -> None:
        await session_manager.handle_request(scope, receive, send)

    mcp_endpoint = RequireBearer(MCPIdentityMiddleware(handle_mcp))

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with session_manager.run():
            _log(f"Appwrite MCP (Streamable HTTP) ready — v{SERVER_VERSION}")
            yield

    routes = [
        Route(
            "/.well-known/oauth-protected-resource/mcp",
            endpoint=mcp_path_protected_resource_metadata_endpoint,
            methods=["GET", "OPTIONS"],
        ),
        Route(
            "/.well-known/oauth-protected-resource",
            endpoint=protected_resource_metadata_endpoint,
            methods=["GET", "OPTIONS"],
        ),
        # Legacy 2025-03-26 MCP clients (e.g. Raycast) discover the
        # authorization server at the MCP origin itself.
        Route(
            "/.well-known/oauth-authorization-server",
            endpoint=authorization_server_metadata_endpoint,
            methods=["GET", "OPTIONS"],
        ),
        # OIDC-style discovery for clients that resolve the advertised
        # authorization server (this origin, when MCP_CONSOLE_URL is set) via
        # openid-configuration instead of oauth-authorization-server.
        Route(
            "/.well-known/openid-configuration",
            endpoint=authorization_server_metadata_endpoint,
            methods=["GET", "OPTIONS"],
        ),
        # Authorize proxy for the console override; 404 unless MCP_CONSOLE_URL
        # is set.
        Route(
            "/oauth2/authorize",
            endpoint=oauth_authorize_proxy_endpoint,
            methods=["GET"],
        ),
        Route("/favicon.svg", endpoint=favicon_svg_endpoint, methods=["GET"]),
        Route("/favicon.ico", endpoint=favicon_ico_endpoint, methods=["GET"]),
        Route("/healthz", endpoint=health_endpoint, methods=["GET"]),
        Route(
            "/",
            endpoint=mcp_endpoint,
            methods=["GET", "POST", "DELETE", "OPTIONS"],
        ),
        Route(
            "/mcp",
            endpoint=mcp_endpoint,
            methods=["GET", "POST", "DELETE", "OPTIONS"],
        ),
    ]

    middleware = [
        Middleware(
            AuthenticationMiddleware, backend=BearerAuthBackend(AppwriteTokenVerifier())
        ),
        Middleware(AuthContextMiddleware),
    ]

    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


def run_http(*, host: str = "0.0.0.0", port: int = 8000) -> None:
    import uvicorn
    from uvicorn.config import LOGGING_CONFIG

    app = build_app()
    log_config = copy.deepcopy(LOGGING_CONFIG)
    log_config.setdefault("filters", {})["hide_healthz"] = {
        "()": "mcp_server_appwrite.http_app.HealthzAccessLogFilter"
    }
    log_config["handlers"]["access"]["filters"] = ["hide_healthz"]

    _log(f"Serving on http://{host}:{port}/  (also available at /mcp)")
    uvicorn.run(app, host=host, port=port, log_config=log_config)
