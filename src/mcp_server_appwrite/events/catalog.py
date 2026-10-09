"""Declarative catalog of the MCP events this server can deliver.

Each :class:`Event` maps one MCP event name onto the Appwrite project-webhook
events that feed it. Everything about an event is data:

* ``arguments`` — what a subscriber passes besides the always-required
  ``project_id``. Every value is checked against :data:`ID_PATTERN` (or a fixed
  set of choices) before it is spliced into an Appwrite event pattern, so a
  ``*`` or a ``.`` can never widen a subscription to resources the caller did
  not name.
* ``templates`` — the Appwrite event patterns, with ``{argument}`` placeholders.
* ``payload`` — the fields delivered to the subscriber. IDs and metadata only:
  Appwrite sends whole rows and users, and the agent fetches details itself
  with ``appwrite_call_tool``.
* ``status`` — the payload status filter. Appwrite webhooks filter only by event
  name, so the ingress applies this one before delivering.

The JSON Schemas served by ``events/list`` are derived from those fields, and
:meth:`Event.validate` enforces exactly what the input schema promises.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from .errors import EventsError

ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,35}$"
"""Appwrite IDs as accepted in a subscription: up to 36 characters, starting
with a letter or digit. Stricter than Appwrite itself, which also allows ``.``:
a dot is the separator in Appwrite event names, so it is rejected along with
``*`` to keep every pattern pinned to the named resource."""

_ID = re.compile(ID_PATTERN)

NOT_FOUND_KIND = "event"
"""``data.kind`` of the NotFound error for an unknown event name."""


class Status(StrEnum):
    """Terminal Appwrite statuses an event can be filtered on."""

    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True)
class Argument:
    """One subscription argument.

    Without ``choices`` the value is an Appwrite ID and must match
    :data:`ID_PATTERN`; with ``choices`` it must be one of them.
    """

    name: str
    description: str
    required: bool = True
    choices: tuple[str, ...] = ()

    def schema(self) -> dict[str, Any]:
        if self.choices:
            return {
                "type": "string",
                "enum": [str(choice) for choice in self.choices],
                "description": self.description,
            }
        return {
            "type": "string",
            "pattern": ID_PATTERN,
            "description": self.description,
        }

    def check(self, value: object) -> str:
        """The value, if it is valid for this argument."""
        if isinstance(value, str):
            if self.choices and value in self.choices:
                return value
            if not self.choices and _ID.fullmatch(value):
                return value
        if self.choices:
            expected = " or ".join(self.choices)
            raise EventsError.invalid_params(f"{self.name} must be {expected}")
        raise EventsError.invalid_params(
            f"{self.name} must be an Appwrite ID: 1-36 letters, digits, "
            "'_' or '-', starting with a letter or digit"
        )


@dataclass(frozen=True)
class PayloadField:
    """One field of a delivered event's ``data``."""

    name: str
    type: str
    description: str
    nullable: bool = False
    format: str | None = None
    choices: tuple[str, ...] = ()

    def schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": [self.type, "null"] if self.nullable else self.type,
            "description": self.description,
        }
        if self.format is not None:
            schema["format"] = self.format
        if self.choices:
            choices: list[str | None] = [str(choice) for choice in self.choices]
            schema["enum"] = [*choices, None] if self.nullable else choices
        return schema


@dataclass(frozen=True)
class StatusFilter:
    """Which Appwrite payload statuses qualify an event for delivery.

    ``field`` is the status field in the Appwrite webhook body, ``accepted``
    the statuses that ever qualify, and ``argument`` the optional subscription
    argument that narrows delivery to one of them.
    """

    accepted: tuple[Status, ...]
    field: str = "status"
    argument: str | None = None

    def matches(self, status: object, arguments: Mapping[str, Any]) -> bool:
        """Whether an Appwrite payload with ``status`` should be delivered to a
        subscription made with ``arguments``."""
        if status not in self.accepted:
            return False
        if self.argument is None:
            return True
        wanted = arguments.get(self.argument)
        return wanted is None or wanted == status


PROJECT = Argument("project_id", "Appwrite project ID to watch.")
"""Required by every event; it is never part of the Appwrite pattern because
project webhooks are already scoped to one project."""


@dataclass(frozen=True)
class Event:
    """One deliverable MCP event and the Appwrite events behind it."""

    name: str
    description: str
    templates: tuple[str, ...]
    payload: tuple[PayloadField, ...]
    arguments: tuple[Argument, ...] = ()
    status: StatusFilter | None = None

    @property
    def parameters(self) -> tuple[Argument, ...]:
        """Every argument the event accepts, ``project_id`` first."""
        return (PROJECT, *self.arguments)

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                argument.name: argument.schema() for argument in self.parameters
            },
            "required": [
                argument.name for argument in self.parameters if argument.required
            ],
            "additionalProperties": False,
        }

    @property
    def payload_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {field.name: field.schema() for field in self.payload},
            "required": [field.name for field in self.payload],
            "additionalProperties": False,
        }

    def validate(self, arguments: object) -> dict[str, str]:
        """Check subscription arguments against :attr:`input_schema`.

        Returns the arguments in declaration order; raises an invalid-params
        :class:`EventsError` on anything the schema would reject."""
        if not isinstance(arguments, Mapping):
            raise EventsError.invalid_params("arguments must be an object")
        known = {argument.name for argument in self.parameters}
        unknown = sorted(str(key) for key in arguments if key not in known)
        if unknown:
            raise EventsError.invalid_params(
                f"Unknown arguments for {self.name}: {', '.join(unknown)}"
            )
        validated: dict[str, str] = {}
        for argument in self.parameters:
            if argument.name not in arguments:
                if argument.required:
                    raise EventsError.invalid_params(f"{argument.name} is required")
                continue
            validated[argument.name] = argument.check(arguments[argument.name])
        return validated

    def patterns(self, arguments: object) -> list[str]:
        """The Appwrite webhook event patterns for a subscription."""
        validated = self.validate(arguments)
        return [template.format(**validated) for template in self.templates]


def _timestamp(name: str, description: str) -> PayloadField:
    return PayloadField(name, "string", description, nullable=True, format="date-time")


_DEPLOYMENT_STATUS = Argument(
    "status",
    "Only deliver deployments that end in this status. Omit for both.",
    required=False,
    choices=(Status.READY, Status.FAILED),
)

_DEPLOYMENT_FILTER = StatusFilter(
    accepted=(Status.READY, Status.FAILED), argument=_DEPLOYMENT_STATUS.name
)


def _deployment_payload(resource: str, kind: str) -> tuple[PayloadField, ...]:
    return (
        PayloadField(resource, "string", f"ID of the {kind}."),
        PayloadField("deployment_id", "string", "ID of the deployment."),
        PayloadField(
            "status",
            "string",
            "Final deployment status.",
            choices=(Status.READY, Status.FAILED),
        ),
        _timestamp("updated_at", "When the deployment reached its final status."),
    )


EVENTS: tuple[Event, ...] = (
    Event(
        name="functions.execution.failed",
        description=(
            "A function execution finished with status failed: synchronous, "
            "asynchronous, scheduled or event-triggered runs. Executions served "
            "through function domains are not covered."
        ),
        arguments=(Argument("function_id", "ID of the function to watch."),),
        # Synchronous runs fire only `.create`, after they finish; async,
        # scheduled and event-triggered runs fire `.create` while waiting and
        # `.update` when they finish. The status filter drops everything but
        # failures.
        templates=(
            "functions.{function_id}.executions.*.update",
            "functions.{function_id}.executions.*.create",
        ),
        payload=(
            PayloadField("function_id", "string", "ID of the function."),
            PayloadField("execution_id", "string", "ID of the execution."),
            PayloadField(
                "status", "string", "Execution status.", choices=(Status.FAILED,)
            ),
            PayloadField(
                "trigger",
                "string",
                "What started the execution: http, schedule or event.",
                nullable=True,
            ),
            PayloadField(
                "response_status_code",
                "integer",
                "HTTP status code the function responded with.",
                nullable=True,
            ),
            _timestamp("created_at", "When the execution started."),
        ),
        status=StatusFilter(accepted=(Status.FAILED,)),
    ),
    Event(
        name="functions.deployment.completed",
        description="A function deployment finished building, as ready or failed.",
        arguments=(
            Argument("function_id", "ID of the function to watch."),
            _DEPLOYMENT_STATUS,
        ),
        templates=("functions.{function_id}.deployments.*.update",),
        payload=_deployment_payload("function_id", "function"),
        status=_DEPLOYMENT_FILTER,
    ),
    Event(
        name="sites.deployment.completed",
        description="A site deployment finished building, as ready or failed.",
        arguments=(
            Argument("site_id", "ID of the site to watch."),
            _DEPLOYMENT_STATUS,
        ),
        templates=("sites.{site_id}.deployments.*.update",),
        payload=_deployment_payload("site_id", "site"),
        status=_DEPLOYMENT_FILTER,
    ),
    Event(
        name="tablesdb.row.created",
        description="A row was created in a TablesDB table.",
        arguments=(
            Argument("database_id", "ID of the database."),
            Argument("table_id", "ID of the table to watch."),
        ),
        templates=("tablesdb.{database_id}.tables.{table_id}.rows.*.create",),
        payload=(
            PayloadField("database_id", "string", "ID of the database."),
            PayloadField("table_id", "string", "ID of the table."),
            PayloadField("row_id", "string", "ID of the new row."),
            _timestamp("created_at", "When the row was created."),
        ),
    ),
    Event(
        name="storage.file.created",
        description="A file was uploaded to a storage bucket.",
        arguments=(Argument("bucket_id", "ID of the bucket to watch."),),
        templates=("buckets.{bucket_id}.files.*.create",),
        payload=(
            PayloadField("bucket_id", "string", "ID of the bucket."),
            PayloadField("file_id", "string", "ID of the new file."),
            PayloadField(
                "mime_type", "string", "MIME type of the file.", nullable=True
            ),
            PayloadField(
                "size", "integer", "Original file size in bytes.", nullable=True
            ),
            _timestamp("created_at", "When the file was uploaded."),
        ),
    ),
    Event(
        name="users.user.created",
        description="A user signed up or was created in the project.",
        templates=("users.*.create",),
        payload=(
            PayloadField("user_id", "string", "ID of the new user."),
            _timestamp("created_at", "When the user was created."),
        ),
    ),
)
"""The v1 catalog, in the order ``events/list`` serves it."""

CATALOG: Mapping[str, Event] = MappingProxyType({event.name: event for event in EVENTS})
"""Read-only index of :data:`EVENTS` by name."""


def lookup(name: object) -> Event:
    """The event called ``name``; raises a NotFound :class:`EventsError`
    (``data.kind = "event"``) when there is none."""
    event = CATALOG.get(name) if isinstance(name, str) else None
    if event is None:
        raise EventsError.not_found(f"Unknown event {name!r}", kind=NOT_FOUND_KIND)
    return event
