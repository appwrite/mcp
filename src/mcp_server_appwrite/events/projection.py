"""Turn an Appwrite webhook body into the ``data`` of one MCP event.

Appwrite sends the full response model with every webhook: whole rows, users
with their email and phone, executions with request headers and logs. Nothing
from that body is forwarded as is. Each catalog event has a projector here that
reads only the fields its ``payload_schema`` declares, so a new field in an
Appwrite model can never leak to a subscriber.

Projection is also where the Appwrite quirks live:

* Executions name their function in ``resourceId`` with ``resourceType`` set to
  ``functions``. Synchronous runs fire only ``.create``, already finished;
  asynchronous runs fire ``.create`` while ``waiting`` and ``.update`` when they
  finish. The status filter keeps only failures, so a waiting ``.create`` drops.
* Deployments are delivered only in a terminal status. Activating a deployment
  or duplicating one also fires ``deployments.*.update``; activation carries the
  function or site model instead of a deployment, which is detected by shape
  (no ``resourceType``), and a duplicate is still ``waiting``.
* Timestamps arrive either in the response-model format or as raw database
  values without a zone (``2026-10-09 09:38:26.634``, which is UTC), and are
  sometimes missing. :func:`timestamp` normalizes them to ISO 8601 UTC or
  ``None``.

Every resource ID in the body is checked against the subscription's sealed
arguments. Appwrite already filters by event pattern, but the webhook lives in
the user's project and its events can be edited there; the check keeps a
subscription pinned to the resource it was authorized for.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from .catalog import Event
from .errors import EventsError


class Drop(StrEnum):
    """Why the ingress acknowledged an Appwrite delivery without forwarding it.

    Every drop answers ``200``: Appwrite pauses a webhook after repeated
    failures and emails the project owner, and none of these is the owner's
    fault."""

    EXPIRED = "expired"
    """The subscription expired and was not refreshed."""
    RETIRED_KEY = "retired_key"
    """Sealed with a key no longer in the ring: an expired subscription whose
    webhook outlived the rotation."""
    UNKNOWN_EVENT = "unknown_event"
    """The sealed event name is not in this server's catalog."""
    EVENT_MISMATCH = "event_mismatch"
    """The Appwrite events fired match none of the subscription's patterns."""
    MALFORMED = "malformed"
    """The body is not a JSON object."""
    SHAPE = "shape"
    """The body is not the model the event expects, e.g. deployment activation."""
    RESOURCE_MISMATCH = "resource_mismatch"
    """The body names a resource other than the subscribed one."""
    STATUS = "status"
    """The status filter does not accept the body's status."""
    TOO_LARGE = "too_large"
    """The body is larger than the ingress reads."""


class Dropped(Exception):
    """Raised by a projector when the delivery must be acknowledged and dropped."""

    def __init__(self, reason: Drop) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class Projection:
    """The forwarded part of one Appwrite delivery."""

    data: dict[str, Any]
    """Exactly the fields of the event's ``payload_schema``."""
    occurred: str | None
    """When the event happened (ISO 8601 UTC), if the body says."""


def timestamp(value: object) -> str | None:
    """An Appwrite timestamp as ISO 8601 UTC with milliseconds, or ``None``.

    Accepts the response-model format (``2026-10-09T09:38:26.634+00:00``) and
    raw database values without a zone (``2026-10-09 09:38:26.634``), which
    Appwrite stores in UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (
        parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def matches(fired: Sequence[str], patterns: Sequence[str]) -> bool:
    """Whether Appwrite fired one of ``patterns``.

    Appwrite lists every form of the event it fired in
    ``X-Appwrite-Webhook-Events``, wildcard forms included, and sends to a
    webhook when one of the webhook's events is literally among them. The
    subscription's patterns are what the webhook was created with."""
    return not set(patterns).isdisjoint(fired)


def project(
    event: Event,
    arguments: Mapping[str, Any],
    body: Mapping[str, Any],
    fired: Sequence[str],
) -> Projection:
    """Project ``body`` onto ``event`` for a subscription made with
    ``arguments``; raises :class:`Dropped` when it must not be delivered."""
    try:
        patterns = event.patterns(arguments)
    except EventsError as error:
        raise Dropped(Drop.UNKNOWN_EVENT) from error
    if not matches(fired, patterns):
        raise Dropped(Drop.EVENT_MISMATCH)
    projector = PROJECTORS.get(event.name)
    if projector is None:
        raise Dropped(Drop.UNKNOWN_EVENT)
    projection = projector(body, arguments)
    if event.status is not None and not event.status.matches(
        projection.data.get(event.status.field), arguments
    ):
        raise Dropped(Drop.STATUS)
    return projection


def _id(body: Mapping[str, Any], key: str = "$id") -> str:
    """A required ID field; a body without it is not the expected model."""
    value = body.get(key)
    if not isinstance(value, str) or not value:
        raise Dropped(Drop.SHAPE)
    return value


def _pinned(body: Mapping[str, Any], key: str, expected: str) -> str:
    """An optional ID field that must equal the subscribed resource."""
    value = body.get(key, expected)
    if value != expected:
        raise Dropped(Drop.RESOURCE_MISMATCH)
    return expected


def _resource(
    body: Mapping[str, Any], kind: str, arguments: Mapping[str, Any], argument: str
) -> str:
    """The owning resource of an execution or deployment: ``resourceType`` must
    be ``kind`` and ``resourceId`` the subscribed one."""
    if body.get("resourceType") != kind:
        raise Dropped(Drop.SHAPE)
    if _id(body, "resourceId") != arguments.get(argument):
        raise Dropped(Drop.RESOURCE_MISMATCH)
    return arguments[argument]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _execution(body: Mapping[str, Any], arguments: Mapping[str, Any]) -> Projection:
    created = timestamp(body.get("$createdAt"))
    return Projection(
        data={
            "function_id": _resource(body, "functions", arguments, "function_id"),
            "execution_id": _id(body),
            "status": body.get("status"),
            "trigger": _text(body.get("trigger")),
            "response_status_code": _integer(body.get("responseStatusCode")),
            "created_at": created,
        },
        occurred=timestamp(body.get("$updatedAt")) or created,
    )


def _deployment(
    kind: str, argument: str
) -> Callable[[Mapping[str, Any], Mapping[str, Any]], Projection]:
    def projector(body: Mapping[str, Any], arguments: Mapping[str, Any]) -> Projection:
        updated = timestamp(body.get("$updatedAt"))
        return Projection(
            data={
                argument: _resource(body, kind, arguments, argument),
                "deployment_id": _id(body),
                "status": body.get("status"),
                "updated_at": updated,
            },
            occurred=updated,
        )

    return projector


def _row(body: Mapping[str, Any], arguments: Mapping[str, Any]) -> Projection:
    created = timestamp(body.get("$createdAt"))
    return Projection(
        data={
            "database_id": _pinned(body, "$databaseId", arguments["database_id"]),
            "table_id": _pinned(body, "$tableId", arguments["table_id"]),
            "row_id": _id(body),
            "created_at": created,
        },
        occurred=created,
    )


def _file(body: Mapping[str, Any], arguments: Mapping[str, Any]) -> Projection:
    created = timestamp(body.get("$createdAt"))
    return Projection(
        data={
            "bucket_id": _pinned(body, "bucketId", arguments["bucket_id"]),
            "file_id": _id(body),
            "mime_type": _text(body.get("mimeType")),
            "size": _integer(body.get("sizeOriginal")),
            "created_at": created,
        },
        occurred=created,
    )


def _user(body: Mapping[str, Any], arguments: Mapping[str, Any]) -> Projection:
    # Users carry email, phone, name, labels and prefs; only the ID and the
    # creation time are delivered.
    created = timestamp(body.get("$createdAt"))
    return Projection(
        data={"user_id": _id(body), "created_at": created},
        occurred=created,
    )


PROJECTORS: Mapping[
    str, Callable[[Mapping[str, Any], Mapping[str, Any]], Projection]
] = {
    "functions.execution.failed": _execution,
    "functions.deployment.completed": _deployment("functions", "function_id"),
    "sites.deployment.completed": _deployment("sites", "site_id"),
    "tablesdb.row.created": _row,
    "storage.file.created": _file,
    "users.user.created": _user,
}
"""One projector per catalog event, keyed by event name."""
