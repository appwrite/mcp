"""Sealed subscription envelopes for MCP Events.

The hosted server keeps no subscription store. Each subscription is one Appwrite
project webhook, and everything the ingress needs to deliver an event is sealed
into that webhook's ``authPassword``, which Appwrite stores encrypted, never
returns, and sends back as HTTP Basic auth on every delivery:

* The webhook ``$id`` is the deterministic :func:`subscription_id`, so a refresh
  is an upsert and an unsubscribe is a delete by id. A push subscription (one
  ``events/stream``) gets a random :func:`stream_id` instead: it lives as long
  as its stream, so two streams with the same arguments are two subscriptions.
* ``authPassword`` holds the envelope from :meth:`Keyring.seal`: a Subscription
  Record v1 sealed with the ``mcp-subscription+jwe`` profile of
  `mcp-event-subscriptions <https://github.com/ChiragAgg5k/mcp-event-subscriptions>`_
  (JWE Compact, ``dir`` + ``A256GCM``). The envelope is bound to a
  :class:`Context`: this server (``MCP_PUBLIC_URL``), the project as tenant and
  the webhook id as resource, so an envelope copied onto another webhook,
  project or deployment fails to open. Appwrite only sends Basic auth when
  ``authUsername`` is non-empty too, so the webhook needs a fixed, non-secret
  username alongside it.
* The webhook ``secret`` is :meth:`Keyring.signing_key`, derived from the
  sealing key and the subscription id, so the ingress can check
  ``X-Appwrite-Webhook-Signature`` (:func:`verify_appwrite_signature`) without
  storage.

This module is the adapter around the package: it builds and reads the record,
supplies the expected context from trusted configuration only, and keeps what
the profile leaves to adapters (subscription ids, the principal digest, the
Appwrite signing key and the ``authPassword`` size budget).

Sealing keys come from ``MCP_EVENTS_SEALING_KEYS`` (``<id>:<base64 32 bytes>``,
comma separated, first one active). Every key in the ring opens envelopes, so a
rotation first adds the new key behind the active one on every replica, then
moves it in front, and drops the old one once every subscription has refreshed
(the TTL cap bounds how long that takes). Sealing with a key some replica does
not hold yet would make that replica drop the deliveries as ``retired_key``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Any

import mcp_event_subscriptions as profile
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEYS_ENV = "MCP_EVENTS_SEALING_KEYS"
KEY_SEPARATOR = ":"
KEYS_SEPARATOR = ","
KEY_BYTES = 32
# A random 32-byte key has ~30 distinct byte values; fewer than this means a
# repeated or hand-typed key (all zeros, "aaaa…", a short passphrase).
KEY_MIN_DISTINCT_BYTES = 16
KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")
"""The profile's ``kid`` syntax."""
COMPACT_PARTS = 5
"""``header..iv.ciphertext.tag``: JWE Compact with an empty encrypted key."""

RECORD_VERSION = 1

SUBSCRIPTION_PREFIX = "sub_"
# Appwrite custom ids: at most 36 chars of [A-Za-z0-9._-], no leading special
# char. "sub_" + 32 hex chars (128 bits of SHA-256) fills that exactly.
SUBSCRIPTION_HASH_CHARS = 32
SUBSCRIPTION_ID_MAX = len(SUBSCRIPTION_PREFIX) + SUBSCRIPTION_HASH_CHARS
SUBSCRIPTION_ID_PATTERN = re.compile(
    rf"{SUBSCRIPTION_PREFIX}[0-9a-f]{{{SUBSCRIPTION_HASH_CHARS}}}"
)
"""Exactly what :func:`subscription_id` produces: the prefix and 32 lowercase
hex characters."""
PRINCIPAL_BYTES = 16

# Longest envelope :meth:`Keyring.seal` produces, and the `authPassword` length
# Appwrite must accept (appwrite/appwrite#14293; today ~99 chars). The realistic
# worst case (300-char callback URL, a 64-byte secret, the longest catalog
# arguments) measures 1580 chars; a typical subscription (150-char URL, a
# 32-byte secret) about 1320. See tests/unit/test_events_envelope.py.
ENVELOPE_BUDGET = 2048

SIGNATURE_INFO = b"mcp-events/appwrite-signature/v1"

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MILLISECOND = timedelta(milliseconds=1)

Argument = str | int | bool | None


class Mode(StrEnum):
    """How a subscription's events reach the subscriber."""

    WEBHOOK = profile.WEBHOOK
    """Signed POSTs to the subscriber's callback URL."""
    PUSH = profile.PUSH
    """Notifications on the ``events/stream`` the subscriber holds open."""


class EnvelopeFailure(StrEnum):
    """Why an envelope did not open."""

    MALFORMED = "malformed"
    """Not a JWE Compact token with a readable key id."""
    KEY = "unknown_key"
    AUTHENTICATION = "authentication_failed"
    """Tampered, not the profile, or bound to another server, project or
    webhook."""
    BINDING = "binding_mismatch"
    """Opened, but its contents do not hash to the subscription id."""


class EnvelopeError(Exception):
    """An envelope that must not be trusted. The ingress drops the delivery."""

    def __init__(self, failure: EnvelopeFailure, message: str) -> None:
        super().__init__(message)
        self.failure = failure


class SubscriptionExpired(Exception):
    """The envelope opened and is bound to this webhook, but its lease has
    ended. The ingress acknowledges and drops the delivery."""


class EnvelopeTooLarge(ValueError):
    """The sealed subscription exceeds :data:`ENVELOPE_BUDGET`, almost always
    because of a very long callback URL. Subscribe rejects it as invalid params."""

    def __init__(self, length: int) -> None:
        super().__init__(
            f"sealed subscription is {length} chars; the limit is {ENVELOPE_BUDGET}"
        )
        self.length = length


class InvalidSubscription(ValueError):
    """The subscription is not a valid Subscription Record v1, e.g. a callback
    URL with credentials or a fragment, or a non-canonical secret."""


class KeyringError(ValueError):
    """``MCP_EVENTS_SEALING_KEYS`` is missing or invalid."""


def canonical_json(value: Any) -> bytes:
    """Sorted keys, compact separators, UTF-8, no NaN or Infinity."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def subscription_id(
    principal: str, callback: str, name: str, arguments: Mapping[str, Argument]
) -> str:
    """Deterministic id over (principal, callback URL, event name, arguments).

    A valid Appwrite custom id, used as the webhook ``$id``. Argument order does
    not change it."""
    material = canonical_json([principal, callback, name, dict(arguments)])
    digest = hashlib.sha256(material).hexdigest()
    return SUBSCRIPTION_PREFIX + digest[:SUBSCRIPTION_HASH_CHARS]


def stream_id() -> str:
    """A fresh id for a push subscription, in the :func:`subscription_id`
    format so its webhook is recognized as managed."""
    return SUBSCRIPTION_PREFIX + secrets.token_hex(SUBSCRIPTION_HASH_CHARS // 2)


def valid_subscription_id(value: str) -> bool:
    """Whether ``value`` is a subscription id this module could have produced."""
    return SUBSCRIPTION_ID_PATTERN.fullmatch(value) is not None


def timestamp(milliseconds: int) -> str:
    """UTC RFC 3339 with milliseconds and ``Z``, as the record's lease and
    ``refreshBefore`` are written."""
    moment = EPOCH + milliseconds * MILLISECOND
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def milliseconds(stamp: str) -> int:
    """Unix epoch milliseconds of a record timestamp."""
    return (datetime.fromisoformat(stamp) - EPOCH) // MILLISECOND


@dataclass(frozen=True)
class Principal:
    """The OAuth caller a subscription belongs to. Only its digest is kept."""

    issuer: str
    subject: str
    client: str

    @property
    def digest(self) -> str:
        """base64url of the first 128 bits of SHA-256 over (iss, sub, client_id)."""
        material = canonical_json([self.issuer, self.subject, self.client])
        return _encode(hashlib.sha256(material).digest()[:PRINCIPAL_BYTES])


@dataclass(frozen=True)
class Context:
    """What an envelope is bound to. When opening, every field comes from
    trusted configuration and routing, never from the envelope."""

    server: str
    """This deployment: ``MCP_PUBLIC_URL``."""
    tenant: str
    """The Appwrite project."""
    resource: str
    """The subscription id, which is also the webhook id."""

    def as_dict(self) -> dict[str, str]:
        return {
            "server": self.server,
            "tenant": self.tenant,
            "resource": self.resource,
        }


@dataclass(frozen=True)
class Subscription:
    """Everything the ingress needs to deliver one subscription's events: a
    typed view of the Subscription Record v1 that is sealed, plus the project
    it is bound to."""

    id: str
    project: str
    name: str
    """Event name, e.g. ``tablesdb.row.created``."""
    arguments: Mapping[str, Argument]
    """Event arguments, sorted by key and read-only."""
    callback: str | None
    """Delivery URL; ``None`` for a push subscription."""
    secret: str | None = field(repr=False)
    """The client's ``whsec_`` secret, ``None`` for a push subscription. Kept
    out of ``repr`` so logging a subscription never leaks it."""
    expires: int
    """Lease expiry as Unix epoch milliseconds."""
    refresh: int
    """``refreshBefore`` as Unix epoch milliseconds."""
    principal: str
    """:attr:`Principal.digest` of the subscriber."""

    def __post_init__(self) -> None:
        canonical = MappingProxyType(dict(sorted(self.arguments.items())))
        object.__setattr__(self, "arguments", canonical)
        if (self.callback is None) != (self.secret is None):
            raise ValueError("a webhook subscription needs a callback and a secret")
        if self.callback is None:
            # A push subscription's id is random, so only its shape is checked;
            # the envelope's context binds it to the webhook.
            if not valid_subscription_id(self.id):
                raise ValueError("push subscription id is invalid")
            return
        expected = subscription_id(
            self.principal, self.callback, self.name, self.arguments
        )
        if self.id != expected:
            raise ValueError("subscription id does not match its contents")

    @property
    def mode(self) -> Mode:
        return Mode.PUSH if self.callback is None else Mode.WEBHOOK

    @classmethod
    def create(
        cls,
        project: str,
        name: str,
        arguments: Mapping[str, Argument],
        callback: str,
        secret: str,
        expires: int,
        refresh: int,
        principal: str,
    ) -> Subscription:
        """Build a webhook subscription, deriving its id."""
        return cls(
            id=subscription_id(principal, callback, name, arguments),
            project=project,
            name=name,
            arguments=arguments,
            callback=callback,
            secret=secret,
            expires=expires,
            refresh=refresh,
            principal=principal,
        )

    @classmethod
    def stream(
        cls,
        project: str,
        name: str,
        arguments: Mapping[str, Argument],
        expires: int,
        refresh: int,
        principal: str,
    ) -> Subscription:
        """Build a push subscription with a fresh :func:`stream_id`."""
        return cls(
            id=stream_id(),
            project=project,
            name=name,
            arguments=arguments,
            callback=None,
            secret=None,
            expires=expires,
            refresh=refresh,
            principal=principal,
        )

    @classmethod
    def from_record(cls, record: Mapping[str, Any], project: str) -> Subscription:
        """The subscription of a validated record. Raises ``ValueError`` when
        its id does not match its contents."""
        delivery = record["delivery"]
        return cls(
            id=record["id"],
            project=project,
            name=record["event"]["name"],
            arguments=record["event"]["arguments"],
            callback=delivery.get("url"),
            secret=delivery.get("secret"),
            expires=milliseconds(record["lease"]["expiresAt"]),
            refresh=milliseconds(record["lease"]["refreshBefore"]),
            principal=record["principal"],
        )

    @property
    def record(self) -> dict[str, Any]:
        """The Subscription Record v1 that is sealed."""
        delivery: dict[str, Any] = {"mode": self.mode.value}
        if self.mode is Mode.WEBHOOK:
            delivery |= {"url": self.callback, "secret": self.secret}
        return {
            "version": RECORD_VERSION,
            "id": self.id,
            "principal": self.principal,
            "event": {"name": self.name, "arguments": dict(self.arguments)},
            "delivery": delivery,
            "lease": {
                "expiresAt": timestamp(self.expires),
                "refreshBefore": timestamp(self.refresh),
            },
            "extensions": {},
        }

    def context(self, server: str) -> Context:
        return Context(server=server, tenant=self.project, resource=self.id)


@dataclass(frozen=True)
class SealingKey:
    """One entry of the keyring: 32 random bytes, used as the JWE key and to
    derive the Appwrite signing key with HKDF."""

    id: str
    material: bytes

    def __post_init__(self) -> None:
        if KEY_ID_PATTERN.fullmatch(self.id) is None:
            raise KeyringError(
                f"sealing key id {self.id!r} must match {KEY_ID_PATTERN.pattern}"
            )
        if len(self.material) != KEY_BYTES:
            raise KeyringError(
                f"sealing key {self.id!r} must be {KEY_BYTES} bytes, "
                f"got {len(self.material)}"
            )
        if len(set(self.material)) < KEY_MIN_DISTINCT_BYTES:
            raise KeyringError(
                f"sealing key {self.id!r} is not random; generate one with "
                "`openssl rand -base64 32`"
            )

    @property
    def signing(self) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=SIGNATURE_INFO
        ).derive(self.material)


@dataclass(frozen=True)
class Keyring:
    """Sealing keys, the active (sealing) one first. Any of them opens."""

    keys: tuple[SealingKey, ...]

    def __post_init__(self) -> None:
        if not self.keys:
            raise KeyringError(f"{KEYS_ENV} must name at least one sealing key")
        ids = [key.id for key in self.keys]
        if len(set(ids)) != len(ids):
            raise KeyringError(f"{KEYS_ENV} repeats a sealing key id")

    @classmethod
    def parse(cls, text: str) -> Keyring:
        """Parse ``<id>:<base64 key>[,<id>:<base64 key>...]``."""
        keys: list[SealingKey] = []
        for entry in text.split(KEYS_SEPARATOR):
            entry = entry.strip()
            if not entry:
                continue
            key_id, separator, encoded = entry.partition(KEY_SEPARATOR)
            if not separator:
                raise KeyringError(
                    f"{KEYS_ENV} entries must look like <id>:<base64 key>"
                )
            try:
                material = _decode_key(encoded.strip())
            except ValueError as error:
                raise KeyringError(
                    f"sealing key {key_id.strip()!r} is not valid base64"
                ) from error
            keys.append(SealingKey(id=key_id.strip(), material=material))
        return cls(keys=tuple(keys))

    @classmethod
    def from_env(cls) -> Keyring:
        """Read the keyring from ``MCP_EVENTS_SEALING_KEYS``."""
        text = os.getenv(KEYS_ENV, "").strip()
        if not text:
            raise KeyringError(
                f"{KEYS_ENV} is not set; MCP Events needs at least one sealing "
                f"key, e.g. {KEYS_ENV}=k1:$(openssl rand -base64 32)"
            )
        return cls.parse(text)

    @property
    def active(self) -> SealingKey:
        return self.keys[0]

    @property
    def materials(self) -> dict[str, bytes]:
        """The ring as the package takes it: key id to key bytes."""
        return {key.id: key.material for key in self.keys}

    def get(self, key_id: str) -> SealingKey:
        for key in self.keys:
            if key.id == key_id:
                return key
        raise EnvelopeError(EnvelopeFailure.KEY, f"unknown sealing key {key_id!r}")

    def seal(self, subscription: Subscription, server: str) -> str:
        """Seal ``subscription`` with the active key, bound to ``server``, its
        project and its id.

        Raises :class:`InvalidSubscription` for a record the profile refuses
        and :class:`EnvelopeTooLarge` above :data:`ENVELOPE_BUDGET`."""
        try:
            envelope = profile.seal(
                subscription.record,
                self.materials,
                active_key=self.active.id,
                context=subscription.context(server).as_dict(),
            )
        except profile.RecordError as error:
            raise InvalidSubscription(str(error)) from error
        if len(envelope) > ENVELOPE_BUDGET:
            raise EnvelopeTooLarge(len(envelope))
        return envelope

    def record(
        self, envelope: str, context: Context, *, now: datetime | None = None
    ) -> dict[str, Any]:
        """The Subscription Record v1 in an envelope bound to ``context``, as
        the profile opens it.

        Raises :class:`EnvelopeError` when it is malformed, names an unknown
        key, was tampered with, or was sealed for another server, project or
        webhook, and :class:`SubscriptionExpired` once its lease has ended at
        ``now`` (default the current time; fixtures pin it)."""
        self.get(key_id(envelope))
        try:
            return profile.open_envelope(
                envelope,
                self.materials,
                expected_context=context.as_dict(),
                now=now,
            )
        except profile.ExpiredSubscription as error:
            raise SubscriptionExpired(str(error)) from error
        except profile.EnvelopeError as error:
            raise EnvelopeError(
                EnvelopeFailure.AUTHENTICATION,
                "envelope was tampered with or belongs to another webhook",
            ) from error

    def open(
        self, envelope: str, context: Context, *, now: datetime | None = None
    ) -> Subscription:
        """Open an envelope for the webhook and project in ``context``: the
        checks of :meth:`record`, then that its contents hash to the
        subscription id (``binding_mismatch`` otherwise)."""
        record = self.record(envelope, context, now=now)
        try:
            return Subscription.from_record(record, context.tenant)
        except ValueError as error:
            raise EnvelopeError(
                EnvelopeFailure.BINDING,
                "envelope contents do not match the subscription id",
            ) from error

    def signing_key(self, id: str, key_id: str | None = None) -> str:
        """Appwrite webhook ``secret`` for subscription ``id``: hex
        HMAC-SHA256 under the signing key derived from ``key_id`` (default the
        active key). 64 chars, within Appwrite's ``Text(256, 8)``. The ingress
        passes the key id of the delivery's envelope, since a webhook is always
        written with a matching envelope and secret."""
        key = self.get(key_id) if key_id is not None else self.active
        return hmac.new(key.signing, id.encode("utf-8"), hashlib.sha256).hexdigest()


def key_id(envelope: str) -> str:
    """The ``kid`` in an envelope's protected header, read without opening it.

    It only picks the key to try; the header is authenticated when the
    envelope opens. Raises :class:`EnvelopeError` (``malformed``) for anything
    that is not JWE Compact with a valid key id."""
    parts = envelope.split(".")
    if len(parts) != COMPACT_PARTS or parts[1]:
        raise EnvelopeError(EnvelopeFailure.MALFORMED, "envelope is not JWE Compact")
    try:
        header = json.loads(_decode(parts[0]))
    except ValueError as error:
        raise EnvelopeError(
            EnvelopeFailure.MALFORMED, "envelope header is not base64url JSON"
        ) from error
    kid = header.get("kid") if isinstance(header, dict) else None
    if not isinstance(kid, str) or KEY_ID_PATTERN.fullmatch(kid) is None:
        raise EnvelopeError(EnvelopeFailure.MALFORMED, "envelope key id is invalid")
    return kid


def appwrite_signature(url: str, body: bytes, key: str) -> str:
    """``X-Appwrite-Webhook-Signature``, computed the way Appwrite's webhooks
    worker does: ``base64_encode(hash_hmac('sha1', $url . $payload, $key, true))``.

    ``url`` is the webhook's configured URL (our ingress URL as registered), not
    the URL the request arrived on, which a proxy may have rewritten."""
    digest = appwrite_hmac(url, key)
    digest.update(body)
    return base64.b64encode(digest.digest()).decode("ascii")


def appwrite_hmac(url: str, key: str) -> hmac.HMAC:
    """The HMAC-SHA1 behind :func:`appwrite_signature`, already fed ``url``.

    Feed it the body as it streams in, so the ingress can check a delivery
    without buffering all of it."""
    return hmac.new(key.encode("utf-8"), url.encode("utf-8"), hashlib.sha1)


def verify_appwrite_signature(url: str, body: bytes, signature: str, key: str) -> bool:
    """Constant-time check of an ``X-Appwrite-Webhook-Signature`` header."""
    expected = appwrite_signature(url, body, key)
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8"))


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(text: str) -> bytes:
    if re.fullmatch(r"[A-Za-z0-9_-]*", text) is None:
        raise ValueError("invalid base64url")
    try:
        return base64.urlsafe_b64decode((text + "=" * (-len(text) % 4)).encode("ascii"))
    except binascii.Error as error:
        raise ValueError("invalid base64url") from error


def _decode_key(text: str) -> bytes:
    """Standard or URL-safe base64, with or without padding."""
    normalized = text.replace("-", "+").replace("_", "/")
    try:
        return base64.b64decode(
            normalized + "=" * (-len(normalized) % 4), validate=True
        )
    except binascii.Error as error:
        raise ValueError("invalid base64") from error
