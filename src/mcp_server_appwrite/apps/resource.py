"""The Appwrite view's UI resource, ``ui://appwrite/view``.

One HTML document (``assets/view.html``, plain JavaScript with no build step)
renders every view; it picks one from the tool result's ``structuredContent``.
Hosts cache UI resources by URI, so the tools point at
``ui://appwrite/view/<hash of the HTML>``: a release that changes the view
changes the URI, and hosts fetch it again. The bare ``ui://appwrite/view``
still serves the current HTML for tool lists cached before versioning.
It declares no CSP domains, so hosts apply SEP-1865's restrictive default: no
network access and no external scripts. Every Appwrite read and write it makes
is a ``tools/call`` the host proxies to this server under the user's own
credentials.
"""

from __future__ import annotations

from functools import cache
from hashlib import sha256
from importlib.resources import files
from typing import Any

import mcp.types as types
from mcp.server.lowlevel.helper_types import ReadResourceContents

from ..constants import VIEW_URI
from .protocol import MIME_TYPE

META: dict[str, Any] = {"ui": {"csp": {}, "prefersBorder": True}}


VERSION_LENGTH = 12
"""Hex digits of the HTML's SHA-256 in the URI: enough that two releases
never collide."""


@cache
def html() -> str:
    return files("mcp_server_appwrite.assets").joinpath("view.html").read_text()


@cache
def uri() -> str:
    """The URI the tools point at, versioned by the HTML it serves."""
    digest = sha256(html().encode("utf-8")).hexdigest()[:VERSION_LENGTH]
    return f"{VIEW_URI}/{digest}"


def serves(requested: str) -> bool:
    return requested in (uri(), VIEW_URI)


def resource() -> types.Resource:
    return types.Resource(
        uri=uri(),
        name="Appwrite view",
        description=(
            "Interactive Appwrite view: table rows, deployment build logs, "
            "function executions, Storage files, Auth users and project usage."
        ),
        mime_type=MIME_TYPE,
        size=len(html().encode("utf-8")),
        _meta=META,
    )


def read() -> ReadResourceContents:
    return ReadResourceContents(html(), MIME_TYPE, META)
