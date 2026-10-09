"""SSRF rules that need a controlled resolver or address table.

Against a real local HTTPS receiver, the ingress e2e flows (#134) cover: SNI and
``Host`` keep the hostname while the socket dials the checked address,
redirects are not followed, loopback is refused without the test-only flag, an
untrusted certificate is a ``tls_error``, the total timeout, and the
per-host concurrency limit. What stays here cannot be produced end to end:

* The blocklist across IPv4, IPv6, IPv4-mapped, NAT64, 6to4 and Teredo forms:
  a test machine cannot route to most of these addresses.
* Mixed public and private DNS answers and DNS rebinding: they need a resolver
  that answers differently between check and connect.
* URL shapes the subscribe path (PR 5) will reject before any delivery.
* The response body cap: delivery discards response bodies, so it is
  invisible on the wire.
"""

import ipaddress
import unittest

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


class ValidateUrlTest(unittest.TestCase):
    def test_accepts_https(self) -> None:
        url = egress.validate_url("https://hooks.example.com/a?b=c")
        self.assertEqual(url.host, "hooks.example.com")

    def test_rejects_other_schemes_and_shapes(self) -> None:
        for url in [
            "http://hooks.example.com/",
            "ftp://hooks.example.com/",
            "file:///etc/passwd",
            "https:///path",
            "https://user:pass@hooks.example.com/",
            "not a url",
        ]:
            with self.subTest(url=url):
                with self.assertRaises(egress.DestinationError):
                    egress.validate_url(url)

    def test_http_only_with_explicit_loopback_flag(self) -> None:
        url = egress.validate_url("http://127.0.0.1:8080/", allow_loopback=True)
        self.assertEqual(url.scheme, "http")


class ConnectTargetTest(unittest.IsolatedAsyncioTestCase):
    async def test_connects_to_the_checked_address(self) -> None:
        spy = SpyNetwork()
        async with egress.Egress(resolver=static(PUBLIC), network=spy) as client:
            with self.assertRaises(httpx.ConnectError):
                await client.post("https://hooks.example.com/x", b"{}", {})
        self.assertEqual(spy.targets, [(str(PUBLIC), 443)])

    async def test_any_private_answer_rejects_the_host(self) -> None:
        spy = SpyNetwork()
        resolver = static(PUBLIC, PRIVATE)
        async with egress.Egress(resolver=resolver, network=spy) as client:
            with self.assertRaises(egress.DestinationError):
                await client.post("https://hooks.example.com/x", b"{}", {})
        self.assertEqual(spy.targets, [])

    async def test_dns_rebinding_is_rejected_at_connect(self) -> None:
        answers = [[PUBLIC], [PRIVATE]]

        async def rebinding(host: str, port: int):
            return answers.pop(0)

        spy = SpyNetwork()
        async with egress.Egress(resolver=rebinding, network=spy) as client:
            await client.check("https://rebind.example.com/hook")
            with self.assertRaises(egress.DestinationError):
                await client.post("https://rebind.example.com/hook", b"{}", {})
        self.assertEqual(spy.targets, [])

    async def test_check_rejects_private_answers(self) -> None:
        async with egress.Egress(resolver=static(PRIVATE)) as client:
            with self.assertRaises(egress.DestinationError):
                await client.check("https://internal.example.com/")

    async def test_unresolvable_host_is_rejected(self) -> None:
        async with egress.Egress(resolver=static(), network=SpyNetwork()) as client:
            with self.assertRaises(egress.DestinationError):
                await client.post("https://nowhere.example.com/", b"{}", {})


class BodyCapTest(unittest.IsolatedAsyncioTestCase):
    async def test_response_body_is_capped(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * 100_000)
        )
        async with egress.Egress(body_limit=1024, transport=transport) as client:
            response = await client.post("https://hooks.example.com/", b"{}", {})
        self.assertEqual(len(response.body), 1024)


if __name__ == "__main__":
    unittest.main()
