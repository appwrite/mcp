"""``events/subscribe`` and ``events/unsubscribe`` on a stateless server.

A subscription is one managed Appwrite webhook in the subscriber's project (see
:mod:`.webhooks`), and nothing about it is stored here. ``events/subscribe``:

1. Validates the request: a catalog event (``-32011``), its arguments, a
   ``webhook`` delivery (``-32014`` otherwise), an ``https`` callback that
   resolves only to public addresses and a ``whsec_`` secret (``-32602``).
2. Takes the principal from the caller's OAuth token (``-32012`` without one),
   and refuses a token whose granted scopes cannot write webhooks (``-32012``)
   before anything reaches the callback.
3. Grants a TTL (:class:`Grant`) and seals the subscription into an envelope
   for this server (an oversized one, or one the subscription record refuses,
   is ``-32602``).
4. Authorizes: reads the event's resource in the project with the caller's own
   token (:class:`.catalog.Access`). Every refresh repeats it, so access that
   was revoked ends the subscription at the next refresh at the latest.
5. Lists the project's webhooks (which also proves the webhook scopes) and
   deletes this principal's managed ones that expired more than
   :data:`CLEANUP_GRACE_MS` ago and are unchanged when read again, so stale
   subscriptions do not hold Free-plan slots. Expiry and owner come from the
   webhook name (:class:`.webhooks.Label`), because the envelope cannot be
   read back.
6. Runs the verification handshake against the callback, every time
   (``-32015`` with ``data.reason`` on failure). The server keeps no
   per-``(principal, url)`` cache and cannot read the previous envelope back
   from Appwrite, so it cannot tell a known callback from a new one; one signed
   POST per refresh is cheap and always proves the callback still wants
   deliveries under the secret it is about to get.
7. Creates the subscription's webhook, or rewrites it on refresh.

A refresh with a new ``delivery.secret`` replaces the secret. The previous one
cannot be kept for dual signing: it lives only in the old envelope, which
Appwrite never returns. Deliveries already in flight were opened from the old
envelope and keep signing with the old secret through their retries.

Errors from Appwrite map onto the events codes; see :func:`_access_error` and
:func:`_webhook_error`. Creating is one call. A refresh writes the signing key
first and the envelope second, so a webhook never holds a signing key derived
from an older sealing key than its envelope's. When the second call fails, the
webhook keeps its previous envelope with the new signing key: the ingress
accepts the key of every sealing key in the ring, and the previous envelope's
sealing key stays in the ring until that envelope expires, so the webhook keeps
delivering until then. The error is reported so the client retries. The other
order could leave a new envelope with a signing key derived from a sealing key
that is retired while that envelope is still live.

``events/unsubscribe`` recomputes the id from the principal and the request and
deletes that webhook if it is a managed one. A missing webhook is still
success, so unsubscribe is idempotent; a missing project is ``-32012``, as on
subscribe. The id is bound to the principal, so a
caller can only ever reach their own subscriptions.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx
from anyio import to_thread
from appwrite_console.client import Client
from appwrite_console.exception import AppwriteException
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.types import RequestParams
from pydantic import ConfigDict

from .. import telemetry
from .catalog import Event, lookup
from .delivery import Callback, CallbackError, InvalidSecretError, decode_secret
from .egress import SECURE_SCHEME, DestinationError
from .envelope import (
    EnvelopeTooLarge,
    InvalidSubscription,
    Principal,
    Subscription,
    subscription_id,
    timestamp,
)
from .errors import CallbackFailure, EventsError
from .ingress import Ingress
from .webhooks import Label, Webhooks, managed, owner

WEBHOOK = "webhook"
"""The only delivery mode served (v1 is webhook-only, like ChatGPT)."""

TTL_DEFAULT_MS = 60 * 60 * 1000
"""Granted when the client suggests no TTL, or asks for none (``ttlMs: null``):
a server MUST NOT grant no-expiry without durable storage, and this one has
none."""

TTL_MIN_MS = 5 * 60 * 1000
"""Shorter suggestions are raised to this floor, which keeps refresh traffic
(and the verification POST each refresh sends) bounded."""

TTL_MAX_MS = 24 * 60 * 60 * 1000
"""Longer suggestions are capped here. A refresh is also the access re-check,
so this bounds how long revoked access can keep receiving events."""

REFRESH_SHARE = 10
"""``refreshBefore`` comes this fraction (1/10) of the grant before the
envelope expires: 6 minutes for the default hour, 30 s at the floor. The
margin absorbs a slow refresh without letting deliveries lapse."""

CLEANUP_GRACE_MS = 10 * 60 * 1000
"""How long past its expiry a managed webhook must be before a subscribe
cleans it up. A client still refreshing that subscription would have done so
well before; past this it has gone, and its deliveries are already dropped."""

PROJECT = "project_id"

SCOPE_PREFIX = "project:"
WEBHOOK_SCOPES = ("webhooks.read", "webhooks.write")
WRITE_GRANTS = frozenset({"all", "project:all", "project:webhooks.write"})
"""OAuth scopes any one of which lets a token write project webhooks."""


class Operation(StrEnum):
    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    CLEANUP = "cleanup"


class Outcome(StrEnum):
    CREATED = "created"
    REFRESHED = "refreshed"
    REMOVED = "removed"
    ABSENT = "absent"


class AppwriteError(StrEnum):
    """Appwrite error ``type`` values the mapping depends on."""

    UNAUTHORIZED_SCOPE = "general_unauthorized_scope"
    PROJECT_NOT_FOUND = "project_not_found"
    ALREADY_EXISTS = "webhook_already_exists"
    PLAN_LIMIT = "additional_resource_not_allowed"
    """Cloud's plan limit on webhooks per project (403)."""


class SubscribeParams(RequestParams):
    """``events/subscribe`` params. Every member is checked by hand so each
    problem gets its own error code and message; unknown members are
    tolerated, as the draft specification may add some."""

    model_config = ConfigDict(extra="allow")

    name: Any = None
    arguments: Any = None
    delivery: Any = None
    cursor: Any = None
    ttl_ms: Any = None
    max_age_ms: Any = None


class UnsubscribeParams(RequestParams):
    """``events/unsubscribe`` params."""

    model_config = ConfigDict(extra="allow")

    name: Any = None
    arguments: Any = None
    delivery: Any = None


@dataclass(frozen=True)
class Grant:
    """A granted subscription lifetime, in Unix epoch milliseconds."""

    expires: int
    """When the envelope stops delivering."""
    refresh: int
    """``refreshBefore``: when the client must have refreshed by."""

    @classmethod
    def of(cls, suggested: int | None, now: int) -> Grant:
        """Grant for a suggested TTL (``None``: no or a null suggestion)."""
        if suggested is None:
            ttl = TTL_DEFAULT_MS
        else:
            ttl = min(max(suggested, TTL_MIN_MS), TTL_MAX_MS)
        expires = now + ttl
        return cls(expires=expires, refresh=expires - ttl // REFRESH_SHARE)


ClientFactory = Callable[[str], Client]
"""Builds the Appwrite client for the current request's token, scoped to a
project (``server.resolve_client``)."""


@dataclass(frozen=True)
class State:
    """What :meth:`Subscriptions._prepare` learned about the project."""

    webhooks: Webhooks
    exists: bool
    """Whether the subscription's webhook is already there (a refresh)."""
    count: int
    """Webhooks in the project after cleanup."""


@dataclass(frozen=True)
class Target:
    """What a subscribe or unsubscribe request names, validated."""

    event: Event
    arguments: dict[str, str]
    delivery: Mapping[str, Any]

    @property
    def project(self) -> str:
        return self.arguments[PROJECT]


class Subscriptions:
    """Serves ``events/subscribe`` and ``events/unsubscribe`` for one
    :class:`Ingress`, whose keyring, URL, egress and dispatcher it shares."""

    def __init__(
        self,
        ingress: Ingress,
        client: ClientFactory,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ingress = ingress
        self._client = client
        self._clock = clock

    def _now(self) -> int:
        return int(self._clock() * 1000)

    async def subscribe(self, params: SubscribeParams) -> dict[str, Any]:
        target = _target(params.name, params.arguments, params.delivery)
        mode = target.delivery.get("mode", WEBHOOK)
        if mode != WEBHOOK:
            raise EventsError.unsupported(
                f"Delivery mode {mode!r} is not supported; use 'webhook'"
            )
        callback = _callback(target.delivery)
        secret = target.delivery.get("secret")
        if not isinstance(secret, str):
            raise EventsError.invalid_params("delivery.secret is required")
        try:
            decode_secret(secret)
        except InvalidSecretError as error:
            raise EventsError.invalid_params(f"delivery.secret: {error}") from error
        suggested = _ttl(params)
        _max_age(params.max_age_ms)
        principal = _principal()
        _writable()

        grant = Grant.of(suggested, self._now())
        subscription = Subscription.create(
            project=target.project,
            name=target.event.name,
            arguments=target.arguments,
            callback=callback,
            secret=secret,
            expires=grant.expires,
            refresh=grant.refresh,
            principal=principal,
        )
        try:
            envelope = self._ingress.keyring.seal(subscription, self._ingress.server)
        except EnvelopeTooLarge as error:
            raise EventsError.invalid_params(
                "delivery.url is too long to fit in the subscription"
            ) from error
        except InvalidSubscription as error:
            raise EventsError.invalid_params(
                "delivery is not a valid subscription: the url must be https "
                "without credentials or a fragment, and the secret canonical "
                "padded base64"
            ) from error
        await self._check(callback)

        state = await to_thread.run_sync(
            self._prepare, target, subscription, abandon_on_cancel=True
        )
        await self._verify(subscription)
        outcome = await to_thread.run_sync(
            self._write, state, target, subscription, envelope, abandon_on_cancel=True
        )
        telemetry.record_subscription(Operation.SUBSCRIBE, outcome, target.event.name)
        _log(f"{subscription.id}: {outcome} {target.event.name} until {grant.expires}")
        return {
            "id": subscription.id,
            "refreshBefore": timestamp(grant.refresh),
            "cursor": None,
            "truncated": False,
        }

    async def unsubscribe(self, params: UnsubscribeParams) -> dict[str, Any]:
        target = _target(params.name, params.arguments, params.delivery)
        url = target.delivery.get("url")
        if not isinstance(url, str) or not url:
            raise EventsError.invalid_params("delivery.url is required")
        principal = _principal()
        id = subscription_id(principal, url, target.event.name, target.arguments)
        outcome = await to_thread.run_sync(
            self._remove, target, id, abandon_on_cancel=True
        )
        telemetry.record_subscription(Operation.UNSUBSCRIBE, outcome, target.event.name)
        _log(f"{id}: unsubscribe {outcome}")
        return {}

    async def _check(self, url: str) -> None:
        """Refuse a callback whose host is not public before anything is sent
        to Appwrite or to the callback. The connect-time check still runs on
        every request."""
        try:
            await self._ingress.egress.check(url)
        except DestinationError as error:
            raise EventsError.invalid_params(f"delivery.url: {error}") from error
        except TimeoutError as error:
            raise EventsError.callback_endpoint(
                "The callback host did not resolve in time",
                reason=CallbackFailure.TIMEOUT,
            ) from error
        except OSError as error:
            raise EventsError.invalid_params(
                "delivery.url: Callback host did not resolve"
            ) from error

    async def _verify(self, subscription: Subscription) -> None:
        callback = Callback(subscription.callback, subscription.id, subscription.secret)
        try:
            await self._ingress.dispatcher.verify(callback)
        except CallbackError as error:
            raise EventsError.callback_endpoint(
                f"The callback URL failed verification ({error.reason})",
                reason=error.reason,
            ) from error

    def _project_client(self, project: str) -> Client:
        try:
            return self._client(project)
        except RuntimeError as error:
            raise EventsError.forbidden(
                "Event subscriptions need an Appwrite OAuth access token"
            ) from error

    def _authorize(self, target: Target) -> Client:
        """The project client, once the caller has proven they may read the
        event's resource."""
        client = self._project_client(target.project)
        access = target.event.access
        try:
            client.call(
                "get",
                access.resolve(target.arguments),
                headers={"accept": "application/json"},
                params=dict(access.params),
            )
        except AppwriteException as error:
            raise _access_error(error, target) from error
        return client

    def _prepare(self, target: Target, subscription: Subscription) -> State:
        """Everything that needs Appwrite before the handshake: authorize,
        read the project's webhooks (which also proves the webhook scopes) and
        clean up this principal's expired ones. A refusal here costs the
        callback nothing."""
        webhooks = Webhooks(self._authorize(target))
        try:
            existing = webhooks.listing()
        except AppwriteException as error:
            raise _webhook_error(error, target.project, 0) from error
        current = next((w for w in existing if w.id == subscription.id), None)
        if current is not None and managed(current) is None:
            raise _foreign(subscription.id, target.project)
        removed = self._clean(webhooks, existing, subscription)
        return State(webhooks, current is not None, len(existing) - removed)

    def _write(
        self,
        state: State,
        target: Target,
        subscription: Subscription,
        envelope: str,
    ) -> Outcome:
        webhooks, count = state.webhooks, state.count
        label = Label(
            target.event.name, subscription.expires, owner(subscription.principal)
        )
        fields: dict[str, Any] = {
            "url": self._ingress.url(subscription.id),
            "label": label,
            "events": target.event.patterns(target.arguments),
            "password": envelope,
        }
        secret = self._ingress.keyring.signing_key(subscription.id)
        if not state.exists:
            try:
                webhooks.create(subscription.id, secret=secret, **fields)
                return Outcome.CREATED
            except AppwriteException as error:
                if error.type != AppwriteError.ALREADY_EXISTS:
                    raise _webhook_error(error, target.project, count) from error
            # Created concurrently by another subscribe: rewrite it below.
            try:
                current = webhooks.get(subscription.id)
            except AppwriteException as error:
                raise _webhook_error(error, target.project, count) from error
            if current is not None and managed(current) is None:
                raise _foreign(subscription.id, target.project)
        # The signing key goes first; see the module docstring for why.
        try:
            webhooks.sign(subscription.id, secret)
        except AppwriteException as error:
            raise _webhook_error(error, target.project, count) from error
        try:
            webhooks.update(subscription.id, **fields)
        except AppwriteException as error:
            # The new signing key is written and the previous envelope stays.
            # Report the failure so the client retries, but do not turn a
            # transient error into a lost subscription.
            raise _webhook_error(error, target.project, count) from error
        return Outcome.REFRESHED

    def _clean(
        self, webhooks: Webhooks, existing: list[Any], subscription: Subscription
    ) -> int:
        """Delete this principal's long-expired managed webhooks; return how
        many went. Best effort: a failed delete is retried on the next
        subscribe.

        Appwrite has no conditional delete, so a refresh of a candidate can
        race this. Two guards keep that from deleting a live subscription:
        only webhooks expired for longer than :data:`CLEANUP_GRACE_MS` are
        candidates, and each is read again right before the delete and kept
        if its name or ``$updatedAt`` changed since the listing. What remains
        is a refresh landing between that read and the delete, of a
        subscription its client left expired for over ten minutes; the client
        then gets its webhook back on the following refresh."""
        tag = owner(subscription.principal)
        cutoff = self._now() - CLEANUP_GRACE_MS
        removed = 0
        for webhook in existing:
            label = managed(webhook)
            if label is None or webhook.id == subscription.id:
                continue
            if label.owner != tag or not label.expired(cutoff):
                continue
            try:
                current = webhooks.get(webhook.id)
            except AppwriteException as error:
                _log(f"{webhook.id}: cleanup failed ({error.code} {error.type})")
                continue
            if current is None or (current.name, current.updatedat) != (
                webhook.name,
                webhook.updatedat,
            ):
                continue
            try:
                webhooks.delete(webhook.id)
            except AppwriteException as error:
                _log(f"{webhook.id}: cleanup failed ({error.code} {error.type})")
                continue
            removed += 1
            telemetry.record_subscription(
                Operation.CLEANUP, Outcome.REMOVED, label.event
            )
            _log(f"{webhook.id}: removed expired {label.event}")
        return removed

    def _remove(self, target: Target, id: str) -> Outcome:
        webhooks = Webhooks(self._project_client(target.project))
        try:
            webhook = webhooks.get(id)
            if webhook is None or managed(webhook) is None:
                return Outcome.ABSENT
            return Outcome.REMOVED if webhooks.delete(id) else Outcome.ABSENT
        except AppwriteException as error:
            raise _webhook_error(error, target.project, 0) from error


def _target(name: object, arguments: object, delivery: object) -> Target:
    event = lookup(name)
    validated = event.validate(arguments)
    if not isinstance(delivery, Mapping):
        raise EventsError.invalid_params("delivery must be an object")
    return Target(event, validated, delivery)


def _callback(delivery: Mapping[str, Any]) -> str:
    url = delivery.get("url")
    if not isinstance(url, str) or not url:
        raise EventsError.invalid_params("delivery.url is required")
    try:
        scheme = httpx.URL(url).scheme
    except (httpx.InvalidURL, TypeError) as error:
        raise EventsError.invalid_params("delivery.url is not a valid URL") from error
    if scheme != SECURE_SCHEME:
        raise EventsError.invalid_params("delivery.url must use https")
    return url


def _ttl(params: SubscribeParams) -> int | None:
    """The suggested TTL in milliseconds; ``None`` when it is absent or
    ``null`` (both get the default grant)."""
    value = params.ttl_ms
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise EventsError.invalid_params("ttlMs must be a positive integer or null")
    return value


def _max_age(value: object) -> None:
    """``maxAgeMs`` bounds replay. Appwrite webhooks have none, so it is only
    checked for shape."""
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EventsError.invalid_params(
            "maxAgeMs must be a non-negative integer or null"
        )


def _writable() -> None:
    """Refuse a token whose granted scopes cannot write webhooks before the
    callback gets a verification POST.

    Listing the webhooks proves ``webhooks.read`` only, and Appwrite has no
    side-effect-free way to prove ``webhooks.write``; the token's ``scope``
    claim does. A token that carries no scopes at all is left to Appwrite,
    which refuses the write itself."""
    token = get_access_token()
    scopes = set(token.scopes) if token is not None else set()
    if not scopes or scopes & WRITE_GRANTS:
        return
    raise EventsError.forbidden(_scopes_message())


def _scopes_message() -> str:
    scopes = " and ".join(SCOPE_PREFIX + scope for scope in WEBHOOK_SCOPES)
    return (
        f"Event subscriptions need the {scopes} scopes. Reconnect the "
        "Appwrite connector and grant them."
    )


def _principal() -> str:
    """Digest of the OAuth principal behind the current request."""
    token = get_access_token()
    claims = (token.claims or {}) if token is not None else {}
    issuer = claims.get("iss")
    subject = (token.subject if token is not None else None) or claims.get("sub")
    client = token.client_id if token is not None else None
    if not all(isinstance(part, str) and part for part in (issuer, subject, client)):
        raise EventsError.forbidden(
            "Event subscriptions need an Appwrite OAuth access token for a user"
        )
    return Principal(str(issuer), str(subject), str(client)).digest


def _foreign(id: str, project: str) -> EventsError:
    """The subscription's webhook id is taken by a webhook whose name is not
    ours (someone renamed it). It is left alone."""
    return EventsError.forbidden(
        f"Webhook {id} in project {project} is not managed by MCP events. "
        "Delete it in the Appwrite Console, then subscribe again."
    )


def _access_error(error: AppwriteException, target: Target) -> EventsError:
    """Map a failed authorization read.

    Appwrite authenticates the caller against the project before it looks up
    the resource: a token without access to the project gets ``401`` (or
    ``404 project_not_found`` for a project that does not exist, which is
    reported the same way so project ids cannot be probed). So a ``404`` for
    the resource itself means the caller has project access and the function,
    site, table or bucket is missing: NotFound with ``data.kind = "resource"``.
    """
    access = target.event.access
    resource = access.describe(target.arguments)
    code = error.code if isinstance(error.code, int) else 0
    if code == 404 and error.type != AppwriteError.PROJECT_NOT_FOUND:
        return EventsError.not_found(
            f"Appwrite has no {resource} in project {target.project}",
            kind="resource",
        )
    if code in (401, 403, 404):
        if error.type == AppwriteError.UNAUTHORIZED_SCOPE:
            return EventsError.forbidden(
                f"Reading {resource} needs the {SCOPE_PREFIX}{access.scope} scope. "
                "Reconnect the Appwrite connector and grant it."
            )
        return EventsError.forbidden(
            f"You do not have access to {resource} in project {target.project}"
        )
    return EventsError.internal(
        f"Appwrite failed to read {resource} (HTTP {code or 'error'})"
    )


def _webhook_error(error: AppwriteException, project: str, count: int) -> EventsError:
    """Map a failed webhook call. ``count`` is how many webhooks the project
    has, which is the plan's maximum when the plan limit is what failed."""
    code = error.code if isinstance(error.code, int) else 0
    if error.type == AppwriteError.PLAN_LIMIT:
        return EventsError.resource_exhausted(
            f"Project {project} has reached its plan's limit of {count} webhooks, "
            "and every event subscription uses one. On the Free plan, upgrade to "
            "Pro for unlimited webhooks, or unsubscribe from another event first.",
            limit="webhooks",
            maximum=count,
        )
    # A project that does not exist is reported like one the caller cannot
    # reach, as on subscribe, so project ids cannot be probed.
    if code in (401, 403) or error.type == AppwriteError.PROJECT_NOT_FOUND:
        if error.type == AppwriteError.UNAUTHORIZED_SCOPE:
            return EventsError.forbidden(_scopes_message())
        return EventsError.forbidden(f"You cannot manage webhooks in project {project}")
    return EventsError.internal(
        f"Appwrite could not save the subscription (HTTP {code or 'error'})"
    )


def _log(message: str) -> None:
    print(f"[appwrite-mcp][events] {message}", file=sys.stderr, flush=True)
