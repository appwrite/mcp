"""Per-call Appwrite request headers: the project environment a call acts in
and the reason it gives.

An environment of an Appwrite Cloud project is reached with the project's ID in
``X-Appwrite-Project`` and the environment's ID in ``X-Appwrite-Environment``;
the environment's own project ID is internal and refused. Environments live in
their project's region, so region routing keeps using the project's ID.

``X-Appwrite-Reason`` says why a request was made. Appwrite records it on the
project's audit log and shows it in the Activity tab next to the change. The
header value is percent-decoded, so it is sent encoded like
``encodeURIComponent``: any language, and a literal ``%`` as ``%25``. Appwrite
refuses a reason longer than 256 characters or one with control characters;
:func:`reason_error` checks the same rules first, so the model is told how to
fix it without a request being made.

``appwrite-console`` 0.15 has no ``set_environment``. :func:`set_environment`
does what the generated setter does (a global header plus the ``environment``
config key), so it can be replaced by the SDK's once one ships.
"""

from __future__ import annotations

import copy
from urllib.parse import quote

from appwrite_console.client import Client

ENVIRONMENT_HEADER = "x-appwrite-environment"
REASON_HEADER = "x-appwrite-reason"

REASON_LENGTH = 256
"""``APP_LIMIT_AUDIT_REASON_LENGTH`` in Appwrite."""


def set_environment(client: Client, environment_id: str) -> Client:
    """Make every request ``client`` sends act in ``environment_id``."""
    client.add_header(ENVIRONMENT_HEADER, environment_id)
    client._config["environment"] = environment_id
    return client


def environment(client: Client) -> str | None:
    """The environment ``client`` acts in, if any."""
    return client.get_config("environment") or None


def scoped(
    client: Client, *, environment_id: str | None = None, reason: str | None = None
) -> Client:
    """``client``, or a copy of it that also sends the environment and the
    reason. A copy, so a reason never outlives its call on a client shared
    across calls (the stdio client)."""
    if not environment_id and not reason:
        return client
    scoped_client = copy.copy(client)
    scoped_client._global_headers = dict(client._global_headers)
    scoped_client._config = dict(client._config)
    if environment_id:
        set_environment(scoped_client, environment_id)
    if reason:
        scoped_client.add_header(REASON_HEADER, encode_reason(reason))
    return scoped_client


def encode_reason(reason: str) -> str:
    """Percent-encode like ``encodeURIComponent``, which leaves ``!'()*``
    alone (``quote`` already leaves ``-_.~``). Spaces become ``%20``: the
    server decodes with ``rawurldecode``, which leaves a ``+`` as it is."""
    return quote(reason, safe="!'()*")


def reason_error(reason: str) -> str | None:
    """Why Appwrite would refuse ``reason``, or ``None`` when it would not.

    Mirrors ``Appwrite\\Event\\Validator\\Reason``: one line of UTF-8 text, no
    control characters, and none of the bidirectional embedding, override and
    isolate controls that can reorder how it reads."""
    try:
        reason.encode("utf-8")
    except UnicodeEncodeError:
        return "reason must be valid UTF-8 text."
    if len(reason) > REASON_LENGTH:
        return (
            f"reason must be at most {REASON_LENGTH} characters; "
            f"it has {len(reason)}. Shorten it."
        )
    if any(_refused(character) for character in reason):
        return (
            "reason must be one line of text without control characters "
            "(line breaks, tabs, terminal escapes or bidirectional controls)."
        )
    return None


def _refused(character: str) -> bool:
    codepoint = ord(character)
    return (
        codepoint < 0x20  # C0 controls, line feed and tab among them
        or 0x7F <= codepoint <= 0x9F  # DEL and C1 controls
        or codepoint in (0x2028, 0x2029)  # line and paragraph separators
        or 0x202A <= codepoint <= 0x202E  # bidirectional embeddings and overrides
        or 0x2066 <= codepoint <= 0x2069  # bidirectional isolates
    )
