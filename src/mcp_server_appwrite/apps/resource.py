"""The Appwrite view's UI resource, ``ui://appwrite/view``.

One HTML document (``assets/view.html``, plain JavaScript with no build step)
renders every view; it picks one from the tool result's ``structuredContent``.
It declares no CSP domains, so hosts apply SEP-1865's restrictive default: no
network access and no external scripts. Every Appwrite read and write it makes
is a ``tools/call`` the host proxies to this server under the user's own
credentials.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files
from typing import Any

import mcp.types as types
from mcp.server.lowlevel.helper_types import ReadResourceContents

from ..constants import VIEW_URI
from .protocol import MIME_TYPE

META: dict[str, Any] = {"ui": {"csp": {}, "prefersBorder": True}}


@cache
def html() -> str:
    return files("mcp_server_appwrite.assets").joinpath("view.html").read_text()


def resource() -> types.Resource:
    return types.Resource(
        uri=VIEW_URI,
        name="Appwrite view",
        description=(
            "Interactive Appwrite view: table rows and deployment build logs."
        ),
        mime_type=MIME_TYPE,
        size=len(html().encode("utf-8")),
        _meta=META,
    )


def read() -> ReadResourceContents:
    return ReadResourceContents(html(), MIME_TYPE, META)
