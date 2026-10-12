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
* **Methods.** ``events/list`` is registered with ``Server.add_request_handler``;
  the SDK adds ``resultType: "complete"`` to custom-method results on the
  2026-07-28 protocol.

Events are served only on the hosted HTTP transport and only behind the
``events`` flag (``MCP_EVENTS``); see :func:`enabled`.
"""

from __future__ import annotations

from typing import Any

from mcp.server import Server, ServerRequestContext
from mcp.server.context import CallNext, HandlerResult
from mcp.types import RequestParams
from pydantic import ConfigDict

from .. import flags
from .catalog import EVENTS, Event
from .errors import EventsError

CAPABILITY = "events"
"""Key under ``capabilities`` that ChatGPT reads."""

EXTENSION = "io.modelcontextprotocol/events"
"""SEP-3415 extension identifier under ``capabilities.extensions``."""

EXTENSION_SETTINGS: dict[str, Any] = {"listChanged": False}
"""The catalog is static, so there is never a list-changed notification."""

DELIVERY_WEBHOOK = "webhook"
"""The only delivery mode served (v1 is webhook-only, like ChatGPT)."""

LIST_METHOD = "events/list"

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


def describe(event: Event) -> dict[str, Any]:
    """One ``events/list`` entry."""
    return {
        "name": event.name,
        "description": event.description,
        "delivery": [DELIVERY_WEBHOOK],
        "inputSchema": event.input_schema,
        "payloadSchema": event.payload_schema,
    }


def list_events(cursor: str | None = None) -> dict[str, Any]:
    """The ``events/list`` result. The catalog fits in one page, so a cursor
    can only be one this server never issued."""
    if cursor is not None:
        raise EventsError.invalid_params("Invalid cursor")
    return {"events": [describe(event) for event in EVENTS]}


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


async def _handle_list(
    context: ServerRequestContext[Any, Any], params: ListParams
) -> dict[str, Any]:
    return list_events(params.cursor)


def register(server: Server[Any]) -> None:
    """Advertise events and serve ``events/list`` on ``server``."""
    server.add_request_handler(LIST_METHOD, ListParams, _handle_list)
    server.middleware.append(CapabilityMiddleware())
