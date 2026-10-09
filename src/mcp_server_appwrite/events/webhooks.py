"""The Appwrite project webhooks that back MCP event subscriptions.

Every subscription is one webhook in the subscriber's own project, written with
the subscriber's OAuth token (``project:webhooks.read`` and
``project:webhooks.write``). This module owns what such a webhook looks like
and how it is recognized later:

* ``$id`` is the subscription id (``sub_`` + 32 hex chars, see
  :func:`.envelope.subscription_id`), so a refresh is an update and an
  unsubscribe a delete by id.
* ``name`` is a :class:`Label`, for example
  ``MCP events | tablesdb.row.created | until 2026-10-09T11:00:00Z | owner OPxPhaa1``.
  Appwrite stores ``authPassword`` write-only, so the sealed envelope (and the
  expiry inside it) can never be read back. The label repeats the two facts a
  later subscribe needs to clean up without opening it: when the subscription
  expires and a short tag of who made it. It is also what a person sees in the
  Console.
* ``authUsername`` is :data:`USERNAME`, a fixed non-secret value. Appwrite sends
  Basic auth only when both username and password are set, and the password is
  the envelope.
* ``secret`` is the derived Appwrite signing key. Current Appwrite returns it
  only from create and from the secret update, so a refresh always sets it
  again rather than comparing.

A webhook is **managed** only when its id is a subscription id *and* its name
parses as a label. Anything else in the project belongs to the user and is
never changed or deleted.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from appwrite_console.client import Client
from appwrite_console.exception import AppwriteException
from appwrite_console.models.webhook import Webhook
from appwrite_console.query import Query
from appwrite_console.services.webhooks import Webhooks as Service

from .envelope import valid_subscription_id

USERNAME = "mcp-events"
"""``authUsername`` of every managed webhook. Not a secret: it only makes
Appwrite send the envelope (the password) as Basic auth."""

PREFIX = "MCP events"
SEPARATOR = " | "
"""ASCII on purpose: Appwrite's worker sends the name back in the
``X-Appwrite-Webhook-Name`` header of every delivery."""
UNTIL = "until "
OWNER = "owner "
STAMP = "%Y-%m-%dT%H:%M:%SZ"

OWNER_CHARS = 8
"""Characters of the principal digest kept in the label. Enough to keep
principals apart within one project; a collision could only ever delete
another principal's *expired* webhook, whose deliveries are already dropped."""

OWNER_PATTERN = re.compile(rf"[A-Za-z0-9_-]{{{OWNER_CHARS}}}")

NAME_MAX = 128
"""Appwrite's limit on a webhook name. The longest label (the longest catalog
event name) is under 100 characters."""

PAGE = 100
"""Webhooks per listing request."""

NOT_FOUND = "webhook_not_found"
ALREADY_EXISTS = "webhook_already_exists"


def owner(principal: str) -> str:
    """The label tag for a principal digest."""
    return principal[:OWNER_CHARS]


@dataclass(frozen=True)
class Label:
    """The ``name`` of a managed webhook."""

    event: str
    expires: int
    """Expiry as Unix epoch milliseconds. Written rounded up to the second, so
    the label never claims an earlier expiry than the envelope."""
    owner: str

    def __str__(self) -> str:
        seconds = math.ceil(self.expires / 1000)
        stamp = datetime.fromtimestamp(seconds, UTC).strftime(STAMP)
        return SEPARATOR.join((PREFIX, self.event, UNTIL + stamp, OWNER + self.owner))

    @classmethod
    def parse(cls, name: str) -> Label | None:
        """The label ``name`` spells, or ``None`` if it is not one."""
        parts = name.split(SEPARATOR)
        if len(parts) != 4:
            return None
        prefix, event, until, tag = parts
        if prefix != PREFIX or not until.startswith(UNTIL):
            return None
        if not tag.startswith(OWNER):
            return None
        tag = tag.removeprefix(OWNER)
        if OWNER_PATTERN.fullmatch(tag) is None or not event:
            return None
        try:
            stamp = datetime.strptime(until.removeprefix(UNTIL), STAMP)
        except ValueError:
            return None
        expires = int(stamp.replace(tzinfo=UTC).timestamp()) * 1000
        return cls(event=event, expires=expires, owner=tag)

    def expired(self, now: int) -> bool:
        return now >= self.expires


def managed(webhook: Webhook) -> Label | None:
    """The label of a webhook this server created, or ``None`` for anything
    else in the project."""
    if not valid_subscription_id(webhook.id):
        return None
    return Label.parse(webhook.name)


class Webhooks:
    """Webhook calls in one project, made with the caller's client.

    Errors other than "not found" propagate as ``AppwriteException`` for the
    caller to translate."""

    def __init__(self, client: Client) -> None:
        self._service = Service(client)

    def listing(self) -> list[Webhook]:
        """Every webhook in the project."""
        webhooks: list[Webhook] = []
        while True:
            page = self._service.list(
                queries=[Query.limit(PAGE), Query.offset(len(webhooks))]
            ).webhooks
            webhooks.extend(page)
            if len(page) < PAGE:
                return webhooks

    def get(self, id: str) -> Webhook | None:
        try:
            return self._service.get(id)
        except AppwriteException as error:
            if error.type == NOT_FOUND:
                return None
            raise

    def create(
        self,
        id: str,
        *,
        url: str,
        label: Label,
        events: list[str],
        password: str,
        secret: str,
    ) -> None:
        self._service.create(
            id,
            url,
            str(label),
            events,
            enabled=True,
            tls=True,
            auth_username=USERNAME,
            auth_password=password,
            secret=secret,
        )

    def update(
        self,
        id: str,
        *,
        url: str,
        label: Label,
        events: list[str],
        password: str,
    ) -> None:
        """Rewrite a managed webhook. Re-enables it if Appwrite paused it after
        failed deliveries, as a refresh should. The update endpoint does not
        take the secret; see :meth:`sign`."""
        self._service.update(
            id,
            str(label),
            url,
            events,
            enabled=True,
            tls=True,
            auth_username=USERNAME,
            auth_password=password,
        )

    def sign(self, id: str, secret: str) -> None:
        """Set the webhook's signing key."""
        self._service.update_secret(id, secret)

    def delete(self, id: str) -> bool:
        """Delete a webhook; ``False`` when it was already gone."""
        try:
            self._service.delete(id)
        except AppwriteException as error:
            if error.type == NOT_FOUND:
                return False
            raise
        return True
