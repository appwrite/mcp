import asyncio
import logging
import os
import unittest
from unittest import mock

from mcp_server_appwrite import auth
from mcp_server_appwrite.http_app import HealthzAccessLogFilter, _send_401


class HealthzAccessLogFilterTests(unittest.TestCase):
    def setUp(self):
        self.filter = HealthzAccessLogFilter()

    def _record(self, args):
        return logging.LogRecord(
            name="uvicorn.access",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg='%s - "%s %s HTTP/%s" %d',
            args=args,
            exc_info=None,
        )

    def test_filters_healthz_access_logs(self):
        record = self._record(("127.0.0.1:12345", "GET", "/healthz", "1.1", 200))

        self.assertFalse(self.filter.filter(record))

    def test_filters_healthz_access_logs_with_query_string(self):
        record = self._record(
            ("127.0.0.1:12345", "GET", "/healthz?ready=1", "1.1", 200)
        )

        self.assertFalse(self.filter.filter(record))

    def test_keeps_non_healthz_access_logs(self):
        record = self._record(("127.0.0.1:12345", "GET", "/mcp", "1.1", 401))

        self.assertTrue(self.filter.filter(record))


class Send401Tests(unittest.TestCase):
    ENV = {
        "APPWRITE_ENDPOINT": "https://cloud.appwrite.io/v1",
        "MCP_PUBLIC_URL": "https://mcp.appwrite.io",
        "APPWRITE_PROJECT_ID": "console",
    }

    def setUp(self):
        patcher = mock.patch.dict(os.environ, self.ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _challenge(self) -> str:
        messages = []

        async def send(message):
            messages.append(message)

        asyncio.run(_send_401(send))
        start = messages[0]
        self.assertEqual(start["status"], 401)
        headers = dict(start["headers"])
        return headers[b"www-authenticate"].decode()

    def test_401_includes_scope_hint_from_discovery(self):
        # SEP-835: the challenge's `scope` parameter mirrors the advertised
        # catalog so clients know exactly what to request.
        pid = "console"
        auth._store_discovery(
            pid,
            {
                "issuer": "https://cloud.appwrite.io/v1/oauth2/console",
                "jwks_uri": "https://cloud.appwrite.io/v1/oauth2/console/jwks",
                "scopes_supported": ["openid", "all", "project:users.read"],
            },
        )
        try:
            challenge = self._challenge()
        finally:
            auth._discovery_cache.pop(pid, None)
        self.assertIn('resource_metadata="', challenge)
        self.assertIn('scope="openid all project:users.read"', challenge)

    def test_401_scope_hint_drops_tokens_outside_rfc6749_grammar(self):
        # The hint lands inside a quoted-string header value; a malicious or
        # misconfigured authorization server must not be able to inject headers
        # (CRLF) or break out of the quoted string (double quote, backslash).
        pid = "console"
        auth._store_discovery(
            pid,
            {
                "issuer": "https://cloud.appwrite.io/v1/oauth2/console",
                "jwks_uri": "https://cloud.appwrite.io/v1/oauth2/console/jwks",
                "scopes_supported": [
                    "openid",
                    'evil"',
                    "back\\slash",
                    "crlf\r\nSet-Cookie: pwned=1",
                    "tab\tseparated",
                    "spa ce",
                    "",
                    42,
                    "project:users.read",
                ],
            },
        )
        try:
            challenge = self._challenge()
        finally:
            auth._discovery_cache.pop(pid, None)
        self.assertIn('scope="openid project:users.read"', challenge)
        self.assertNotIn("Set-Cookie", challenge)
        self.assertNotIn("\r", challenge)
        self.assertNotIn("\n", challenge)
        self.assertNotIn("evil", challenge)
        self.assertNotIn("back", challenge)

    def test_401_omits_scope_hint_when_no_valid_scope_tokens_remain(self):
        pid = "console"
        auth._store_discovery(
            pid,
            {
                "issuer": "https://cloud.appwrite.io/v1/oauth2/console",
                "jwks_uri": "https://cloud.appwrite.io/v1/oauth2/console/jwks",
                "scopes_supported": ['bad"scope', "crlf\r\n"],
            },
        )
        try:
            challenge = self._challenge()
        finally:
            auth._discovery_cache.pop(pid, None)
        self.assertIn('resource_metadata="', challenge)
        self.assertNotIn("scope=", challenge)

    def test_401_omits_scope_hint_when_discovery_unavailable(self):
        # An unauthenticated 401 must never fail because discovery is down.
        with mock.patch.dict(
            os.environ,
            {
                "APPWRITE_ENDPOINT": "http://127.0.0.1:1/v1",
                "APPWRITE_PROJECT_ID": "unreachableproj",
            },
        ):
            challenge = self._challenge()
        self.assertIn('error="invalid_token"', challenge)
        self.assertIn('resource_metadata="', challenge)
        self.assertNotIn("scope=", challenge)


if __name__ == "__main__":
    unittest.main()
