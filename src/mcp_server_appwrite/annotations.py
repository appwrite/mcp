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

A separate bucket — :func:`annotations_for_gateway` — exists for tools that
*dispatch* to other tools (e.g. ``appwrite_call_tool``). A gateway cannot
honestly advertise a static safety profile because the destructive capability
of every call depends on which sub-tool is invoked at runtime, which the
gateway decides from caller-supplied input. The right move per the MCP
2025-06-18 spec is to leave ``destructiveHint`` unset (``None``): MCP's
default for unset ``destructiveHint`` is ``true``, so clients that gate
human approval on this hint will prompt the user — exactly matching the
runtime ``confirm_write=true`` boundary enforced inside the gateway.

Why ``destructiveHint=None`` is not equivalent to ``destructiveHint=False``:

- ``destructiveHint=False`` is a *promise* ("I guarantee this tool is
  non-destructive"). For a gateway that promise is false: dispatches can be
  destructive.
- ``destructiveHint=None`` (unset) tells clients "I cannot promise anything;
  apply your host policy" — the only honest answer for a dynamic dispatcher.

Flagged P1 by Greptile on appwrite/mcp#87 (operator.py:263) and fixed here.
"""

from __future__ import annotations

import mcp.types as types

from .constants import READ_VERBS, VERBS

__all__ = [
    "annotations_for_classification",
    "annotations_for_gateway",
    "classify_tool_name",
]


def annotations_for_gateway() -> types.ToolAnnotations:
    """Return the canonical ToolAnnotations for a gateway / dispatcher tool.

    A gateway tool (e.g. ``appwrite_call_tool``) cannot honestly advertise a
    static safety profile because the destructive capability depends on the
    sub-tool chosen at runtime, which the gateway decides from caller-supplied
    input. There is no single read/write/delete answer that covers every call.

    The MCP 2025-06-18 spec treats an unset ``destructiveHint`` as ``True`` by
    default; clients that use the hint to gate human approval will therefore
    prompt the user for every gateway call — matching the runtime
    ``confirm_write=true`` boundary enforced inside the gateway. Clients that
    ignore the hint see no change in behaviour.

    Why not reuse ``annotations_for_classification("unknown")``:

    - ``unknown`` is for verb-bucket tools whose classification we genuinely
      cannot derive (``health``, ``graphql``, custom helpers, etc.). Their
      ``destructiveHint=False`` is a conservative "we have no evidence the verb
      is destructive" claim.
    - A gateway makes no such claim: dispatch outcomes are read, write *or*
      delete. ``destructiveHint=None`` (unset) is the honest signal "the host
      should apply its policy" rather than "we vouch for non-destructiveness".

    Flagged P1 by Greptile on appwrite/mcp#87 — leaving the hint unset is the
    correct resolution. Marked by Greptile as "destructiveHint is a
    Promise; gate must default to true via spec default".
    """
    return types.ToolAnnotations(
        title=None,
        # A gateway can dispatch any read/write/delete sub-call, so it is by
        # definition not read-only at the tool level.
        readOnlyHint=False,
        # UNSET on purpose: spec default is True, clients gate approval on it,
        # and any explicit `True`/`False` would be a false promise either way.
        # Using `None` here is the semantic equivalent of "I don't know; apply
        # host policy" — see module docstring for the full rationale.
        destructiveHint=None,
        # Gateway calls are not idempotent in general: the dispatched sub-tool
        # may mutate state, and re-dispatching is at least as observable as
        # dispatching once.
        idempotentHint=False,
        # The gateway passes through to live Appwrite Cloud / self-hosted APIs.
        openWorldHint=True,
    )


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
