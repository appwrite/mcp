"""MCP Apps (SEP-1865) negotiation on the low-level SDK server.

The extension is ``io.modelcontextprotocol/ui``. A host that supports it reads
``_meta.ui.resourceUri`` on a tool, fetches that ``ui://`` resource with
``resources/read`` and renders it in a sandboxed iframe next to the tool call.
Hosts without it ignore ``_meta`` and show the tool's text content, so the
tools stay usable everywhere.

The spec asks servers to offer UI tools only to hosts that declared the
extension. Whether a host did is known on stdio (the ``initialize`` handshake)
and on the 2026-07-28 protocol (``clientCapabilities`` in every request's
``_meta``). The hosted 2025-era transport is stateless, so its ``tools/list``
arrives without either; there the tools are offered, since a host that cannot
render them still gets a text answer. See :func:`supported`.

Apps are offered behind the ``apps`` flag (``MCP_APPS``); see :func:`enabled`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from mcp.server import Server, ServerRequestContext
from mcp.types import CLIENT_CAPABILITIES_META_KEY

from .. import flags

EXTENSION = "io.modelcontextprotocol/ui"
"""SEP-1865 extension identifier under ``capabilities.extensions``."""

MIME_TYPE = "text/html;profile=mcp-app"
"""The only UI resource type the extension defines."""

SETTINGS: dict[str, Any] = {"mimeTypes": [MIME_TYPE]}


def enabled() -> bool:
    """Whether this server offers MCP Apps."""
    return flags.enabled(flags.APPS)


def supported(context: ServerRequestContext[Any, Any]) -> bool:
    """Whether the host behind ``context`` may render MCP Apps: ``False`` only
    when its capabilities are known and lack the extension."""
    capabilities = _capabilities(context)
    if capabilities is None:
        return True
    extensions = capabilities.get("extensions")
    if not isinstance(extensions, Mapping):
        return False
    settings = extensions.get(EXTENSION)
    if not isinstance(settings, Mapping):
        return False
    mime_types = settings.get("mimeTypes")
    return not isinstance(mime_types, list) or MIME_TYPE in mime_types


def register(server: Server[Any]) -> None:
    """Advertise the extension in ``server``'s capabilities."""
    server.extensions[EXTENSION] = dict(SETTINGS)


def _capabilities(
    context: ServerRequestContext[Any, Any],
) -> Mapping[str, Any] | None:
    """The client capabilities of this request's ``_meta`` (2026-07-28) or of
    the session's handshake, ``None`` when neither is known."""
    meta = context.meta
    if isinstance(meta, Mapping):
        capabilities = meta.get(CLIENT_CAPABILITIES_META_KEY)
        if isinstance(capabilities, Mapping):
            return capabilities
    try:
        params = context.session.client_params
    except Exception:
        return None
    if params is None:
        return None
    return params.capabilities.model_dump(by_alias=True, exclude_none=True)
