"""Sign and deliver MCP events to webhook callbacks.

MCP webhook delivery is a profile of Standard Webhooks
(https://github.com/standard-webhooks/standard-webhooks/blob/main/spec/standard-webhooks.md):
every POST carries ``webhook-id``, ``webhook-timestamp`` (Unix seconds) and
``webhook-signature: v1,<base64 HMAC-SHA256(key, "{id}.{timestamp}.{body}")>``,
where ``key`` is the base64-decoded part of the client's ``whsec_`` secret.
During rotation the header carries one space-separated signature per secret so
the receiver can verify with either. MCP adds ``X-MCP-Subscription-Id``.

Two kinds of POST go out through :class:`Dispatcher`, both over the SSRF-safe
:mod:`egress` path:

* a **verification handshake** before a callback is used: a signed
  ``{"type": "verification", "challenge": ...}`` body that the receiver must
  echo in a 2xx JSON response, compared in constant time;
* **event deliveries**: one event per POST, ``{eventId, name, timestamp, data,
  cursor: null}`` serialized once, at most 256 KiB. Failed attempts retry with
  bounded, jittered exponential backoff and are re-signed with a fresh
  timestamp each time; ``410`` and ``413`` are final.

Retries run in-process and nothing is persisted: Appwrite webhooks have no
replay, so a delivery that exhausts its retries is abandoned (``cursor`` stays
``null``). Failures are reported only as the spec's categories, never as raw
endpoint responses, so a callback URL cannot be used as a response oracle.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import random
import secrets
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import anyio
import httpx

from .egress import DestinationError, Egress
from .errors import CallbackFailure

SECRET_PREFIX = "whsec_"
SECRET_MIN_BYTES = 24
SECRET_MAX_BYTES = 64
SIGNATURE_VERSION = "v1"

ID_HEADER = "webhook-id"
TIMESTAMP_HEADER = "webhook-timestamp"
SIGNATURE_HEADER = "webhook-signature"
SUBSCRIPTION_HEADER = "X-MCP-Subscription-Id"
CONTENT_TYPE = "application/json"

VERIFICATION_TYPE = "verification"
VERIFICATION_ID_PREFIX = "msg_verification_"
CHALLENGE_BYTES = 32

# Delivery profile: receivers and intermediaries may reject larger bodies.
MAX_BODY_BYTES = 256 * 1024

# Receivers return 410 to refuse an event for good and 413 for a body they
# will never accept; retrying either cannot succeed.
FINAL_STATUSES = frozenset({410, 413})


class CallbackError(Exception):
    """The callback endpoint failed verification for ``reason``, the
    ``data.reason`` of the CallbackEndpointError the subscribe handler raises."""

    def __init__(self, reason: CallbackFailure) -> None:
        super().__init__(f"Callback endpoint verification failed: {reason}")
        self.reason = reason


class InvalidSecretError(ValueError):
    """A delivery secret is not ``whsec_`` + base64 of 24-64 bytes."""


class PayloadTooLargeError(ValueError):
    """An event body exceeds :data:`MAX_BODY_BYTES` and was not sent."""


class Outcome(StrEnum):
    DELIVERED = "delivered"
    # The receiver (410/413) or the egress policy refused the event; not retried.
    REJECTED = "rejected"
    # Every attempt failed with a retryable error.
    ABANDONED = "abandoned"


def decode_secret(secret: str) -> bytes:
    """Validate a ``whsec_`` secret and return its HMAC key bytes.

    The subscribe handler calls this to reject bad ``delivery.secret`` values
    with ``InvalidParams`` before anything is stored or sent.
    """
    if not isinstance(secret, str) or not secret.startswith(SECRET_PREFIX):
        raise InvalidSecretError(f"Secret must start with {SECRET_PREFIX}")
    encoded = secret.removeprefix(SECRET_PREFIX)
    try:
        key = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise InvalidSecretError("Secret must be base64 after the prefix") from error
    if not SECRET_MIN_BYTES <= len(key) <= SECRET_MAX_BYTES:
        raise InvalidSecretError(
            f"Secret must decode to {SECRET_MIN_BYTES}-{SECRET_MAX_BYTES} bytes"
        )
    return key


def sign(keys: Sequence[bytes], message_id: str, timestamp: int, body: bytes) -> str:
    """Standard Webhooks ``webhook-signature`` value, one entry per key."""
    content = f"{message_id}.{timestamp}.".encode() + body
    return " ".join(
        f"{SIGNATURE_VERSION},"
        + base64.b64encode(hmac.new(key, content, hashlib.sha256).digest()).decode()
        for key in keys
    )


def encode(payload: Mapping[str, Any]) -> bytes:
    """Compact JSON, serialized once so the signed bytes are the sent bytes."""
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()


@dataclass(frozen=True)
class Callback:
    """Where and how to deliver for one subscription.

    ``secrets`` holds every active ``whsec_`` secret, newest first; more than
    one during a rotation grace window.
    """

    url: str
    subscription: str
    secrets: tuple[str, ...]
    keys: tuple[bytes, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.secrets:
            raise InvalidSecretError("At least one secret is required")
        keys = tuple(decode_secret(secret) for secret in self.secrets)
        object.__setattr__(self, "keys", keys)


@dataclass(frozen=True)
class Event:
    """One event occurrence. ``timestamp`` is ISO 8601 with a timezone."""

    id: str
    name: str
    timestamp: str
    data: Mapping[str, Any]

    def encode(self) -> bytes:
        return encode(
            {
                "eventId": self.id,
                "name": self.name,
                "timestamp": self.timestamp,
                "data": dict(self.data),
                "cursor": None,
            }
        )


@dataclass(frozen=True)
class RetryPolicy:
    """Backoff between delivery attempts.

    ``delays`` are the waits before each retry, so attempts are
    ``len(delays) + 1``. Each wait is scaled by a random factor in
    ``[1 - jitter, 1 + jitter]``. The defaults give 4 attempts with at most
    ~12.6 minutes of waiting, inside the spec's 15-minute window even with
    every attempt hitting the 10 s egress timeout.
    """

    delays: tuple[float, ...] = (30.0, 120.0, 480.0)
    jitter: float = 0.2

    def __post_init__(self) -> None:
        if any(delay < 0 for delay in self.delays):
            raise ValueError("delays must not be negative")
        if not 0 <= self.jitter < 1:
            raise ValueError("jitter must be in [0, 1)")

    @property
    def attempts(self) -> int:
        return len(self.delays) + 1

    def delay(self, retry: int, sample: float) -> float:
        """Wait before retry number ``retry`` (0-based); ``sample`` in [0, 1)."""
        return self.delays[retry] * (1 + self.jitter * (2 * sample - 1))


@dataclass(frozen=True)
class Result:
    """How a delivery ended. ``reason`` is set unless delivered."""

    outcome: Outcome
    attempts: int
    status: int | None = None
    reason: CallbackFailure | None = None


def classify(error: BaseException) -> CallbackFailure:
    """Map a transport failure to its reported category."""
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return CallbackFailure.TIMEOUT
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, ssl.SSLError):
            return CallbackFailure.TLS_ERROR
        cause = cause.__cause__ or cause.__context__
    return CallbackFailure.CONNECTION_REFUSED


def status_reason(status: int) -> CallbackFailure:
    return CallbackFailure.HTTP_5XX if status >= 500 else CallbackFailure.HTTP_4XX


# Failures raised by Egress.post; anything else is a bug and propagates.
TRANSPORT_ERRORS = (DestinationError, TimeoutError, httpx.TransportError, OSError)


class Dispatcher:
    """Verifies callbacks and delivers events through one :class:`Egress`.

    ``sleep``, ``clock`` and ``sample`` (jitter source) are injectable so tests can drive the
    retry schedule without waiting.
    """

    def __init__(
        self,
        egress: Egress,
        policy: RetryPolicy | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
        clock: Callable[[], float] = time.time,
        sample: Callable[[], float] = random.random,
    ) -> None:
        self._egress = egress
        self._policy = policy or RetryPolicy()
        self._sleep = sleep
        self._clock = clock
        self._sample = sample

    def headers(
        self, callback: Callback, message_id: str, body: bytes
    ) -> dict[str, str]:
        """Signed headers for one attempt, stamped with the current time."""
        timestamp = int(self._clock())
        return {
            "Content-Type": CONTENT_TYPE,
            ID_HEADER: message_id,
            TIMESTAMP_HEADER: str(timestamp),
            SIGNATURE_HEADER: sign(callback.keys, message_id, timestamp, body),
            SUBSCRIPTION_HEADER: callback.subscription,
        }

    async def verify(self, callback: Callback) -> None:
        """Run the verification handshake once; raise :class:`CallbackError`."""
        challenge = secrets.token_urlsafe(CHALLENGE_BYTES)
        body = encode({"type": VERIFICATION_TYPE, "challenge": challenge})
        message_id = VERIFICATION_ID_PREFIX + secrets.token_hex(12)
        try:
            response = await self._egress.post(
                callback.url, body, self.headers(callback, message_id, body)
            )
        except TRANSPORT_ERRORS as error:
            raise CallbackError(classify(error)) from error
        if response.status >= 400:
            raise CallbackError(status_reason(response.status))
        if not 200 <= response.status < 300:
            raise CallbackError(CallbackFailure.CHALLENGE_FAILED)
        if not hmac.compare_digest(_echo(response.body), challenge.encode()):
            raise CallbackError(CallbackFailure.CHALLENGE_FAILED)

    async def deliver(self, callback: Callback, event: Event) -> Result:
        """Deliver ``event``, retrying per the policy, and report the outcome."""
        body = event.encode()
        if len(body) > MAX_BODY_BYTES:
            raise PayloadTooLargeError(
                f"Event body is {len(body)} bytes; the limit is {MAX_BODY_BYTES}"
            )
        reason: CallbackFailure | None = None
        status: int | None = None
        for attempt in range(1, self._policy.attempts + 1):
            if attempt > 1:
                await self._sleep(self._policy.delay(attempt - 2, self._sample()))
            try:
                response = await self._egress.post(
                    callback.url, body, self.headers(callback, event.id, body)
                )
            except DestinationError:
                return Result(
                    Outcome.REJECTED, attempt, reason=CallbackFailure.CONNECTION_REFUSED
                )
            except TRANSPORT_ERRORS as error:
                reason, status = classify(error), None
                continue
            status = response.status
            if 200 <= status < 300:
                return Result(Outcome.DELIVERED, attempt, status)
            reason = status_reason(status)
            if status in FINAL_STATUSES:
                return Result(Outcome.REJECTED, attempt, status, reason)
        return Result(Outcome.ABANDONED, self._policy.attempts, status, reason)


def _echo(body: bytes) -> bytes:
    """The ``challenge`` a verification response echoed, or empty bytes."""
    try:
        payload = json.loads(body)
    except ValueError:
        return b""
    if not isinstance(payload, dict):
        return b""
    challenge = payload.get("challenge")
    return challenge.encode() if isinstance(challenge, str) else b""
