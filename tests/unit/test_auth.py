import asyncio
import os
import unittest
from unittest import mock

from mcp_server_appwrite import auth

ENV = {
    "APPWRITE_ENDPOINT": "https://cloud.appwrite.io/v1",
    "MCP_PUBLIC_URL": "https://mcp.appwrite.io",
}


class AuthHelperTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_issuer_and_resource_urls(self):
        self.assertEqual(
            auth.issuer_url("proj1"), "https://cloud.appwrite.io/v1/oauth2/proj1"
        )
        self.assertEqual(
            auth.canonical_resource("proj1"), "https://mcp.appwrite.io/proj1/mcp"
        )
        self.assertEqual(
            auth.resource_metadata_url("proj1"),
            "https://mcp.appwrite.io/.well-known/oauth-protected-resource/proj1/mcp",
        )

    def test_build_resource_metadata_shape(self):
        meta = auth.build_resource_metadata("proj1", ["users.read", "teams.read"])
        self.assertEqual(meta["resource"], "https://mcp.appwrite.io/proj1/mcp")
        self.assertEqual(
            meta["authorization_servers"],
            ["https://cloud.appwrite.io/v1/oauth2/proj1"],
        )
        self.assertEqual(meta["bearer_methods_supported"], ["header"])
        self.assertEqual(meta["scopes_supported"], ["users.read", "teams.read"])

    def test_supported_scopes_uses_cache_without_network(self):
        auth._scopes_cache["cachedproj"] = ["rows.read", "rows.write"]
        try:
            scopes = asyncio.run(auth.supported_scopes("cachedproj"))
        finally:
            auth._scopes_cache.pop("cachedproj", None)
        self.assertEqual(scopes, ["rows.read", "rows.write"])

    def test_supported_scopes_reads_dcr_enabled_discovery(self):
        # The authorization server's discovery document now also advertises
        # `registration_endpoint` (RFC 7591). Sourcing scopes must keep working
        # against that document — the MCP points clients at this same AS, and
        # the clients self-register there. This guards the discovery contract
        # without a network round-trip.
        discovery = {
            "issuer": "https://cloud.appwrite.io/v1/oauth2/proj1",
            "registration_endpoint": "https://cloud.appwrite.io/v1/oauth2/proj1/register",
            "scopes_supported": ["users.read", "rows.write"],
        }
        seen_urls: list[str] = []

        class _FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return discovery

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url):
                seen_urls.append(url)
                return _FakeResponse()

        with mock.patch.object(auth.httpx, "AsyncClient", _FakeAsyncClient):
            try:
                scopes = asyncio.run(auth.supported_scopes("dcrproj"))
            finally:
                auth._scopes_cache.pop("dcrproj", None)

        self.assertEqual(scopes, ["users.read", "rows.write"])
        # Discovery is read from the OIDC well-known path under the project issuer.
        self.assertEqual(
            seen_urls,
            ["https://cloud.appwrite.io/v1/oauth2/dcrproj/.well-known/openid-configuration"],
        )
        # The registration endpoint the MCP points clients to follows `issuer/register`.
        self.assertEqual(
            discovery["registration_endpoint"],
            f"{discovery['issuer']}/register",
        )

    def test_supported_scopes_raises_when_discovery_unreachable(self):
        # Point discovery at an unroutable address so the fetch fails fast.
        with mock.patch.dict(
            os.environ, {"APPWRITE_ENDPOINT": "http://127.0.0.1:1/v1"}
        ):
            with self.assertRaises(Exception):
                asyncio.run(auth.supported_scopes("unreachableproj"))
        self.assertNotIn("unreachableproj", auth._scopes_cache)

    def test_project_id_from_issuer_accepts_matching_issuer(self):
        self.assertEqual(
            auth.project_id_from_issuer("https://cloud.appwrite.io/v1/oauth2/abc123"),
            "abc123",
        )

    def test_project_id_from_issuer_rejects_foreign_issuer(self):
        self.assertIsNone(
            auth.project_id_from_issuer("https://evil.example.com/v1/oauth2/abc123")
        )
        self.assertIsNone(auth.project_id_from_issuer(None))
        # Extra path segments are not a valid single project id.
        self.assertIsNone(
            auth.project_id_from_issuer("https://cloud.appwrite.io/v1/oauth2/abc/extra")
        )


class AudienceTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.verifier = auth.AppwriteTokenVerifier()
        self.resource = "https://mcp.appwrite.io/proj1/mcp"

    def test_audience_match_string(self):
        self.assertTrue(self.verifier._audience_ok(self.resource, self.resource))

    def test_audience_match_list(self):
        self.assertTrue(
            self.verifier._audience_ok(["other", self.resource], self.resource)
        )

    def test_audience_mismatch_rejected(self):
        self.assertFalse(self.verifier._audience_ok("nope", self.resource))

    def test_missing_audience_rejected(self):
        self.assertFalse(self.verifier._audience_ok(None, self.resource))


if __name__ == "__main__":
    unittest.main()
