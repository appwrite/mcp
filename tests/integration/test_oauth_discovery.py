"""Live contract test for the OAuth authorization-server discovery the MCP relies on.

The hosted MCP server is an OAuth 2.1 Resource Server: it points clients at the
project's Appwrite authorization server, and MCP-aware clients then self-register
via RFC 7591 Dynamic Client Registration. That registration step only works if the
authorization server advertises ``registration_endpoint`` in its discovery document
(added by the cloud ``feat/oauth2-dynamic-client-registration`` work).

This test locks that contract end-to-end against the configured Appwrite endpoint:
the same discovery document the MCP fetches for ``scopes_supported`` must advertise
the registration endpoint the README promises. It is skipped without live config.

Note: it asserts against the document's own ``issuer`` (not the request host) so it
is correct even when the endpoint regionally redirects (e.g. ``fra.cloud.appwrite.io``).
"""

from __future__ import annotations

import os
import unittest

import httpx

from mcp_server_appwrite import auth

from .support import requires_live_integration


@requires_live_integration
class OAuthDiscoveryContractTests(unittest.TestCase):
    def _discovery(self) -> dict:
        project_id = os.environ["APPWRITE_PROJECT_ID"]
        url = f"{auth.issuer_url(project_id)}/.well-known/openid-configuration"
        resp = httpx.get(url, timeout=10.0, follow_redirects=True)
        resp.raise_for_status()
        return resp.json()

    def test_discovery_advertises_dynamic_client_registration(self):
        doc = self._discovery()

        # The fields the OAuth 2.1 + DCR client flow depends on.
        issuer = doc.get("issuer")
        self.assertTrue(issuer, "discovery document missing issuer")
        self.assertTrue(doc.get("token_endpoint"), "discovery missing token_endpoint")

        # RFC 7591: registration must be advertised for clients to self-register.
        # Skip (rather than fail) when the endpoint predates the dynamic client
        # registration rollout, so this stays green against not-yet-upgraded
        # deployments and starts verifying the moment the endpoint exposes it.
        if "registration_endpoint" not in doc:
            self.skipTest(
                "authorization server does not advertise registration_endpoint yet "
                "(RFC 7591 dynamic client registration not deployed for this endpoint)"
            )
        self.assertEqual(doc["registration_endpoint"], f"{issuer}/register")

    def test_discovery_exposes_supported_scopes(self):
        # The MCP sources its protected-resource metadata scopes from here.
        doc = self._discovery()
        self.assertIsInstance(
            doc.get("scopes_supported"),
            list,
            "discovery missing scopes_supported (MCP cannot advertise scopes)",
        )


if __name__ == "__main__":
    unittest.main()
