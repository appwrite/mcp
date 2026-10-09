"""Sealed subscription envelopes for MCP Events.

The hosted server keeps no subscription store. Each subscription is one Appwrite
project webhook, and everything the ingress needs to deliver an event is sealed
into that webhook's ``authPassword``, which Appwrite stores encrypted, never
returns, and sends back as HTTP Basic auth on every delivery:

* The webhook ``$id`` is the deterministic :func:`subscription_id`, so a refresh
  is an upsert and an unsubscribe is a delete by id.
* ``authPassword`` holds the envelope from :meth:`Keyring.seal`:
  ``v1.<key id>.<base64url(nonce | ciphertext | tag)>``. The ciphertext is
  AES-256-GCM over compact JSON of the subscription. The subscription id and the
  project id are not in the plaintext; they are bound as associated data, so an
  envelope copied onto another webhook or project fails to open. Appwrite only
  sends Basic auth when ``authUsername`` is non-empty too, so the webhook needs
  a fixed, non-secret username alongside it.
* The webhook ``secret`` is :meth:`Keyring.signing_key`, derived from the
  sealing key and the subscription id, so the ingress can check
  ``X-Appwrite-Webhook-Signature`` (:func:`verify_appwrite_signature`) without
  storage.

Sealing keys come from ``MCP_EVENTS_SEALING_KEYS`` (``<id>:<base64 32 bytes>``,
comma separated, first one active). Every key in the ring opens envelopes, so a
rotation adds the new key in front and drops the old one once every subscription
has refreshed (the TTL cap bounds how long that takes).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEYS_ENV = "MCP_EVENTS_SEALING_KEYS"
VERSION = "v1"
SEPARATOR = "."
KEY_SEPARATOR = ":"
KEYS_SEPARATOR = ","
KEY_BYTES = 32
# A random 32-byte key has ~30 distinct byte values; fewer than this means a
# repeated or hand-typed key (all zeros, "aaaa…", a short passphrase).
KEY_MIN_DISTINCT_BYTES = 16
KEY_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
BASE64URL_PATTERN = re.compile(r"[A-Za-z0-9_-]*")
NONCE_BYTES = 12
TAG_BYTES = 16

SUBSCRIPTION_PREFIX = "sub_"
# Appwrite custom ids: at most 36 chars of [A-Za-z0-9._-], no leading special
# char. "sub_" + 32 hex chars (128 bits of SHA-256) fills that exactly.
SUBSCRIPTION_HASH_CHARS = 32
SUBSCRIPTION_ID_MAX = 36
SUBSCRIPTION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
PRINCIPAL_BYTES = 16

# Longest envelope :meth:`Keyring.seal` produces, and the `authPassword` length
# Appwrite must accept (appwrite/appwrite#14293; today ~99 chars). The realistic
# worst case (300-char callback URL, current and previous 64-byte secrets, the
# longest catalog arguments) measures 1034 chars; a typical subscription
# (150-char URL, one 32-byte secret) about 650. See tests/unit/test_events_envelope.py.
ENVELOPE_BUDGET = 2048

SEAL_INFO = b"mcp-events/seal/v1"
SIGNATURE_INFO = b"mcp-events/appwrite-signature/v1"

Argument = str | int | bool | None


class EnvelopeFailure(StrEnum):
    """Why an envelope did not open."""

    MALFORMED = "malformed"
    VERSION = "unsupported_version"
    KEY = "unknown_key"
    AUTHENTICATION = "authentication_failed"
    """Tampered, or sealed for a different subscription id or project."""
    BINDING = "binding_mismatch"
    """Opened, but its contents do not hash to the subscription id."""


class Field(StrEnum):
    """Plaintext keys, one letter each to keep the envelope small."""

    NAME = "n"
    ARGUMENTS = "a"
    CALLBACK = "c"
    SECRETS = "s"
    EXPIRES = "e"
    PRINCIPAL = "p"


class EnvelopeError(Exception):
    """An envelope that must not be trusted. The ingress drops the delivery."""

    def __init__(self, failure: EnvelopeFailure, message: str) -> None:
        super().__init__(message)
        self.failure = failure


class EnvelopeTooLarge(ValueError):
    """The sealed subscription exceeds :data:`ENVELOPE_BUDGET`, almost always
    because of a very long callback URL. Subscribe rejects it as invalid params."""

    def __init__(self, length: int) -> None:
        super().__init__(
            f"sealed subscription is {length} chars; the limit is {ENVELOPE_BUDGET}"
        )
        self.length = length


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


def valid_subscription_id(value: str) -> bool:
    """Whether ``value`` is a subscription id this module could have produced."""
    return (
        len(value) == SUBSCRIPTION_ID_MAX
        and value.startswith(SUBSCRIPTION_PREFIX)
        and SUBSCRIPTION_ID_PATTERN.fullmatch(value) is not None
    )


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
class Subscription:
    """Everything the ingress needs to deliver one subscription's events."""

    id: str
    project: str
    name: str
    """Event name, e.g. ``tablesdb.row.created``."""
    arguments: Mapping[str, Argument]
    """Event arguments, sorted by key and read-only."""
    callback: str
    """Delivery URL."""
    secrets: tuple[str, ...]
    """``whsec_`` secrets: the current one first, the previous one during rotation."""
    expires: int
    """Expiry as Unix epoch milliseconds."""
    principal: str
    """:attr:`Principal.digest` of the subscriber."""

    def __post_init__(self) -> None:
        canonical = MappingProxyType(dict(sorted(self.arguments.items())))
        object.__setattr__(self, "arguments", canonical)
        object.__setattr__(self, "secrets", tuple(self.secrets))
        if not self.secrets:
            raise ValueError("a subscription needs at least one secret")
        expected = subscription_id(
            self.principal, self.callback, self.name, self.arguments
        )
        if self.id != expected:
            raise ValueError("subscription id does not match its contents")

    @classmethod
    def create(
        cls,
        project: str,
        name: str,
        arguments: Mapping[str, Argument],
        callback: str,
        secrets: tuple[str, ...],
        expires: int,
        principal: str,
    ) -> Subscription:
        """Build a subscription, deriving its id."""
        return cls(
            id=subscription_id(principal, callback, name, arguments),
            project=project,
            name=name,
            arguments=arguments,
            callback=callback,
            secrets=secrets,
            expires=expires,
            principal=principal,
        )

    def expired(self, now: int | None = None) -> bool:
        """Whether the subscription has expired at ``now`` (epoch ms, default
        the current time). Kept apart from opening so the ingress can answer an
        expired delivery with a 2xx drop and an invalid one differently."""
        current = now if now is not None else time.time_ns() // 1_000_000
        return current >= self.expires


@dataclass(frozen=True)
class Envelope:
    """The parsed ``<version>.<key id>.<payload>`` form of a sealed envelope."""

    version: str
    key: str
    """Id of the sealing key; also selects the Appwrite signing key."""
    payload: bytes
    """``nonce | ciphertext | tag``."""

    @classmethod
    def parse(cls, text: str) -> Envelope:
        parts = text.split(SEPARATOR)
        if len(parts) != 3:
            raise EnvelopeError(EnvelopeFailure.MALFORMED, "envelope is malformed")
        version, key, payload = parts
        if version != VERSION:
            raise EnvelopeError(
                EnvelopeFailure.VERSION, f"unsupported envelope version {version!r}"
            )
        if KEY_ID_PATTERN.fullmatch(key) is None:
            raise EnvelopeError(EnvelopeFailure.MALFORMED, "envelope key id is invalid")
        try:
            raw = _decode(payload)
        except ValueError as error:
            raise EnvelopeError(
                EnvelopeFailure.MALFORMED, "envelope payload is not base64url"
            ) from error
        if len(raw) < NONCE_BYTES + TAG_BYTES:
            raise EnvelopeError(EnvelopeFailure.MALFORMED, "envelope is truncated")
        return cls(version=version, key=key, payload=raw)

    def __str__(self) -> str:
        return SEPARATOR.join((self.version, self.key, _encode(self.payload)))


@dataclass(frozen=True)
class SealingKey:
    """One entry of the keyring. ``material`` is the configured 32 bytes;
    separate encryption and signing keys are derived from it with HKDF."""

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
    def encryption(self) -> bytes:
        return _derive(self.material, SEAL_INFO)

    @property
    def signing(self) -> bytes:
        return _derive(self.material, SIGNATURE_INFO)


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

    def get(self, key_id: str) -> SealingKey:
        for key in self.keys:
            if key.id == key_id:
                return key
        raise EnvelopeError(EnvelopeFailure.KEY, f"unknown sealing key {key_id!r}")

    def seal(self, subscription: Subscription) -> str:
        """Seal ``subscription`` with the active key into an envelope string.

        Raises :class:`EnvelopeTooLarge` above :data:`ENVELOPE_BUDGET`."""
        key = self.active
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(key.encryption).encrypt(
            nonce,
            _plaintext(subscription),
            _associated(key.id, subscription.id, subscription.project),
        )
        envelope = str(Envelope(version=VERSION, key=key.id, payload=nonce + sealed))
        if len(envelope) > ENVELOPE_BUDGET:
            raise EnvelopeTooLarge(len(envelope))
        return envelope

    def open(self, envelope: str, id: str, project: str) -> Subscription:
        """Open an envelope for the webhook ``id`` in ``project``.

        Raises :class:`EnvelopeError` when it is malformed, names an unknown key
        or version, was tampered with, or was sealed for another webhook or
        project. Expiry is not checked here; see :meth:`Subscription.expired`."""
        parsed = Envelope.parse(envelope)
        key = self.get(parsed.key)
        nonce, sealed = parsed.payload[:NONCE_BYTES], parsed.payload[NONCE_BYTES:]
        try:
            plaintext = AESGCM(key.encryption).decrypt(
                nonce, sealed, _associated(key.id, id, project)
            )
        except InvalidTag as error:
            raise EnvelopeError(
                EnvelopeFailure.AUTHENTICATION,
                "envelope was tampered with or belongs to another webhook",
            ) from error
        try:
            fields = json.loads(plaintext)
            return Subscription(
                id=id,
                project=project,
                name=fields[Field.NAME],
                arguments=fields[Field.ARGUMENTS],
                callback=fields[Field.CALLBACK],
                secrets=tuple(fields[Field.SECRETS]),
                expires=fields[Field.EXPIRES],
                principal=fields[Field.PRINCIPAL],
            )
        except (ValueError, KeyError, TypeError) as error:
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


def appwrite_signature(url: str, body: bytes, key: str) -> str:
    """``X-Appwrite-Webhook-Signature``, computed the way Appwrite's webhooks
    worker does: ``base64_encode(hash_hmac('sha1', $url . $payload, $key, true))``.

    ``url`` is the webhook's configured URL (our ingress URL as registered), not
    the URL the request arrived on, which a proxy may have rewritten."""
    digest = hmac.new(key.encode("utf-8"), url.encode("utf-8") + body, hashlib.sha1)
    return base64.b64encode(digest.digest()).decode("ascii")


def verify_appwrite_signature(url: str, body: bytes, signature: str, key: str) -> bool:
    """Constant-time check of an ``X-Appwrite-Webhook-Signature`` header."""
    expected = appwrite_signature(url, body, key)
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8"))


def _plaintext(subscription: Subscription) -> bytes:
    return canonical_json(
        {
            Field.NAME: subscription.name,
            Field.ARGUMENTS: dict(subscription.arguments),
            Field.CALLBACK: subscription.callback,
            Field.SECRETS: list(subscription.secrets),
            Field.EXPIRES: subscription.expires,
            Field.PRINCIPAL: subscription.principal,
        }
    )


def _associated(key_id: str, id: str, project: str) -> bytes:
    return canonical_json([VERSION, key_id, id, project])


def _derive(material: bytes, info: bytes) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(), length=KEY_BYTES, salt=None, info=info
    ).derive(material)


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(text: str) -> bytes:
    if BASE64URL_PATTERN.fullmatch(text) is None:
        raise ValueError("invalid base64url")
    try:
        return base64.urlsafe_b64decode((text + "=" * (-len(text) % 4)).encode("ascii"))
    except (binascii.Error, UnicodeEncodeError) as error:
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
