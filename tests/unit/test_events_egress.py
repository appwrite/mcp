import asyncio
import datetime
import ipaddress
import ssl
import tempfile
import unittest
from pathlib import Path

import httpcore
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from mcp_server_appwrite.events import delivery, egress

HOSTNAME = "callback.test"
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


class LimitsTest(unittest.IsolatedAsyncioTestCase):
    async def test_total_timeout(self) -> None:
        async def slow(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(5)
            return httpx.Response(200)

        transport = httpx.MockTransport(slow)
        async with egress.Egress(timeout=0.05, transport=transport) as client:
            with self.assertRaises(TimeoutError):
                await client.post("https://hooks.example.com/", b"{}", {})

    async def test_response_body_is_capped(self) -> None:
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * 100_000)
        )
        async with egress.Egress(body_limit=1024, transport=transport) as client:
            response = await client.post("https://hooks.example.com/", b"{}", {})
        self.assertEqual(len(response.body), 1024)

    async def test_concurrency_is_limited_per_host(self) -> None:
        active: dict[str, int] = {}
        peak: dict[str, int] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            active[host] = active.get(host, 0) + 1
            peak[host] = max(peak.get(host, 0), active[host])
            await asyncio.sleep(0.01)
            active[host] -= 1
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)
        async with egress.Egress(concurrency=2, transport=transport) as client:
            await asyncio.gather(
                *[client.post("https://a.example.com/", b"", {}) for _ in range(6)],
                *[client.post("https://b.example.com/", b"", {}) for _ in range(6)],
            )
            self.assertEqual(client._limiter._semaphores, {})
        self.assertEqual(peak, {"a.example.com": 2, "b.example.com": 2})


def certificate(directory: Path) -> tuple[Path, Path]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, HOSTNAME)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOSTNAME)]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    certificate_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    certificate_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


class LocalTLSServerTest(unittest.IsolatedAsyncioTestCase):
    """A real TLS server on 127.0.0.1 that only knows itself as callback.test."""

    async def asyncSetUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        certificate_path, key_path = certificate(Path(self.directory.name))
        self.server_names: list[str | None] = []
        self.requests: list[tuple[str, dict[str, str]]] = []

        server_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        server_context.load_cert_chain(certificate_path, key_path)
        server_context.sni_callback = self._record_sni
        self.server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, ssl=server_context
        )
        self.port = self.server.sockets[0].getsockname()[1]

        self.trusting = ssl.create_default_context(cafile=str(certificate_path))
        self.resolver = static(ipaddress.ip_address("127.0.0.1"))

    async def asyncTearDown(self) -> None:
        self.server.close()
        await self.server.wait_closed()
        self.directory.cleanup()

    def _record_sni(self, connection, server_name, context) -> None:
        self.server_names.append(server_name)

    async def _handle(self, reader, writer) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionResetError):
            writer.close()
            return
        lines = head.decode().split("\r\n")
        path = lines[0].split(" ")[1]
        headers = {
            name.strip().lower(): value.strip()
            for name, value in (line.split(":", 1) for line in lines[1:] if line)
        }
        await reader.readexactly(int(headers.get("content-length", "0")))
        self.requests.append((path, headers))
        if path == "/redirect":
            status = "302 Found"
            extra = f"Location: https://{HOSTNAME}:{self.port}/ok\r\n"
            body = b""
        else:
            status, extra, body = "200 OK", "", b'{"ok":true}'
        writer.write(
            f"HTTP/1.1 {status}\r\n{extra}Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n".encode() + body
        )
        await writer.drain()
        writer.close()

    def url(self, path: str) -> str:
        return f"https://{HOSTNAME}:{self.port}{path}"

    async def test_sni_and_host_keep_the_hostname(self) -> None:
        async with egress.Egress(
            resolver=self.resolver, allow_loopback=True, ssl_context=self.trusting
        ) as client:
            response = await client.post(self.url("/ok"), b"{}", {})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b'{"ok":true}')
        self.assertEqual(self.server_names, [HOSTNAME])
        self.assertEqual(self.requests[0][1]["host"], f"{HOSTNAME}:{self.port}")

    async def test_redirects_are_not_followed(self) -> None:
        async with egress.Egress(
            resolver=self.resolver, allow_loopback=True, ssl_context=self.trusting
        ) as client:
            response = await client.post(self.url("/redirect"), b"{}", {})
        self.assertEqual(response.status, 302)
        self.assertEqual([path for path, _ in self.requests], ["/redirect"])

    async def test_loopback_is_blocked_without_the_flag(self) -> None:
        async with egress.Egress(
            resolver=self.resolver, ssl_context=self.trusting
        ) as client:
            with self.assertRaises(egress.DestinationError):
                await client.post(self.url("/ok"), b"{}", {})
        self.assertEqual(self.requests, [])

    async def test_untrusted_certificate_is_a_tls_error(self) -> None:
        # The server side logs the aborted handshake; that noise is expected.
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: None)
        async with egress.Egress(resolver=self.resolver, allow_loopback=True) as client:
            with self.assertRaises(httpx.ConnectError) as raised:
                await client.post(self.url("/ok"), b"{}", {})
        self.assertIs(delivery.classify(raised.exception), delivery.Reason.TLS_ERROR)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
