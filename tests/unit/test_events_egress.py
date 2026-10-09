"""SSRF rules that need a controlled resolver or address table.

Against a real local HTTPS receiver, the ingress e2e flows (#134) cover: SNI and
``Host`` keep the hostname while the socket dials the checked address,
redirects are not followed, loopback is refused without the test-only flag, an
untrusted certificate is a ``tls_error``, the total timeout, and the
per-host concurrency limit. The subscribe e2e flows cover callback URL shapes
(scheme, credentials, missing host, private and metadata addresses). What stays
here cannot be produced end to end:

* The blocklist across IPv4, IPv6, IPv4-mapped, NAT64, 6to4 and Teredo forms:
  a test machine cannot route to most of these addresses.
* Mixed public and private DNS answers and DNS rebinding: they need a resolver
  that answers differently between check and connect.
* The response body cap: delivery discards response bodies, so it is
  invisible on the wire.
* Plain ``http`` under ``allow_loopback``: the flag is test-only, and no test
  can safely prove that a public ``http`` callback is refused by sending to
  one, so the scheme and resolution rules are checked here.
* The deadline covering the wait for a per-host slot, and headers (the
  signature timestamp) built only once the slot is held, at a precision the
  wire's whole-second ``webhook-timestamp`` cannot show.
"""

import ipaddress
import time
import unittest

import anyio
import httpcore
import httpx

from mcp_server_appwrite.events import egress

PUBLIC = ipaddress.ip_address("93.184.216.34")
PRIVATE = ipaddress.ip_address("10.0.0.7")

BLOCKED = [
    "0.0.0.0",
    "0.1.2.3",
    "10.1.2.3",
    "100.64.0.1",
    "100.127.255.254",
    "127.0.0.1",
    "127.8.8.8",
    "169.254.169.254",
    "172.16.0.1",
    "172.31.255.255",
    "192.0.0.8",
    "192.0.2.1",
    "192.88.99.1",
    "192.168.1.1",
    "198.18.0.1",
    "198.51.100.1",
    "203.0.113.1",
    "224.0.0.1",
    "239.255.255.250",
    "240.0.0.1",
    "255.255.255.255",
    "::",
    "::1",
    "::8.8.8.8",  # IPv4-compatible (deprecated)
    "::ffff:127.0.0.1",  # IPv4-mapped loopback
    "::ffff:10.0.0.1",  # IPv4-mapped private
    "::ffff:169.254.169.254",  # IPv4-mapped metadata service
    "::ffff:100.64.0.1",  # IPv4-mapped CGNAT
    "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
    "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
    "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
    "64:ff9b:1::1",  # local-use NAT64
    "2002:a00:1::1",  # 6to4 of 10.0.0.1
    "2002:7f00:1::1",  # 6to4 of 127.0.0.1
    "2001::1",  # Teredo
    "2001:db8::1",  # documentation
    "3fff::1",  # documentation
    "100::1",  # discard-only
    "fc00::1",  # unique local
    "fd12:3456::1",  # unique local
    "fe80::1",  # link-local
    "febf::1",  # link-local
    "fec0::1",  # site-local
    "ff02::1",  # multicast
]

ALLOWED = [
    "8.8.8.8",
    "1.1.1.1",
    "93.184.216.34",
    "2606:4700:4700::1111",
    "2a00:1450:4001::200e",
    "::ffff:8.8.8.8",  # IPv4-mapped public
    "64:ff9b::808:808",  # NAT64 of 8.8.8.8
]


def static(*addresses: ipaddress.IPv4Address | ipaddress.IPv6Address):
    async def resolver(host: str, port: int):
        return list(addresses)

    return resolver


class SpyNetwork(httpcore.AsyncNetworkBackend):
    """Records connect targets and refuses every connection."""

    def __init__(self) -> None:
        self.targets: list[tuple[str, int]] = []

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        self.targets.append((host, port))
        raise httpcore.ConnectError("refused by spy")


class PermittedTest(unittest.TestCase):
    def test_blocks_special_purpose_addresses(self) -> None:
        for text in BLOCKED:
            with self.subTest(address=text):
                self.assertFalse(egress.permitted(ipaddress.ip_address(text)))

    def test_allows_global_addresses(self) -> None:
        for text in ALLOWED:
            with self.subTest(address=text):
                self.assertTrue(egress.permitted(ipaddress.ip_address(text)))

    def test_allow_loopback_admits_only_loopback(self) -> None:
        for text in ["127.0.0.1", "::1", "::ffff:127.0.0.1"]:
            with self.subTest(address=text):
                self.assertTrue(
                    egress.permitted(ipaddress.ip_address(text), allow_loopback=True)
                )
        for text in ["10.0.0.1", "169.254.169.254", "fd00::1", "64:ff9b::7f00:1"]:
            with self.subTest(address=text):
                self.assertFalse(
                    egress.permitted(ipaddress.ip_address(text), allow_loopback=True)
                )


class LoopbackHttpTest(unittest.IsolatedAsyncioTestCase):
    """``allow_loopback`` admits plain ``http`` for a loopback host only."""

    def test_http_needs_a_loopback_host(self) -> None:
        for url in (
            "http://localhost:8080/x",
            "http://api.localhost/x",
            "http://127.8.8.8/x",
            "http://[::1]/x",
        ):
            with self.subTest(url=url):
                egress.validate_url(url, allow_loopback=True)
        for url in (
            "http://hooks.example.com/x",
            "http://93.184.216.34/x",
            "http://10.0.0.7/x",
            "http://localhost.example.com/x",
        ):
            with self.subTest(url=url):
                with self.assertRaises(egress.DestinationError):
                    egress.validate_url(url, allow_loopback=True)
        with self.assertRaises(egress.DestinationError):
            egress.validate_url("http://localhost/x")
        egress.validate_url("https://hooks.example.com/x", allow_loopback=True)

    async def test_loopback_name_must_resolve_to_loopback(self) -> None:
        loopback = ipaddress.ip_address("127.0.0.1")
        for answers in ((PUBLIC,), (loopback, PUBLIC)):
            with self.subTest(answers=answers):
                spy = SpyNetwork()
                async with egress.Egress(
                    resolver=static(*answers), network=spy, allow_loopback=True
                ) as client:
                    with self.assertRaises(egress.DestinationError):
                        await client.post("http://localhost:9/x", b"{}", dict)
                self.assertEqual(spy.targets, [])
        spy = SpyNetwork()
        async with egress.Egress(
            resolver=static(loopback), network=spy, allow_loopback=True
        ) as client:
            with self.assertRaises(httpx.ConnectError):
                await client.post("http://localhost:9/x", b"{}", dict)
        self.assertEqual(spy.targets, [("127.0.0.1", 9)])


class SlowTransport(httpx.AsyncBaseTransport):
    """Answers every request after ``delay`` seconds, recording arrivals."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.arrivals: list[float] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.arrivals.append(time.monotonic())
        await anyio.sleep(self.delay)
        return httpx.Response(200, content=b"{}")


class DeadlineTest(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_for_a_slot_counts_against_the_deadline(self) -> None:
        # Four 80 ms requests to one host, one slot, a 100 ms timeout: only
        # the first fits. With the deadline started after the slot, all four
        # succeeded after ~330 ms.
        transport = SlowTransport(0.08)
        outcomes: list[str] = []

        async def post(client: egress.Egress) -> None:
            try:
                await client.post("https://hooks.example.com/", b"{}", dict)
                outcomes.append("ok")
            except TimeoutError:
                outcomes.append("timeout")

        started = time.monotonic()
        async with egress.Egress(
            concurrency=1, timeout=0.1, transport=transport
        ) as client:
            async with anyio.create_task_group() as group:
                for _ in range(4):
                    group.start_soon(post, client)
        elapsed = time.monotonic() - started
        self.assertEqual(sorted(outcomes), ["ok", "timeout", "timeout", "timeout"])
        self.assertLess(elapsed, 0.2)

    async def test_headers_are_built_once_the_slot_is_held(self) -> None:
        transport = SlowTransport(0.08)
        built: list[float] = []

        def headers() -> dict[str, str]:
            built.append(time.monotonic())
            return {}

        async with egress.Egress(
            concurrency=1, timeout=1.0, transport=transport
        ) as client:
            async with anyio.create_task_group() as group:
                for _ in range(4):
                    group.start_soon(
                        client.post, "https://hooks.example.com/", b"{}", headers
                    )
        self.assertEqual(len(built), 4)
        for stamped, sent in zip(built, transport.arrivals, strict=True):
            self.assertLess(sent - stamped, 0.03)
        # Queued requests were stamped as they went out, not when queued.
        self.assertGreater(built[-1] - built[0], 0.2)


class ConnectTargetTest(unittest.IsolatedAsyncioTestCase):
    async def test_connects_to_the_checked_address(self) -> None:
        spy = SpyNetwork()
        async with egress.Egress(resolver=static(PUBLIC), network=spy) as client:
            with self.assertRaises(httpx.ConnectError):
                await client.post("https://hooks.example.com/x", b"{}", dict)
        self.assertEqual(spy.targets, [(str(PUBLIC), 443)])

    async def test_any_private_answer_rejects_the_host(self) -> None:
        spy = SpyNetwork()
        resolver = static(PUBLIC, PRIVATE)
        async with egress.Egress(resolver=resolver, network=spy) as client:
            with self.assertRaises(egress.DestinationError):
                await client.post("https://hooks.example.com/x", b"{}", dict)
        self.assertEqual(spy.targets, [])

    async def test_dns_rebinding_is_rejected_at_connect(self) -> None:
        answers = [[PUBLIC], [PRIVATE]]

        async def rebinding(host: str, port: int):
            return answers.pop(0)

        spy = SpyNetwork()
        async with egress.Egress(resolver=rebinding, network=spy) as client:
            await client.check("https://rebind.example.com/hook")
            with self.assertRaises(egress.DestinationError):
                await client.post("https://rebind.example.com/hook", b"{}", dict)
        self.assertEqual(spy.targets, [])

    async def test_check_rejects_private_answers(self) -> None:
        async with egress.Egress(resolver=static(PRIVATE)) as client:
            with self.assertRaises(egress.DestinationError):
                await client.check("https://internal.example.com/")

    async def test_unresolvable_host_is_rejected(self) -> None:
        async with egress.Egress(resolver=static(), network=SpyNetwork()) as client:
            with self.assertRaises(egress.DestinationError):
                await client.post("https://nowhere.example.com/", b"{}", dict)


class BodyCapTest(unittest.IsolatedAsyncioTestCase):
    async def test_response_body_is_capped(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * 100_000)
        )
        async with egress.Egress(body_limit=1024, transport=transport) as client:
            response = await client.post("https://hooks.example.com/", b"{}", dict)
        self.assertEqual(len(response.body), 1024)


if __name__ == "__main__":
    unittest.main()
