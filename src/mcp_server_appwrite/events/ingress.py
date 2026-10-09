"""Receive Appwrite webhooks and forward them as MCP events.

Each subscription is one Appwrite webhook in the subscriber's project, pointed
at ``{MCP_PUBLIC_URL}/appwrite/webhooks/{subscription id}`` with the sealed
envelope as its Basic auth password (see :mod:`.envelope`). This endpoint is
not behind the bearer-token gate: the Appwrite signature is the authentication.

For each delivery:

1. Take the envelope from Basic auth and read its key id.
2. Verify ``X-Appwrite-Webhook-Signature``: Appwrite signs
   ``HMAC-SHA1(url + body)`` with the webhook secret, where ``url`` is the URL
   the webhook was registered with. The ingress rebuilds that URL from
   ``MCP_PUBLIC_URL`` and the path and never trusts the inbound ``Host``. The
   secret is derived from the subscription id and the envelope's sealing key,
   so no storage is needed. The body is hashed as it streams in.
3. Open the envelope for (subscription id, ``X-Appwrite-Webhook-Project-Id``).
4. Check expiry, map the Appwrite event onto the subscribed catalog event and
   project the body down to the event's payload fields (:mod:`.projection`).
5. Start the delivery in the background and answer ``200`` at once, well inside
   Appwrite's 15 second timeout.

Status codes follow one rule: **anything that came from the subscriber's own
webhook is answered with 2xx.** Appwrite counts every ``>= 400`` answer and,
after ten in a row, pauses the webhook and emails the project owner. Expired
subscriptions, filtered statuses, unknown events and bodies too large to read
are all normal operation, so they are acknowledged and dropped. Only a request
that fails authentication gets ``401``: no Basic auth, an envelope that does not
parse, a bad signature, or an envelope that does not open for this webhook and
project. A legitimate webhook cannot produce those in normal operation:

* After a sealing-key rotation the old key stays in the ring until every
  subscription sealed with it has expired, so its signature still verifies and
  its envelope still opens. Once the old key is dropped, only expired
  subscriptions still name it; their deliveries are dropped with ``200``
  (``retired_key``) rather than rejected, because the webhook is an orphan that
  has not been cleaned up yet, not an attacker.
* Changing ``MCP_PUBLIC_URL`` changes the signed URL and breaks every existing
  subscription until it refreshes. Do not change it while subscriptions exist.
* Editing the webhook's password, secret or URL in the Console does produce
  ``401``; the subscription is broken either way, and pausing it is the right
  outcome.

Nothing from the Appwrite body is forwarded beyond the projected fields.
Deliveries run in a task group owned by the app lifespan and are lost if the
process stops mid-retry (at most once, like Appwrite webhooks themselves).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

import anyio
from anyio.abc import TaskGroup
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .. import error_monitoring, telemetry
from ..auth import public_base_url
from .catalog import CATALOG
from .delivery import (
    Callback,
    Dispatcher,
    Event,
    InvalidSecretError,
    PayloadTooLargeError,
)
from .egress import Egress
from .envelope import (
    Envelope,
    EnvelopeError,
    Keyring,
    appwrite_hmac,
    canonical_json,
    valid_subscription_id,
)
from .projection import Drop, Dropped, project

PATH = "/appwrite/webhooks/{id}"
"""Route of the ingress; ``id`` is the subscription id, also the webhook id."""

BODY_LIMIT_BYTES = 2 * 1024 * 1024
"""Bytes of an Appwrite body kept for projection. Larger bodies (a row with
very large text columns) are still hashed to the end so the signature is
checked, then dropped: the forwarded fields are IDs, but the JSON has to parse
to find them."""

EVENT_PREFIX = "evt_"
EVENT_HASH_CHARS = 32

SIGNATURE_HEADER = "x-appwrite-webhook-signature"
PROJECT_HEADER = "x-appwrite-webhook-project-id"
DELIVERY_HEADER = "x-appwrite-webhook-delivery-id"
EVENTS_HEADER = "x-appwrite-webhook-events"
BASIC_SCHEME = "basic "


class Outcome(StrEnum):
    """What the ingress did with an Appwrite delivery."""

    ACCEPTED = "accepted"
    DROPPED = "dropped"
    REJECTED = "rejected"


class Rejection(StrEnum):
    """Why a request failed authentication (``401``)."""

    CREDENTIALS = "credentials"
    """No Basic auth, or a password that is not an envelope."""
    SIGNATURE = "signature"
    """``X-Appwrite-Webhook-Signature`` is missing or wrong."""
    ENVELOPE = "envelope"
    """The envelope does not open for this webhook and project."""


@dataclass(frozen=True)
class Body:
    """An Appwrite request body as read by :meth:`Ingress.read`."""

    content: bytes
    """The body, or empty when it exceeded :data:`BODY_LIMIT_BYTES`."""
    complete: bool
    """Whether ``content`` is the whole body."""
    signature: str
    """The Appwrite signature computed over the whole body."""


def event_id(delivery: str, subscription: str) -> str:
    """Stable MCP ``eventId`` for one Appwrite delivery to one subscription.

    ``X-Appwrite-Webhook-Delivery-Id`` is the same on every Appwrite retry, so
    a retried delivery gets the same ``eventId`` and receivers can dedupe it."""
    digest = hashlib.sha256(canonical_json([delivery, subscription])).hexdigest()
    return EVENT_PREFIX + digest[:EVENT_HASH_CHARS]


def password(header: str | None) -> str | None:
    """The password of an HTTP Basic ``Authorization`` header."""
    if header is None or not header.lower().startswith(BASIC_SCHEME):
        return None
    try:
        decoded = base64.b64decode(header[len(BASIC_SCHEME) :], validate=True)
        _username, separator, secret = decoded.decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return None
    return secret if separator and secret else None


def fired(header: str | None) -> list[str]:
    """The Appwrite event names in ``X-Appwrite-Webhook-Events``."""
    return [name.strip() for name in (header or "").split(",") if name.strip()]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _log(message: str) -> None:
    print(f"[appwrite-mcp][events] {message}", file=sys.stderr, flush=True)


class Ingress:
    """The Appwrite webhook endpoint and the deliveries it starts.

    Create one per process. :meth:`run` must wrap the app's lifetime: it owns
    the task group deliveries run in and closes the shared :class:`Egress` on
    the way out.
    """

    def __init__(
        self,
        keyring: Keyring,
        egress: Egress,
        dispatcher: Dispatcher | None = None,
        *,
        base: str,
    ) -> None:
        self._keyring = keyring
        self._egress = egress
        self._dispatcher = dispatcher or Dispatcher(egress)
        self._base = base.rstrip("/")
        self._group: TaskGroup | None = None

    @classmethod
    def from_env(cls) -> Ingress:
        """Sealing keys from ``MCP_EVENTS_SEALING_KEYS``, the public URL from
        ``MCP_PUBLIC_URL``. Raises :class:`~.envelope.KeyringError` when the
        keys are missing, so a misconfigured server fails at startup."""
        return cls(Keyring.from_env(), Egress(), base=public_base_url())

    @property
    def keyring(self) -> Keyring:
        return self._keyring

    @property
    def egress(self) -> Egress:
        return self._egress

    @property
    def dispatcher(self) -> Dispatcher:
        return self._dispatcher

    @property
    def route(self) -> Route:
        return Route(PATH, endpoint=self.handle, methods=["POST"])

    def url(self, id: str) -> str:
        """The URL the webhook for subscription ``id`` is registered with."""
        return f"{self._base}{PATH.format(id=id)}"

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Serve deliveries for the duration of the block; cancel what is still
        in flight and close the egress client afterwards."""
        async with self._egress:
            async with anyio.create_task_group() as group:
                self._group = group
                try:
                    yield
                finally:
                    self._group = None
                    group.cancel_scope.cancel()

    async def read(self, request: Request, id: str, key: str) -> Body:
        """Stream the body, hashing all of it and keeping at most
        :data:`BODY_LIMIT_BYTES`."""
        digest = appwrite_hmac(self.url(id), key)
        content = bytearray()
        complete = True
        async for chunk in request.stream():
            digest.update(chunk)
            if complete and len(content) + len(chunk) > BODY_LIMIT_BYTES:
                complete = False
                content.clear()
            elif complete:
                content.extend(chunk)
        signature = base64.b64encode(digest.digest()).decode("ascii")
        return Body(bytes(content), complete, signature)

    async def handle(self, request: Request) -> Response:
        id = request.path_params["id"]
        envelope = password(request.headers.get("authorization"))
        if not valid_subscription_id(id) or envelope is None:
            return self._reject(id, Rejection.CREDENTIALS)
        try:
            key_id = Envelope.parse(envelope).key
        except EnvelopeError:
            return self._reject(id, Rejection.CREDENTIALS)
        try:
            key = self._keyring.signing_key(id, key_id)
        except EnvelopeError:
            return self._drop(id, Drop.RETIRED_KEY)

        body = await self.read(request, id, key)
        signature = request.headers.get(SIGNATURE_HEADER, "")
        if not hmac.compare_digest(
            body.signature.encode("ascii"), signature.encode("utf-8")
        ):
            return self._reject(id, Rejection.SIGNATURE)

        project_id = request.headers.get(PROJECT_HEADER, "")
        try:
            subscription = self._keyring.open(envelope, id, project_id)
        except EnvelopeError:
            return self._reject(id, Rejection.ENVELOPE)
        if subscription.expired():
            return self._drop(id, Drop.EXPIRED, subscription.name)
        event = CATALOG.get(subscription.name)
        if event is None:
            return self._drop(id, Drop.UNKNOWN_EVENT)
        if not body.complete:
            return self._drop(id, Drop.TOO_LARGE, event.name)
        try:
            payload = json.loads(body.content)
        except ValueError:
            return self._drop(id, Drop.MALFORMED, event.name)
        if not isinstance(payload, dict):
            return self._drop(id, Drop.MALFORMED, event.name)
        try:
            projection = project(
                event,
                subscription.arguments,
                payload,
                fired(request.headers.get(EVENTS_HEADER)),
            )
        except Dropped as dropped:
            return self._drop(id, dropped.reason, event.name)

        try:
            callback = Callback(
                subscription.callback, subscription.id, subscription.secrets
            )
        except InvalidSecretError:
            return self._drop(id, Drop.MALFORMED, event.name)
        delivery = (
            request.headers.get(DELIVERY_HEADER)
            or hashlib.sha256(body.content).hexdigest()
        )
        occurrence = Event(
            id=event_id(delivery, subscription.id),
            name=event.name,
            timestamp=projection.occurred or _now(),
            data=projection.data,
        )
        if self._group is None:
            raise RuntimeError("Ingress.run() is not active")
        self._group.start_soon(self._deliver, callback, occurrence)
        telemetry.record_ingress(Outcome.ACCEPTED, None, event.name)
        return JSONResponse({"status": Outcome.ACCEPTED, "eventId": occurrence.id})

    async def _deliver(self, callback: Callback, event: Event) -> None:
        started = time.monotonic()
        try:
            result = await self._dispatcher.deliver(callback, event)
        except PayloadTooLargeError:
            telemetry.record_event_delivery(event.name, "too_large", None)
            _log(f"{callback.subscription}: {event.id} too large; not sent")
            return
        except Exception as error:
            # A failing child would cancel the whole task group, and with it
            # every other delivery in flight.
            error_monitoring.capture_exception(
                error, tags={"mcp.events.subscription": callback.subscription}
            )
            telemetry.record_event_delivery(event.name, "error", None)
            _log(f"{callback.subscription}: {event.id} failed: {error!r}")
            return
        reason = result.reason.value if result.reason is not None else None
        telemetry.record_event_delivery(event.name, result.outcome, reason)
        _log(
            f"{callback.subscription}: {event.id} {result.outcome} after "
            f"{result.attempts} attempt(s) in {time.monotonic() - started:.1f}s"
            + (f" ({reason})" if reason else "")
        )

    def _drop(self, id: str, reason: Drop, event: str | None = None) -> Response:
        telemetry.record_ingress(Outcome.DROPPED, reason, event)
        _log(f"{id}: dropped ({reason})")
        return JSONResponse({"status": Outcome.DROPPED, "reason": reason})

    def _reject(self, id: str, reason: Rejection) -> Response:
        telemetry.record_ingress(Outcome.REJECTED, reason, None)
        _log(f"{id[:64]!r}: rejected ({reason})")
        # Appwrite shows failed responses in the webhook's logs, so the body
        # says only which check failed.
        return JSONResponse(
            {"status": Outcome.REJECTED, "reason": reason}, status_code=401
        )
