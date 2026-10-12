"""JSON-RPC errors for the MCP Events methods.

The events specification is still a draft and its error codes are moving: the
values below are the ones ChatGPT and the working-group design sketch use today.
SEP-3415 renumbers them, so every code lives behind a constant and switching is
a one-line edit per code:

=====================  ===================  ==================
Meaning                ChatGPT / sketch     SEP-3415 (draft)
=====================  ===================  ==================
Bad arguments          -32602               -32602
NotFound               -32011               -32023
Forbidden              -32012               -32024
ResourceExhausted      -32013               -32025
Unsupported            -32014               -32026
CallbackEndpointError  -32015               -32027
=====================  ===================  ==================

Handlers raise :class:`EventsError`. It is an ``MCPError``, so the SDK's
dispatcher turns it into the JSON-RPC ``error`` member (code, message, data)
without any extra plumbing.
"""

from __future__ import annotations

from enum import StrEnum

from mcp import MCPError

# Malformed arguments, callback URL or secret: the JSON-RPC code, unchanged by
# SEP-3415. Re-exported so callers take every events code from this module.
from mcp.types import INVALID_PARAMS

__all__ = [
    "CALLBACK_ENDPOINT",
    "FORBIDDEN",
    "INVALID_PARAMS",
    "NOT_FOUND",
    "RESOURCE_EXHAUSTED",
    "UNSUPPORTED",
    "CallbackFailure",
    "EventsError",
]

NOT_FOUND = -32011
"""Unknown event name or target resource; ``data.kind`` names which. SEP-3415: -32023."""

FORBIDDEN = -32012
"""The caller may not watch the target resource. SEP-3415: -32024."""

RESOURCE_EXHAUSTED = -32013
"""A quota was hit; ``data.limit`` and ``data.max`` describe it. SEP-3415: -32025."""

UNSUPPORTED = -32014
"""A delivery mode or option this server does not offer. SEP-3415: -32026."""

CALLBACK_ENDPOINT = -32015
"""The callback URL failed verification; ``data.reason`` says how. SEP-3415: -32027."""


class CallbackFailure(StrEnum):
    """``data.reason`` values for :data:`CALLBACK_ENDPOINT` errors."""

    CHALLENGE_FAILED = "challenge_failed"
    TIMEOUT = "timeout"
    CONNECTION_REFUSED = "connection_refused"
    TLS_ERROR = "tls_error"
    HTTP_4XX = "http_4xx"
    HTTP_5XX = "http_5xx"


class EventsError(MCPError):
    """A JSON-RPC error raised by an events method.

    Prefer the named constructors: they keep each code paired with the
    ``data`` shape the specification expects for it.
    """

    @classmethod
    def invalid_params(cls, message: str) -> EventsError:
        return cls(INVALID_PARAMS, message)

    @classmethod
    def not_found(cls, message: str, *, kind: str) -> EventsError:
        return cls(NOT_FOUND, message, {"kind": kind})

    @classmethod
    def forbidden(cls, message: str) -> EventsError:
        return cls(FORBIDDEN, message)

    @classmethod
    def resource_exhausted(
        cls, message: str, *, limit: str, maximum: int
    ) -> EventsError:
        return cls(RESOURCE_EXHAUSTED, message, {"limit": limit, "max": maximum})

    @classmethod
    def unsupported(cls, message: str) -> EventsError:
        return cls(UNSUPPORTED, message)

    @classmethod
    def callback_endpoint(cls, message: str, *, reason: CallbackFailure) -> EventsError:
        return cls(CALLBACK_ENDPOINT, message, {"reason": reason.value})
