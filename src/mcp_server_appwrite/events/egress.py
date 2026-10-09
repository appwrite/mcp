"""SSRF-safe outbound HTTP for webhook callbacks.

Callback URLs are chosen by MCP clients, so every request we send to one is an
attacker-steerable request from inside our network. This module is the only
path those requests take, for verification handshakes and event deliveries
alike, and it enforces the MCP Events "Webhook Security" rules:

* ``https`` only. Plain ``http`` is accepted solely through the explicit
  ``allow_loopback`` constructor flag, which tests use to reach a local server;
  nothing reads it from the environment.
* We resolve DNS ourselves and refuse the request if *any* resolved address is
  not globally routable (IANA IPv4/IPv6 special-purpose registries, plus the
  IPv6 forms that embed an IPv4 address).
* The check happens at connect time, inside the httpcore network backend, and
  the socket is opened to the address we just checked. The hostname never
  reaches a second resolver, so DNS rebinding between check and connect is not
  possible. TLS SNI and the ``Host`` header still carry the original hostname,
  because httpcore derives both from the request URL, not from the address the
  backend connected to.
* Redirects are never followed, the whole request is bounded by one deadline,
  response bodies are read up to a small cap, and concurrent requests to one
  destination host are limited so a single subscriber cannot monopolise egress.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import typing
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import TracebackType

import anyio
import httpcore
import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Maps (host, port) to every address the host resolves to.
Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]

SECURE_SCHEME = "https"
INSECURE_SCHEME = "http"

# Total wall-clock budget for one request: connect, TLS, send and read.
TIMEOUT_SECONDS = 10.0

# Receivers answer with an acknowledgement or a challenge echo; anything past
# this is discarded unread.
BODY_LIMIT_BYTES = 16 * 1024

# Concurrent in-flight requests allowed per destination host.
CONCURRENCY = 4

# Special-purpose ranges that are never a valid webhook destination. The
# ``ipaddress`` predicates already cover most of these; listing them keeps the
# policy explicit and independent of Python patch-level registry updates.
BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),  # "this network"
    ipaddress.ip_network("10.0.0.0/8"),  # private
    ipaddress.ip_network("100.64.0.0/10"),  # carrier-grade NAT
    ipaddress.ip_network("127.0.0.0/8"),  # loopback
    ipaddress.ip_network("169.254.0.0/16"),  # link-local, cloud metadata
    ipaddress.ip_network("172.16.0.0/12"),  # private
    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.ip_network("192.0.2.0/24"),  # documentation
    ipaddress.ip_network("192.88.99.0/24"),  # deprecated 6to4 relay anycast
    ipaddress.ip_network("192.168.0.0/16"),  # private
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("198.51.100.0/24"),  # documentation
    ipaddress.ip_network("203.0.113.0/24"),  # documentation
    ipaddress.ip_network("224.0.0.0/4"),  # multicast
    ipaddress.ip_network("240.0.0.0/4"),  # reserved, includes broadcast
    ipaddress.ip_network("::/96"),  # unspecified, loopback, IPv4-compatible
    ipaddress.ip_network("64:ff9b:1::/48"),  # local-use NAT64
    ipaddress.ip_network("100::/64"),  # discard-only
    ipaddress.ip_network("2001::/23"),  # IETF protocol assignments, Teredo
    ipaddress.ip_network("2001:db8::/32"),  # documentation
    ipaddress.ip_network("2002::/16"),  # 6to4, routes via arbitrary relays
    ipaddress.ip_network("3fff::/20"),  # documentation
    ipaddress.ip_network("fc00::/7"),  # unique local
    ipaddress.ip_network("fe80::/10"),  # link-local
    ipaddress.ip_network("fec0::/10"),  # deprecated site-local
    ipaddress.ip_network("ff00::/8"),  # multicast
)

# Well-known NAT64 prefix: the low 32 bits are the IPv4 destination.
NAT64_NETWORK = ipaddress.IPv6Network("64:ff9b::/96")


class DestinationError(Exception):
    """The callback URL or an address it resolves to is not a permitted target."""


def permitted(address: IPAddress, *, allow_loopback: bool = False) -> bool:
    """Whether ``address`` is a globally routable webhook destination.

    IPv6 forms that carry an IPv4 destination (IPv4-mapped and NAT64) are
    judged by the embedded address, so ``::ffff:10.0.0.1`` is as blocked as
    ``10.0.0.1``. ``allow_loopback`` admits loopback only (for tests); every
    other special-purpose range stays blocked.
    """
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return permitted(address.ipv4_mapped, allow_loopback=allow_loopback)
        if address in NAT64_NETWORK:
            embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
            return permitted(embedded)
    if allow_loopback and address.is_loopback:
        return True
    if any(address in network for network in BLOCKED_NETWORKS):
        return False
    return address.is_global and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_url(url: str, *, allow_loopback: bool = False) -> httpx.URL:
    """Parse ``url`` and enforce the scheme and host rules for callbacks.

    This is the syntactic half of the policy; address checks happen when the
    connection is opened. Raises :class:`DestinationError`.
    """
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError) as error:
        raise DestinationError("Callback URL is not a valid URL") from error
    schemes = {SECURE_SCHEME, INSECURE_SCHEME} if allow_loopback else {SECURE_SCHEME}
    if parsed.scheme not in schemes:
        raise DestinationError("Callback URL must use https")
    if not parsed.host:
        raise DestinationError("Callback URL must include a host")
    if parsed.userinfo:
        raise DestinationError("Callback URL must not include credentials")
    return parsed


async def resolve(host: str, port: int) -> list[IPAddress]:
    """Resolve ``host`` to every address it has, without connecting."""
    results = await anyio.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[IPAddress] = []
    for *_, sockaddr in results:
        address = ipaddress.ip_address(sockaddr[0])
        if address not in addresses:
            addresses.append(address)
    return addresses


class Backend(httpcore.AsyncNetworkBackend):
    """httpcore network backend that connects only to checked addresses.

    httpcore hands us the URL's hostname; we resolve it, reject the whole
    request if any address is not permitted, and open the TCP stream to the
    checked IP. httpcore then starts TLS with ``server_hostname`` taken from
    the request origin, so SNI and certificate validation use the hostname.
    """

    def __init__(
        self,
        resolver: Resolver = resolve,
        *,
        allow_loopback: bool = False,
        network: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._resolver = resolver
        self._allow_loopback = allow_loopback
        # httpcore exports AnyIOBackend behind an import guard, which hides its
        # base class from type checkers; anyio is a hard dependency here.
        self._network = network or typing.cast(
            httpcore.AsyncNetworkBackend, httpcore.AnyIOBackend()
        )

    async def addresses(self, host: str, port: int) -> list[IPAddress]:
        """Resolve ``host`` and return its addresses if all are permitted."""
        addresses = await self._resolver(host, port)
        if not addresses:
            raise DestinationError("Callback host did not resolve")
        for address in addresses:
            if not permitted(address, allow_loopback=self._allow_loopback):
                raise DestinationError("Callback host resolves to a non-public address")
        return addresses

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        with anyio.fail_after(timeout):
            addresses = await self.addresses(host, port)
        failure: Exception | None = None
        for address in addresses:
            try:
                return await self._network.connect_tcp(
                    str(address),
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as error:
                failure = error
        assert failure is not None
        raise failure

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise DestinationError("Unix sockets are not a webhook destination")

    async def sleep(self, seconds: float) -> None:
        await self._network.sleep(seconds)


class Transport(httpx.AsyncHTTPTransport):
    """httpx transport whose connection pool dials through :class:`Backend`.

    Proxies are deliberately unsupported: a proxy would resolve the hostname
    itself and defeat the connect-time address check.
    """

    def __init__(
        self,
        backend: Backend,
        *,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context or httpx.create_ssl_context(trust_env=False),
            network_backend=backend,
        )


class Limiter:
    """Bounds concurrent requests per destination host.

    Entries exist only while a host has requests waiting or in flight, so the
    map does not grow with the number of distinct callback hosts seen.
    """

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._semaphores: dict[str, anyio.Semaphore] = {}
        self._users: dict[str, int] = {}

    @asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        semaphore = self._semaphores.get(host)
        if semaphore is None:
            semaphore = anyio.Semaphore(self._capacity)
            self._semaphores[host] = semaphore
        self._users[host] = self._users.get(host, 0) + 1
        try:
            async with semaphore:
                yield
        finally:
            self._users[host] -= 1
            if self._users[host] == 0:
                del self._users[host]
                del self._semaphores[host]


@dataclass(frozen=True)
class Response:
    """Status and (capped) body of a callback response."""

    status: int
    body: bytes


class Egress:
    """The single outbound HTTP path for webhook callbacks.

    Create one per process and share it; it owns a pooled ``httpx`` client.
    ``transport`` replaces the SSRF-checking transport entirely and exists for
    tests that stub the network with ``httpx.MockTransport``.
    """

    def __init__(
        self,
        *,
        timeout: float = TIMEOUT_SECONDS,
        body_limit: int = BODY_LIMIT_BYTES,
        concurrency: int = CONCURRENCY,
        resolver: Resolver = resolve,
        allow_loopback: bool = False,
        ssl_context: ssl.SSLContext | None = None,
        network: httpcore.AsyncNetworkBackend | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout = timeout
        self._body_limit = body_limit
        self._allow_loopback = allow_loopback
        self._limiter = Limiter(concurrency)
        self._backend = Backend(
            resolver, allow_loopback=allow_loopback, network=network
        )
        self._client = httpx.AsyncClient(
            transport=transport or Transport(self._backend, ssl_context=ssl_context),
            follow_redirects=False,
            timeout=httpx.Timeout(timeout),
            trust_env=False,
        )

    async def check(self, url: str) -> None:
        """Validate ``url`` and its current DNS answers without connecting.

        Lets the subscribe path reject an obviously bad callback early. It is
        not a substitute for the connect-time check, which runs on every
        request regardless.
        """
        parsed = validate_url(url, allow_loopback=self._allow_loopback)
        port = parsed.port or (443 if parsed.scheme == SECURE_SCHEME else 80)
        with anyio.fail_after(self._timeout):
            await self._backend.addresses(parsed.host, port)

    async def post(
        self, url: str, content: bytes, headers: Mapping[str, str]
    ) -> Response:
        """POST ``content`` to ``url`` under the egress policy.

        Raises :class:`DestinationError` for a forbidden URL or address,
        ``TimeoutError`` when the deadline passes, and ``httpx.TransportError``
        for network and TLS failures.
        """
        parsed = validate_url(url, allow_loopback=self._allow_loopback)
        # Identity encoding keeps the body cap meaningful: a compressed reply
        # cannot expand past it.
        request_headers = {**headers, "Accept-Encoding": "identity"}
        async with self._limiter.slot(parsed.host):
            with anyio.fail_after(self._timeout):
                async with self._client.stream(
                    "POST", parsed, content=content, headers=request_headers
                ) as response:
                    body = await self._read(response)
        return Response(status=response.status_code, body=body)

    async def _read(self, response: httpx.Response) -> bytes:
        # Raw bytes, not decoded ones: a body sent with Content-Encoding despite
        # our identity request is cut at the cap instead of being inflated.
        if response.is_stream_consumed:
            # In-memory responses (e.g. from httpx.MockTransport) arrive read.
            return response.content[: self._body_limit]
        body = bytearray()
        async for chunk in response.aiter_raw():
            body.extend(chunk)
            if len(body) >= self._body_limit:
                break
        return bytes(body[: self._body_limit])

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> typing.Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
