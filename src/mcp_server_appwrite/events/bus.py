"""Carry push events from the ingress to the replica that holds their stream.

A push subscription is one open ``events/stream`` request on one replica, but
Appwrite's webhook for it lands on whichever replica the gateway picks. So the
ingress publishes each projected event under the subscription id, and the
replica holding the stream listens on that id for as long as the stream is open.

The bus is fire-and-forget, like the events themselves: Appwrite webhooks have
no replay, so an event published while nobody listens (the stream just closed,
or its client is reconnecting) is dropped. :meth:`Bus.publish` says whether
anyone received it, so the ingress can count the drop.

:class:`RedisBus` is the implementation: Redis pub/sub on channel
``mcp:events:<subscription id>``, configured by ``MCP_EVENTS_REDIS_URL``. Every
open stream holds its own pub/sub connection; the stream caps in
:mod:`.streams` bound how many a replica opens.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import AsyncIterator
from typing import Protocol

from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import RedisError

from .delivery import Event

URL_ENV = "MCP_EVENTS_REDIS_URL"

CHANNEL_PREFIX = "mcp:events:"

TIMEOUT_SECONDS = 2.0
"""Connect and command timeout. A publish sits on the ingress's request path,
so a stalled Redis must not hold Appwrite's delivery for long."""

POLL_SECONDS = 1.0
"""How long a listener waits for a message before checking its connection."""

PING_SECONDS = 10.0
"""How often a listener pings Redis over its own connection."""

PONG_SECONDS = 5.0
"""How long a listener waits for the answer to its ping. Without one the
connection is dead (a peer that vanished without a reset can otherwise look
quiet for as long as TCP takes to give up), and :class:`BusError` is raised."""

PING_MESSAGE = "mcp-liveness"


class BusError(Exception):
    """The bus is unreachable. A listener raises it when its connection is lost;
    events published meanwhile are gone."""


class Listener(Protocol):
    """The events of one subscription, from when it was opened until
    :meth:`aclose`."""

    def __aiter__(self) -> AsyncIterator[Event]: ...

    async def aclose(self) -> None: ...


class Bus(Protocol):
    """Routes events by subscription id between replicas."""

    async def publish(self, id: str, event: Event) -> bool:
        """Send ``event`` to the listener of subscription ``id``; whether one
        received it. Raises :class:`BusError` when the bus is unreachable."""
        ...

    async def listen(self, id: str) -> Listener:
        """Start receiving the events of subscription ``id``: nothing published
        after this returns is missed. Raises :class:`BusError`."""
        ...

    async def aclose(self) -> None: ...


def channel(id: str) -> str:
    return CHANNEL_PREFIX + id


def encode(event: Event) -> bytes:
    return json.dumps(event.occurrence(), separators=(",", ":")).encode("utf-8")


def decode(message: bytes) -> Event | None:
    """The event in a bus message, or ``None`` for anything else."""
    try:
        payload = json.loads(message)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    id, name = payload.get("eventId"), payload.get("name")
    timestamp, data = payload.get("timestamp"), payload.get("data")
    if not (
        isinstance(id, str)
        and isinstance(name, str)
        and isinstance(timestamp, str)
        and isinstance(data, dict)
    ):
        return None
    return Event(id=id, name=name, timestamp=timestamp, data=data)


class RedisBus:
    """:class:`Bus` over Redis pub/sub."""

    def __init__(
        self,
        url: str,
        *,
        timeout: float = TIMEOUT_SECONDS,
        ping: float = PING_SECONDS,
        pong: float = PONG_SECONDS,
    ) -> None:
        self._ping = ping
        self._pong = pong
        self._client = Redis.from_url(
            url,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
            socket_keepalive=True,
        )

    @classmethod
    def from_env(cls) -> RedisBus | None:
        """The bus at ``MCP_EVENTS_REDIS_URL``, or ``None`` when it is unset."""
        url = os.getenv(URL_ENV, "").strip()
        return cls(url) if url else None

    async def publish(self, id: str, event: Event) -> bool:
        try:
            receivers = await self._client.publish(channel(id), encode(event))
        except (RedisError, OSError) as error:
            raise BusError(f"publish failed: {error!r}") from error
        return receivers > 0

    async def listen(self, id: str) -> RedisListener:
        pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        try:
            await pubsub.subscribe(channel(id))
        except BaseException as error:
            await RedisListener(pubsub, self._ping, self._pong).aclose()
            if isinstance(error, (RedisError, OSError)):
                raise BusError(f"subscribe failed: {error!r}") from error
            raise
        return RedisListener(pubsub, self._ping, self._pong)

    async def aclose(self) -> None:
        await self._client.aclose()


class RedisListener:
    """:class:`Listener` on one Redis pub/sub connection."""

    def __init__(self, pubsub: PubSub, ping: float, pong: float) -> None:
        self._pubsub = pubsub
        self._ping = ping
        self._pong = pong

    async def __aiter__(self) -> AsyncIterator[Event]:
        pinged: float | None = None
        """When the unanswered ping went out."""
        alive = time.monotonic()
        """When Redis last answered."""
        while True:
            now = time.monotonic()
            if pinged is not None and now - pinged > self._pong:
                raise BusError("listen failed: Redis stopped answering pings")
            try:
                if pinged is None and now - alive > self._ping:
                    await self._pubsub.ping(PING_MESSAGE)
                    pinged = now
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=min(POLL_SECONDS, self._ping),
                )
            except (RedisError, OSError) as error:
                raise BusError(f"listen failed: {error!r}") from error
            if message is None:
                continue
            alive, pinged = time.monotonic(), None
            if message.get("type") != "message":
                continue
            event = decode(message["data"])
            if event is None:
                _log(f"{message.get('channel')!r}: ignored a malformed message")
                continue
            yield event

    async def aclose(self) -> None:
        try:
            await self._pubsub.aclose()
        except (RedisError, OSError):
            pass


def _log(message: str) -> None:
    print(f"[appwrite-mcp][events] {message}", file=sys.stderr, flush=True)
