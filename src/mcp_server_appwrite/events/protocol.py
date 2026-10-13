"""MCP Events protocol layer on the low-level SDK server.

The ``mcp`` SDK has no events support yet, so this module adds it from the
outside and keeps the workaround in one place:

* **Capability.** ``ServerCapabilities`` drops unknown keys, so ``events``
  never reaches the wire through the SDK (python-sdk#3640). A
  ``Server.middleware`` patches the already-serialized ``server/discover`` (and
  legacy ``initialize``) result instead. It advertises both shapes clients read
  today: ``capabilities.events = {}`` for ChatGPT and
  ``capabilities.extensions["io.modelcontextprotocol/events"]`` for SEP-3415.
  Delete the middleware once the SDK models the capability.
* **Methods.** ``events/list``, ``events/subscribe``, ``events/unsubscribe``
  and, with push on, ``events/stream`` (:mod:`.streams`) are registered with
  ``Server.add_request_handler``; the SDK adds ``resultType: "complete"`` to
  custom-method results on the 2026-07-28 protocol. All but the listing are
  counted like ``tools/call`` (``mcp.messages.received`` /
  ``mcp.jsonrpc.errors``), and failures on our side go to Sentry with the same
  tags; errors the caller caused (bad input, no access, a quota, an
  unreachable callback) are counted but not reported.

Events are served only on the hosted HTTP transport and only behind the
``events`` flag (``MCP_EVENTS``); see :func:`enabled`. Push delivery also needs
the ``events-push`` flag (``MCP_EVENTS_PUSH``) and a bus
(``MCP_EVENTS_REDIS_URL``); see :func:`push_enabled`. ``events/list``
advertises ``push`` only then.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar

from mcp import MCPError
from mcp.server import Server, ServerRequestContext
from mcp.server.context import CallNext, HandlerResult
from mcp.types import RequestParams
from pydantic import ConfigDict

from .. import error_monitoring, flags, telemetry
from .catalog import CATALOG, EVENTS, Event
from .envelope import Mode
from .errors import EventsError
from .ingress import Ingress
from .streams import METHOD as STREAM_METHOD
from .streams import StreamParams, Streams
from .subscriptions import (
    Operation,
    SubscribeParams,
    Subscriptions,
    UnsubscribeParams,
)

CAPABILITY = "events"
"""Key under ``capabilities`` that ChatGPT reads."""

EXTENSION = "io.modelcontextprotocol/events"
"""SEP-3415 extension identifier under ``capabilities.extensions``."""

EXTENSION_SETTINGS: dict[str, Any] = {"listChanged": False}
"""The catalog is static, so there is never a list-changed notification."""

LIST_METHOD = "events/list"
SUBSCRIBE_METHOD = "events/subscribe"
UNSUBSCRIBE_METHOD = "events/unsubscribe"

TRANSPORT = "http"
"""Events need the hosted ingress, so stdio never advertises them."""

CAPABILITY_METHODS = frozenset({"server/discover", "initialize"})
"""Methods whose result carries ``capabilities``: ``server/discover`` on the
2026-07-28 protocol and ``initialize`` on earlier ones."""


class ListParams(RequestParams):
    """``events/list`` params. Unknown members are tolerated, as the draft
    specification may add some."""

    model_config = ConfigDict(extra="allow")

    cursor: str | None = None


def enabled(transport: str) -> bool:
    """Whether this server should advertise and serve events."""
    return transport == TRANSPORT and flags.enabled(flags.EVENTS)


def push_enabled(transport: str, ingress: Ingress) -> bool:
    """Whether this server should also offer push delivery (``events/stream``):
    the events and events-push flags are on and ``ingress`` has a bus."""
    return (
        enabled(transport)
        and flags.enabled(flags.EVENTS_PUSH)
        and ingress.bus is not None
    )


def describe(event: Event, push: bool) -> dict[str, Any]:
    """One ``events/list`` entry."""
    delivery = [Mode.WEBHOOK.value, *([Mode.PUSH.value] if push else [])]
    return {
        "name": event.name,
        "description": event.description,
        "delivery": delivery,
        "inputSchema": event.input_schema,
        "payloadSchema": event.payload_schema,
    }


def list_events(cursor: str | None = None, *, push: bool = False) -> dict[str, Any]:
    """The ``events/list`` result. The catalog fits in one page, so a cursor
    can only be one this server never issued."""
    if cursor is not None:
        raise EventsError.invalid_params("Invalid cursor")
    return {"events": [describe(event, push) for event in EVENTS]}


def advertise(result: dict[str, Any]) -> dict[str, Any]:
    """``result`` with both events capability shapes added. Existing
    capabilities and extensions are kept."""
    capabilities = dict(result.get("capabilities") or {})
    extensions = dict(capabilities.get("extensions") or {})
    extensions[EXTENSION] = dict(EXTENSION_SETTINGS)
    capabilities[CAPABILITY] = {}
    capabilities["extensions"] = extensions
    return {**result, "capabilities": capabilities}


class CapabilityMiddleware:
    """Adds the events capability to discovery results (python-sdk#3640)."""

    # `ctx` matches the SDK's `ServerMiddleware` protocol parameter name.
    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        result = await call_next(ctx)
        if ctx.method in CAPABILITY_METHODS and isinstance(result, dict):
            return advertise(result)
        return result


Identify = Callable[[ServerRequestContext[Any, Any]], Mapping[str, Any]]
"""Sentry tags naming the client behind a request (``mcp.client.name``, …)."""

_Params = TypeVar("_Params", SubscribeParams, UnsubscribeParams, StreamParams)

Handler = Callable[[ServerRequestContext[Any, Any], _Params], Awaitable[dict[str, Any]]]


def _observed(
    method: str,
    operation: Operation,
    handler: Handler[_Params],
    identify: Identify,
    *,
    successes: bool = True,
) -> Handler[_Params]:
    """``handler`` with the telemetry and error reporting ``tools/call`` has.
    Without ``successes`` only errors are counted here: ``events/stream``
    counts its own success once the stream is open."""

    async def handle(
        context: ServerRequestContext[Any, Any], params: _Params
    ) -> dict[str, Any]:
        started = time.monotonic()
        name = params.name
        event = name if isinstance(name, str) and name in CATALOG else None
        try:
            result = await handler(context, params)
        except Exception as error:
            # A bug is reported, and its text never sent to the client.
            failure = (
                error
                if isinstance(error, MCPError)
                else EventsError.internal("Internal error")
            )
            code = failure.code
            telemetry.record_message(
                method,
                "error",
                time.monotonic() - started,
                error_code=code,
                error_message=type(error).__name__,
            )
            telemetry.record_subscription(operation, "error", event, str(code))
            if not (isinstance(error, EventsError) and error.expected):
                _report(error, method, event, params, context, identify)
            if failure is error:
                raise
            raise failure from error
        if successes:
            telemetry.record_message(method, "success", time.monotonic() - started)
        return result

    return handle


def _report(
    error: Exception,
    method: str,
    event: str | None,
    params: SubscribeParams | UnsubscribeParams | StreamParams,
    context: ServerRequestContext[Any, Any],
    identify: Identify,
) -> None:
    arguments = params.arguments if isinstance(params.arguments, Mapping) else {}
    project = arguments.get("project_id")
    try:
        client = dict(identify(context))
    except Exception:
        client = {}
    tags = {
        "mcp.method": method,
        "transport": TRANSPORT,
        **client,
        **({"event.name": event} if event else {}),
        **({"appwrite.project_id": project} if isinstance(project, str) else {}),
    }
    error_monitoring.capture_exception(
        error,
        tags=tags,
        context={"mcp": {"method": method, "event": event, "transport": TRANSPORT}},
        transaction=f"mcp.{method}:{event}" if event else f"mcp.{method}",
    )


def register(
    server: Server[Any],
    subscriptions: Subscriptions,
    identify: Identify,
    streams: Streams | None = None,
) -> None:
    """Advertise events and serve ``events/list``, ``events/subscribe`` and
    ``events/unsubscribe`` on ``server``, and ``events/stream`` when
    ``streams`` is given."""
    push = streams is not None

    async def handle_list(
        context: ServerRequestContext[Any, Any], params: ListParams
    ) -> dict[str, Any]:
        return list_events(params.cursor, push=push)

    async def subscribe(
        context: ServerRequestContext[Any, Any], params: SubscribeParams
    ) -> dict[str, Any]:
        return await subscriptions.subscribe(params)

    async def unsubscribe(
        context: ServerRequestContext[Any, Any], params: UnsubscribeParams
    ) -> dict[str, Any]:
        return await subscriptions.unsubscribe(params)

    server.add_request_handler(LIST_METHOD, ListParams, handle_list)
    server.add_request_handler(
        SUBSCRIBE_METHOD,
        SubscribeParams,
        _observed(SUBSCRIBE_METHOD, Operation.SUBSCRIBE, subscribe, identify),
    )
    server.add_request_handler(
        UNSUBSCRIBE_METHOD,
        UnsubscribeParams,
        _observed(UNSUBSCRIBE_METHOD, Operation.UNSUBSCRIBE, unsubscribe, identify),
    )
    if streams is not None:
        server.add_request_handler(
            STREAM_METHOD,
            StreamParams,
            _observed(
                STREAM_METHOD,
                Operation.STREAM,
                streams.stream,
                identify,
                successes=False,
            ),
        )
    server.middleware.append(CapabilityMiddleware())
