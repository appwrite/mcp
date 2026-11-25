"""Asking the user, not the model, before a delete or a paid change runs.

``appwrite_call_write_tool`` takes ``confirm_write=true``, but the model sets
that flag itself. For deletes and paid changes the server also asks the person,
through MCP elicitation, when the client can show a form.

This uses the 2026-07-28 multi-round-trip flow, which needs no session and so
works on the stateless hosted transport: the first ``tools/call`` gets an
``InputRequiredResult`` holding a form-mode ``elicitation/create``, the client
shows it, then retries the same call with the answer in ``inputResponses``.
The answer is written by the client, never by the model, so a model cannot
approve its own call. Clients that do not declare form elicitation on
2026-07-28 keep the ``confirm_write`` flow alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import mcp.types as types
from mcp.server import ServerRequestContext
from mcp.types.version import MODERN_PROTOCOL_VERSIONS

INPUT_KEY = "confirm"
"""Key of the confirmation in ``inputRequests`` and ``inputResponses``."""


def supported(context: ServerRequestContext[Any, Any]) -> bool:
    """Whether the client behind ``context`` can show a confirmation form."""
    if context.protocol_version not in MODERN_PROTOCOL_VERSIONS:
        return False
    meta = context.meta
    if not isinstance(meta, Mapping):
        return False
    capabilities = meta.get(types.CLIENT_CAPABILITIES_META_KEY)
    if not isinstance(capabilities, Mapping):
        return False
    elicitation = capabilities.get("elicitation")
    if not isinstance(elicitation, Mapping):
        return False
    # An empty object declares form mode, for backwards compatibility.
    return "form" in elicitation or "url" not in elicitation


def request(message: str) -> types.InputRequiredResult:
    """Ask the user to approve the call described by ``message``."""
    return types.InputRequiredResult(
        input_requests={
            INPUT_KEY: types.ElicitRequest(
                params=types.ElicitRequestFormParams(
                    message=message,
                    requested_schema={"type": "object", "properties": {}},
                )
            )
        }
    )


def answer(params: types.CallToolRequestParams) -> str | None:
    """The user's answer carried by a retried call: ``accept``, ``decline`` or
    ``cancel``. ``None`` when the call carries none yet."""
    responses = params.input_responses or {}
    response = responses.get(INPUT_KEY)
    if isinstance(response, types.ElicitResult):
        return response.action
    return None
