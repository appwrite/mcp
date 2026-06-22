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

    def test_protected_resource_metadata_shape(self):
        meta = auth.protected_resource_metadata("proj1")
        self.assertEqual(meta["resource"], "https://mcp.appwrite.io/proj1/mcp")
        self.assertEqual(
            meta["authorization_servers"],
            ["https://cloud.appwrite.io/v1/oauth2/proj1"],
        )
        self.assertEqual(meta["bearer_methods_supported"], ["header"])
        self.assertIn("users.read", meta["scopes_supported"])

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
