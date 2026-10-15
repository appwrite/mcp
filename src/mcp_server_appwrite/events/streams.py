"""``events/stream``: push delivery of MCP events.

A push subscription is one long-lived ``events/stream`` request. On the hosted
Streamable HTTP transport it is a POST whose SSE response carries only that
subscription's ``notifications/events/*``, each with the request id in
``params._meta["io.modelcontextprotocol/subscriptionId"]``. The client cancels by
aborting the request.

**Protocol layer.** The low-level SDK serves this as an ordinary
``add_request_handler`` method, with no ASGI branch of its own: on the
stateless SSE transport ``http_app`` configures, a handler may run for as long
as it likes, notifications it sends for its own request go out as SSE ``data:``
frames on that POST, a client abort cancels the handler, and nothing caps
concurrent requests. An invalid subscription is refused before anything is
sent, so its JSON-RPC error is the only message and no stream is opened. On
the 2026-07-28 protocol that answer is plain JSON as long as it comes within
the SDK's 15 s SSE deferral; 2025-era transports always answer in SSE.

**Lifecycle.** Each stream owns exactly one Appwrite webhook in the subscriber's
project, whose sealed envelope says the delivery mode is push (see
:mod:`.envelope`). The stream:

1. Validates like ``events/subscribe`` (catalog event, arguments, a user behind
   the token, webhook write scope), minus the callback, secret and handshake,
   and takes a slot under :data:`PRINCIPAL_STREAMS` and :data:`STREAMS`
   (``-32013`` beyond them).
2. Starts listening on the :mod:`.bus` under a random subscription id, then
   creates the webhook with a short lease (:data:`LEASE_MS`) through
   :meth:`.subscriptions.Subscriptions.open`, which also authorizes the caller
   and cleans up their expired subscriptions. Listening first means no event
   the webhook delivers is missed.
3. Sends ``notifications/events/active {cursor: null, truncated: false}``, then
   forwards each event from the bus as ``notifications/events/event``, sends
   ``notifications/events/heartbeat {cursor: null}`` every
   :data:`HEARTBEAT_SECONDS`, and renews the lease every
   :data:`RENEW_SECONDS` with the request's own bearer token.
4. On client abort, a terminal error or server shutdown, stops listening and
   deletes the webhook (best effort, shielded from the cancellation). If the
   process dies instead, the lease runs out: the ingress drops the webhook's
   deliveries as ``expired`` and the caller's next subscribe or stream deletes
   it.

A stream renews with the token it was opened with, so it ends shortly before
that token expires (:data:`EXPIRY_SECONDS`), the way shutdown ends it: with
its final result and no ``terminated``, so the client reconnects with a fresh
token instead of giving the subscription up. A renewal refused with Forbidden
or NotFound (access or a scope was revoked, the resource or the webhook was
deleted) sends ``notifications/events/terminated`` and ends the stream. Other
renewal failures, a call slower than :data:`APPWRITE_SECONDS` among them, send
``notifications/events/error`` and retry after :data:`RETRY_SECONDS`, and only
once the lease has run out is the stream terminated. Losing the bus connection sends an error too; once it
is back, ``notifications/events/active`` with ``truncated: true`` marks the
gap. Events have no replay, so ``cursor`` is always ``null`` and ``maxAgeMs``
is accepted without effect.

**Shutdown.** :meth:`Streams.close`, called when the server is asked to stop,
ends every stream: each deletes its webhook and sends its final result, and the
client reconnects to another replica.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import Any, cast

import anyio
from anyio.abc import TaskGroup
from mcp.server import ServerRequestContext
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.shared.subscriptions import SUBSCRIPTION_ID_META_KEY
from mcp.types import RequestParams
from mcp_types import Notification, ServerNotification
from pydantic import ConfigDict

from .. import telemetry
from .bus import Bus, BusError, Listener
from .envelope import Mode, Subscription
from .errors import FORBIDDEN, INTERNAL_ERROR, NOT_FOUND, EventsError
from .subscriptions import (
    Grant,
    Operation,
    Outcome,
    Subscriptions,
    Target,
    check_max_age,
    current_principal,
    require_writable,
    resolve_target,
)

METHOD = "events/stream"

ACTIVE = "notifications/events/active"
EVENT = "notifications/events/event"
HEARTBEAT = "notifications/events/heartbeat"
ERROR = "notifications/events/error"
TERMINATED = "notifications/events/terminated"

LEASE_MS = 5 * 60 * 1000
"""Lease written into a stream's webhook. Short, because a stream renews it
while open: a replica that dies leaves a webhook that stops delivering within
this long."""

RENEW_SECONDS = 120.0
"""How often an open stream renews its lease. Leaves room for one failed
renewal and a retry before the lease runs out."""

RETRY_SECONDS = 15.0
"""Wait before retrying a failed renewal or reconnecting to the bus."""

HEARTBEAT_SECONDS = 25.0
"""Heartbeat interval: within the spec's 30 s, and inside proxy idle timeouts."""

PRINCIPAL_STREAMS = 25
"""Open streams one user may hold on one replica."""

STREAMS = 1000
"""Open streams one replica holds. Each holds a bus connection."""

CLOSE_SECONDS = 10.0
"""How long deleting a closed stream's webhook, or announcing its end to a
client that stopped reading, may take."""

APPWRITE_SECONDS = 30.0
"""How long renewing a stream's lease may take. The Appwrite SDK sets no
request timeout, and a renewal that hangs would let the lease run out while
heartbeats make the stream look healthy."""

EXPIRY_SECONDS = 60.0
"""How long before its token expires a stream ends, so the renewal that would
be refused never happens."""


@dataclass(frozen=True)
class Settings:
    """Clocks (in seconds) and caps of the streams; tests shrink them."""

    lease: float = LEASE_MS / 1000
    renew: float = RENEW_SECONDS
    retry: float = RETRY_SECONDS
    heartbeat: float = HEARTBEAT_SECONDS
    principal_streams: int = PRINCIPAL_STREAMS
    streams: int = STREAMS
    appwrite: float = APPWRITE_SECONDS
    expiry: float = EXPIRY_SECONDS


class StreamParams(RequestParams):
    """``events/stream`` params. Unknown members are tolerated, as the draft
    specification may add some."""

    model_config = ConfigDict(extra="allow")

    name: Any = None
    arguments: Any = None
    cursor: Any = None
    max_age_ms: Any = None


class Streams:
    """Serves ``events/stream`` for the subscriptions of one ingress, with
    events arriving on ``bus``."""

    def __init__(
        self,
        subscriptions: Subscriptions,
        bus: Bus,
        *,
        clock: Callable[[], float] = time.time,
        settings: Settings = Settings(),
    ) -> None:
        self._subscriptions = subscriptions
        self._bus = bus
        self._clock = clock
        self._settings = settings
        self._open: dict[str, int] = {}
        """Open streams per principal; an entry exists only while above zero."""
        self._count = 0
        self._closed = False
        self._closing: anyio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def bus(self) -> Bus:
        return self._bus

    @property
    def subscriptions(self) -> Subscriptions:
        return self._subscriptions

    @property
    def settings(self) -> Settings:
        return self._settings

    async def closed(self) -> None:
        """Return once :meth:`close` has been called."""
        if self._closed:
            return
        await (self._closing or anyio.Event()).wait()

    def close(self) -> None:
        """End every open stream and refuse new ones. Safe to call from a
        signal handler or another thread: ``_closed`` is set first, and every
        stream checks it only after the event it waits on exists."""
        self._closed = True
        if self._loop is not None and self._closing is not None:
            self._loop.call_soon_threadsafe(self._closing.set)

    async def stream(
        self, context: ServerRequestContext[Any, Any], params: StreamParams
    ) -> dict[str, Any]:
        started = time.monotonic()
        if context.request_id is None:
            raise EventsError.invalid_params("events/stream needs a request id")
        target = resolve_target(params.name, params.arguments)
        if params.cursor is not None and not isinstance(params.cursor, str):
            raise EventsError.invalid_params("cursor must be a string or null")
        check_max_age(params.max_age_ms)
        principal = current_principal()
        require_writable()
        token = get_access_token()
        expires = token.expires_at if token is not None else None
        if self._closing is None:
            self._closing = anyio.Event()
            self._loop = asyncio.get_running_loop()
        if self._closed:
            raise EventsError.internal("The server is shutting down; reconnect")
        with self._slot(principal):
            grant = self.grant()
            subscription = Subscription.stream(
                project=target.project,
                name=target.event.name,
                arguments=target.arguments,
                expires=grant.expires,
                refresh=grant.refresh,
                principal=principal,
            )
            try:
                listener = await self._bus.listen(subscription.id)
            except BusError as error:
                raise EventsError.internal("Push delivery is unavailable") from error
            stream = Stream(self, context, target, subscription, listener, expires)
            try:
                await self._subscriptions.open(target, subscription)
            except BaseException as error:
                with anyio.CancelScope(shield=True):
                    await stream.listener.aclose()
                    # A refusal created nothing; an abort mid-create waited for
                    # the create, so the webhook exists by now.
                    if not isinstance(error, EventsError):
                        await stream.delete()
                raise
            await stream.run(started)
        return {"_meta": {}}

    @contextmanager
    def _slot(self, principal: str) -> Generator[None]:
        held = self._open.get(principal, 0)
        if held >= self._settings.principal_streams:
            raise EventsError.resource_exhausted(
                f"You already hold {held} event streams on this server; close one "
                "before opening another",
                limit="streams",
                maximum=self._settings.principal_streams,
            )
        if self._count >= self._settings.streams:
            raise EventsError.resource_exhausted(
                "This server holds as many event streams as it can; retry later",
                limit="server_streams",
                maximum=self._settings.streams,
            )
        self._open[principal] = held + 1
        self._count += 1
        try:
            yield
        finally:
            self._count -= 1
            remaining = self._open[principal] - 1
            if remaining:
                self._open[principal] = remaining
            else:
                del self._open[principal]

    def grant(self) -> Grant:
        """A full lease from now; its ``refresh`` is when the stream renews
        it."""
        now = self.now()
        expires = now + int(self._settings.lease * 1000)
        return Grant(expires, min(now + int(self._settings.renew * 1000), expires))

    def now(self) -> int:
        """Unix epoch milliseconds."""
        return int(self._clock() * 1000)


class Stream:
    """One open ``events/stream``."""

    def __init__(
        self,
        streams: Streams,
        context: ServerRequestContext[Any, Any],
        target: Target,
        subscription: Subscription,
        listener: Listener,
        expires: int | None,
    ) -> None:
        self._streams = streams
        self._context = context
        self._target = target
        self.subscription = subscription
        self.listener = listener
        self._expires = expires
        """When the stream's token expires, in Unix epoch seconds."""
        self._group: TaskGroup | None = None
        self._outcome = Outcome.CLOSED

    @property
    def _event(self) -> str:
        return self._target.event.name

    async def run(self, started: float) -> None:
        """Serve the stream until the client leaves, it is terminated or the
        server closes it; then delete its webhook. ``started`` is when the
        request arrived (``time.monotonic``)."""
        telemetry.record_stream(self._event, 1)
        telemetry.record_subscription(Operation.STREAM, Outcome.OPENED, self._event)
        _log(f"{self.subscription.id}: stream opened for {self._event}")
        try:
            await self._notify(ACTIVE, {"cursor": None, "truncated": False})
            # Counted once open, not when it ends: a client abort, the usual
            # end of a stream, is a cancellation.
            telemetry.record_message(METHOD, "success", time.monotonic() - started)
            async with anyio.create_task_group() as group:
                self._group = group
                group.start_soon(self._forward)
                group.start_soon(self._beat)
                group.start_soon(self._keep)
                group.start_soon(self._wait_closing)
                group.start_soon(self._wait_expiry)
        finally:
            with anyio.CancelScope(shield=True):
                await self.listener.aclose()
                await self.delete()
                telemetry.record_stream(self._event, -1)
                telemetry.record_subscription(
                    Operation.STREAM, self._outcome, self._event
                )
                _log(f"{self.subscription.id}: stream {self._outcome}")

    def _end(self) -> None:
        if self._group is not None:
            self._group.cancel_scope.cancel()

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        request = self._context.request_id
        notification = Notification[dict[str, Any], str](
            method=method,
            params={**params, "_meta": {SUBSCRIPTION_ID_META_KEY: request}},
        )
        # The SDK's notification union has no events notifications yet
        # (python-sdk#3640); it only serializes the model it is given.
        await self._context.session.send_notification(
            cast(ServerNotification, notification), related_request_id=request
        )

    async def _forward(self) -> None:
        while True:
            try:
                async for event in self.listener:
                    if event.name != self._event:
                        continue
                    await self._notify(EVENT, event.occurrence())
                    telemetry.record_event_delivery(
                        event.name, "delivered", None, Mode.PUSH
                    )
            except BusError as error:
                _log(f"{self.subscription.id}: {error}")
                await self._error(
                    INTERNAL_ERROR, "Lost the connection that carries events"
                )
            await self.listener.aclose()
            self.listener = await self._reconnect()
            await self._notify(ACTIVE, {"cursor": None, "truncated": True})

    async def _reconnect(self) -> Listener:
        while True:
            await anyio.sleep(self._streams.settings.retry)
            try:
                return await self._streams.bus.listen(self.subscription.id)
            except BusError as error:
                _log(f"{self.subscription.id}: {error}")

    async def _beat(self) -> None:
        while True:
            await anyio.sleep(self._streams.settings.heartbeat)
            await self._notify(HEARTBEAT, {"cursor": None})

    async def _keep(self) -> None:
        """Renew the lease until the stream ends."""
        settings = self._streams.settings
        delay = settings.renew
        while True:
            await anyio.sleep(delay)
            grant = self._streams.grant()
            renewed = replace(
                self.subscription, expires=grant.expires, refresh=grant.refresh
            )
            try:
                with anyio.fail_after(settings.appwrite):
                    await self._streams.subscriptions.renew(self._target, renewed)
            except (EventsError, TimeoutError) as caught:
                error = (
                    caught
                    if isinstance(caught, EventsError)
                    else EventsError.internal("Appwrite did not answer in time")
                )
                if error.code in (FORBIDDEN, NOT_FOUND):
                    await self._terminate(error)
                    return
                _log(f"{self.subscription.id}: renewal failed ({error.code})")
                if self._streams.now() >= self.subscription.expires:
                    await self._terminate(
                        EventsError.internal("The subscription could not be renewed")
                    )
                    return
                await self._error(error.code, error.message)
                delay = settings.retry
                continue
            self.subscription = renewed
            delay = settings.renew

    async def _wait_closing(self) -> None:
        await self._streams.closed()
        self._end()

    async def _wait_expiry(self) -> None:
        """End the stream before its token expires; see the module docstring."""
        if self._expires is None:
            return
        now = self._streams.now() / 1000
        ending = self._expires - self._streams.settings.expiry
        await anyio.sleep(max(ending if ending > now else self._expires, now) - now)
        self._outcome = Outcome.EXPIRED
        self._end()

    async def _error(self, code: int, message: str) -> None:
        await self._notify(ERROR, {"error": {"code": code, "message": message}})

    async def _terminate(self, error: EventsError) -> None:
        payload: dict[str, Any] = {"code": error.code, "message": error.message}
        if error.data is not None:
            payload["data"] = error.data
        # A client that stopped reading must not keep the stream open.
        with anyio.move_on_after(CLOSE_SECONDS):
            await self._notify(TERMINATED, {"error": payload})
        self._outcome = Outcome.TERMINATED
        _log(f"{self.subscription.id}: terminated ({error.code})")
        self._end()

    async def delete(self) -> None:
        """Delete the stream's webhook, best effort, within
        :data:`CLOSE_SECONDS`."""
        try:
            with anyio.fail_after(CLOSE_SECONDS):
                await self._streams.subscriptions.close(
                    self._target, self.subscription.id
                )
        except Exception as error:
            # The lease runs out and cleanup removes the webhook later.
            _log(f"{self.subscription.id}: webhook not deleted ({error!r})")


def _log(message: str) -> None:
    print(f"[appwrite-mcp][events] {message}", file=sys.stderr, flush=True)
