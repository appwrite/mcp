"""MCP ToolAnnotations helpers — derive safety hints from tool name classification.

The MCP 2025-06-18 spec defines five optional `ToolAnnotations` fields:

- ``title``:           human-readable name shown in clients.
- ``readOnlyHint``:    if true, the tool does not modify its environment.
- ``destructiveHint``: if true, the tool may perform destructive changes to its
                       environment. Only meaningful when ``readOnlyHint=false``.
- ``idempotentHint``:  if true, calling the tool repeatedly with the same
                       arguments has the same observable effect as calling it once.
- ``openWorldHint``:   if true, the tool interacts with an open world (e.g. the
                       network, the filesystem, a remote API).

The Appwrite SDK exposes ~25 services with hundreds of methods. The MCP server
hides them behind the operator surface and exposes them on demand. Each tool
name carries an action verb (e.g. ``users_create``, ``storage_delete_bucket``)
that determines whether it is a read, write, delete, or unknown operation.

This module is the **single source of truth** for:

1. ``classify_tool_name(tool_name)`` — extract the verb bucket from a tool name.
2. ``annotations_for_classification(classification)`` — map that bucket to a
   canonical ``ToolAnnotations`` instance with explicit (non-default) hints.

Both helpers are pure and deterministic so they can be tested exhaustively.
Centralizing the mapping avoids the 30-place duplication that invites drift
between the 25 dynamic SDK tools and the 4 public operator tools.

Why every field is set explicitly (instead of relying on defaults):

- MCP defaults for ``destructiveHint`` and ``openWorldHint`` are ``true``.
  For a read tool this default would falsely advertise destructive behavior.
- Some clients (the Appwrite MCP broker, claude-code's safety classifier, the
  Gemini MCP connector) gate prompt flow on these hints; defaults leak false
  positives into the prompt path.
- Inline rationale comments document *why* each hint has its value, so
  reviewers (and future-us) can audit the choice without re-reading the spec.

Why ``unknown`` defaults the way it does:

- We cannot honestly claim ``readOnlyHint=true`` (we don't know it doesn't write).
- We cannot honestly claim ``destructiveHint=true`` either (we have no evidence
  the verb is destructive). Reporting false positives here is worse than
  reporting false negatives — clients use destructiveHint to prompt the user.
- ``idempotentHint=false`` is the only honest choice for an unclassified verb.
- ``openWorldHint=true`` is correct because the Appwrite SDK clearly touches
  the network (Cloud or self-hosted endpoint).
"""

from __future__ import annotations

import mcp.types as types

from .constants import READ_VERBS, VERBS

__all__ = [
    "annotations_for_classification",
    "classify_tool_name",
]


def classify_tool_name(tool_name: str) -> str:
    """Classify an SDK tool name into one of ``"read" | "write" | "delete" | "unknown"``.

    The classification is determined by the first token in the tool name that
    appears in :data:`mcp_server_appwrite.constants.VERBS`. The verb's bucket
    is then mapped:

    - ``list``, ``get``  → ``"read"``
    - ``create``, ``update`` → ``"write"``
    - ``delete``        → ``"delete"``
    - anything else     → ``"unknown"``

    Examples::

        >>> classify_tool_name("users_list")
        'read'
        >>> classify_tool_name("users_create")
        'write'
        >>> classify_tool_name("storage_delete_bucket")
        'delete'
        >>> classify_tool_name("avatars_get_flag")
        'read'
        >>> classify_tool_name("health")
        'unknown'

    The function is case-insensitive on the verb and ignores empty tokens
    (e.g. trailing/leading underscores).
    """
    if not tool_name:
        return "unknown"
    tokens = [token for token in tool_name.lower().split("_") if token]
    verb = next((token for token in tokens if token in VERBS), None)
    if verb is None:
        return "unknown"
    if verb in READ_VERBS:
        return "read"
    if verb in {"create", "update"}:
        return "write"
    if verb == "delete":
        return "delete"
    return "unknown"


def annotations_for_classification(classification: str) -> types.ToolAnnotations:
    """Return the canonical ToolAnnotations for a tool's classification.

    :param classification: one of ``"read" | "write" | "delete" | "unknown"``.
        Any other value falls through to the ``"unknown"`` branch.
    :returns: a ``ToolAnnotations`` instance with every field set explicitly.
    """
    if classification == "read":
        return types.ToolAnnotations(
            # No title: callers (operator.py, service.py) attach the human-
            # readable name from the tool's own description. Keeping it None
            # here avoids forcing every caller to thread a title through.
            title=None,
            # List/get return server-side state; no write side-effects.
            readOnlyHint=True,
            # Never deletes or modifies remote state.
            destructiveHint=False,
            # Same args return same result within the lifetime of a request.
            # Idempotency here means "no extra effect from being called twice",
            # not "snapshot stability across time".
            idempotentHint=True,
            # Calls live Appwrite Cloud APIs (or a self-hosted endpoint).
            openWorldHint=True,
        )
    if classification == "write":
        return types.ToolAnnotations(
            title=None,
            # create/update mutate server-side state.
            readOnlyHint=False,
            # create/update do not remove the underlying resource (delete does).
            destructiveHint=False,
            # Repeated create calls produce *new* resources (different IDs);
            # repeated update calls reach a stable end-state but the
            # server-visible effect of "calling twice" still differs from "once".
            idempotentHint=False,
            # Calls live Appwrite Cloud APIs.
            openWorldHint=True,
        )
    if classification == "delete":
        return types.ToolAnnotations(
            title=None,
            # delete removes a resource — clearly not read-only.
            readOnlyHint=False,
            # Deleting an already-deleted resource is a 404 from the Appwrite
            # SDK, which the runtime caller handles. From the MCP hint
            # perspective the *intent* of the verb is destructive.
            destructiveHint=True,
            # Deleting an already-deleted resource does not produce additional
            # observable change after the first successful delete.
            idempotentHint=True,
            # Calls live Appwrite Cloud APIs.
            openWorldHint=True,
        )
    # "unknown" (and any unrecognised bucket) — verb was not in VERBS or the
    # tool is an Appwrite-specific helper. The most conservative, honest
    # defaults: can't promise read-only, can't promise idempotency, can't
    # claim destructive without evidence, but the call clearly leaves the host.
    return types.ToolAnnotations(
        title=None,
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
