"""Hosted Streamable-HTTP transport for the Appwrite MCP.

Builds a single-tenant Starlette ASGI app for the served Appwrite project:

* ``/mcp`` — the MCP Streamable-HTTP endpoint, gated by a bearer-token check that
  returns an RFC 9728 ``WWW-Authenticate`` challenge when unauthenticated.
* ``/.well-known/oauth-protected-resource/mcp`` — RFC 9728 protected resource
  metadata pointing at the project's Appwrite OAuth authorization server.
* ``/healthz`` — liveness probe.

Auth uses the SDK primitives (``BearerAuthBackend`` + ``AuthContextMiddleware``) so the
validated token is reachable from tool handlers via ``get_access_token()``.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import sys
from importlib.resources import files

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
from starlette.types import ASGIApp, Receive, Scope, Send

from . import error_monitoring, telemetry
from .auth import (
    AppwriteTokenVerifier,
    protected_resource_metadata,
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


async def _send_401(send: Send) -> None:
    """RFC 9728 §5.1 — 401 with a WWW-Authenticate pointing to resource metadata."""
    metadata_url = resource_metadata_url()
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
        scopes = (await protected_resource_metadata()).get("scopes_supported") or []
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
    """ASGI gate: require an authenticated user for ``/mcp`` requests.

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
            # A presented-but-invalid token was already counted (with its specific
            # reason) by the token verifier. Only count the no-token case here so we
            # don't double-count rejections.
            if not _has_authorization_header(scope):
                telemetry.record_auth(outcome="rejected", reason="missing")
            await _send_401(send)
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


async def protected_resource_metadata_endpoint(request: Request) -> JSONResponse:
    metadata = await protected_resource_metadata()
    headers = {**CORS_HEADERS, "Link": _icon_link_header()}
    return JSONResponse(metadata, headers=headers)


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

    mcp_endpoint = RequireBearer(handle_mcp)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async with session_manager.run():
            _log(f"Appwrite MCP (Streamable HTTP) ready — v{SERVER_VERSION}")
            yield

    routes = [
        Route(
            "/.well-known/oauth-protected-resource/mcp",
            endpoint=protected_resource_metadata_endpoint,
            methods=["GET", "OPTIONS"],
        ),
        Route("/favicon.svg", endpoint=favicon_svg_endpoint, methods=["GET"]),
        Route("/favicon.ico", endpoint=favicon_ico_endpoint, methods=["GET"]),
        Route("/healthz", endpoint=health_endpoint, methods=["GET"]),
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

    _log(f"Serving on http://{host}:{port}  (resource path: /mcp)")
    uvicorn.run(app, host=host, port=port, log_config=log_config)
