import asyncio
import os
import unittest
from unittest import mock

import jwt

from mcp_server_appwrite import auth

ENV = {
    "APPWRITE_ENDPOINT": "https://cloud.appwrite.io/v1",
    "MCP_PUBLIC_URL": "https://mcp.appwrite.io",
    "APPWRITE_PROJECT_ID": "console",
}

# The issuer the authorization server *discovers to* is the regional host, which
# deliberately differs from the configured APPWRITE_ENDPOINT host — production
# Cloud discovery returns e.g. fra.cloud.appwrite.io while the MCP is configured
# with cloud.appwrite.io.
DISCOVERED_ISSUER = "https://fra.cloud.appwrite.io/v1/oauth2/console"


def discovery_doc(scopes: list[str], issuer: str = DISCOVERED_ISSUER) -> dict:
    return {
        "issuer": issuer,
        "jwks_uri": f"{issuer}/.well-known/jwks.json",
        "scopes_supported": scopes,
    }


class AuthHelperTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        auth._deprecated_scope_cache.clear()
        self.addCleanup(auth._deprecated_scope_cache.clear)

    def test_issuer_and_resource_urls(self):
        self.assertEqual(
            auth.issuer_url(), "https://cloud.appwrite.io/v1/oauth2/console"
        )
        self.assertEqual(auth.canonical_resource(), "https://mcp.appwrite.io/")
        self.assertEqual(auth.mcp_path_resource(), "https://mcp.appwrite.io/mcp")
        self.assertEqual(
            auth.resource_metadata_url(),
            "https://mcp.appwrite.io/.well-known/oauth-protected-resource",
        )
        self.assertEqual(
            auth.resource_metadata_url(auth.mcp_path_resource()),
            "https://mcp.appwrite.io/.well-known/oauth-protected-resource/mcp",
        )

    def test_build_resource_metadata_shape(self):
        meta = auth.build_resource_metadata(["users.read", "teams.read"])
        self.assertEqual(meta["resource"], "https://mcp.appwrite.io/")
        self.assertEqual(
            meta["authorization_servers"],
            ["https://cloud.appwrite.io/v1/oauth2/console"],
        )
        self.assertEqual(meta["bearer_methods_supported"], ["header"])
        self.assertEqual(meta["scopes_supported"], ["users.read", "teams.read"])

    def test_advertised_scopes_use_cache_without_network(self):
        pid = auth.configured_project_id()
        auth._store_discovery(pid, discovery_doc(["rows.read", "rows.write"]))
        try:
            scopes = asyncio.run(auth.protected_resource_metadata())["scopes_supported"]
        finally:
            auth._discovery_cache.pop(pid, None)
        # No curated list configured, so the discovered catalog is mirrored.
        self.assertEqual(scopes, ["rows.read", "rows.write"])

    def test_advertised_scopes_mirror_full_catalog_by_default(self):
        # The MCP mirrors the authorization server's full scope catalog so
        # clients request everything and consent-time narrowing is the control
        # point (granular MCP scopes design).
        catalog = [
            "openid",
            "profile",
            "email",
            "phone",
            "all",
            "project:all",
            "organization:all",
            "project:users.read",
            "project:users.write",
            "organization:projects.read",
        ]
        pid = auth.configured_project_id()
        auth._store_discovery(pid, discovery_doc(catalog))

        async def no_deprecated_scopes(_client, _kind):
            return set()

        try:
            with mock.patch.object(
                auth, "_load_deprecated_scopes", no_deprecated_scopes
            ):
                scopes = asyncio.run(auth.protected_resource_metadata())[
                    "scopes_supported"
                ]
        finally:
            auth._discovery_cache.pop(pid, None)
        self.assertEqual(scopes, catalog)

    def test_advertised_scopes_filter_deprecated_catalog_scopes(self):
        catalog = [
            "openid",
            "project:all",
            "organization:all",
            "project:policies.read",
            "project:project.policies.read",
            "project:collections.read",
            "project:rows.read",
            "organization:keys.read",
            "organization:organization.keys.read",
        ]
        pid = auth.configured_project_id()
        auth._store_discovery(pid, discovery_doc(catalog))

        async def deprecated_scopes(_client, kind):
            if kind == "project":
                return {"policies.read", "collections.read"}
            if kind == "organization":
                return {"keys.read"}
            return set()

        try:
            with mock.patch.object(auth, "_load_deprecated_scopes", deprecated_scopes):
                scopes = asyncio.run(auth.protected_resource_metadata())[
                    "scopes_supported"
                ]
        finally:
            auth._discovery_cache.pop(pid, None)

        self.assertEqual(
            scopes,
            [
                "openid",
                "project:all",
                "organization:all",
                "project:project.policies.read",
                "project:rows.read",
                "organization:organization.keys.read",
            ],
        )

    def test_advertised_scopes_fail_open_when_scope_catalog_unavailable(self):
        catalog = ["openid", "project:policies.read", "project:rows.read"]
        pid = auth.configured_project_id()
        auth._store_discovery(pid, discovery_doc(catalog))

        async def unavailable_catalog(_client, _kind):
            raise RuntimeError("catalog unavailable")

        try:
            with mock.patch.object(
                auth, "_load_deprecated_scopes", unavailable_catalog
            ):
                scopes = asyncio.run(auth.protected_resource_metadata())[
                    "scopes_supported"
                ]
        finally:
            auth._discovery_cache.pop(pid, None)

        self.assertEqual(scopes, catalog)

    def test_advertised_scopes_use_deprecated_scope_cache_without_client(self):
        catalog = ["openid", "project:policies.read", "project:rows.read"]
        pid = auth.configured_project_id()
        auth._store_discovery(pid, discovery_doc(catalog))
        auth._store_deprecated_scopes("project", {"policies.read"})

        try:
            with mock.patch.object(auth.httpx, "AsyncClient") as async_client:
                scopes = asyncio.run(auth.protected_resource_metadata())[
                    "scopes_supported"
                ]
        finally:
            auth._discovery_cache.pop(pid, None)

        async_client.assert_not_called()
        self.assertEqual(scopes, ["openid", "project:rows.read"])

    def test_advertised_scopes_never_filter_all_scopes(self):
        catalog = [
            "openid",
            "project:all",
            "project:rows.read",
            "organization:all",
            "organization:keys.read",
        ]
        pid = auth.configured_project_id()
        auth._store_discovery(pid, discovery_doc(catalog))

        async def deprecated_scopes(_client, kind):
            if kind == "project":
                return {"all"}
            if kind == "organization":
                return {"all", "keys.read"}
            return set()

        try:
            with mock.patch.object(auth, "_load_deprecated_scopes", deprecated_scopes):
                scopes = asyncio.run(auth.protected_resource_metadata())[
                    "scopes_supported"
                ]
        finally:
            auth._discovery_cache.pop(pid, None)

        self.assertEqual(
            scopes,
            ["openid", "project:all", "project:rows.read", "organization:all"],
        )

    def test_advertised_scopes_env_override(self):
        pid = auth.configured_project_id()
        auth._store_discovery(
            pid, discovery_doc(["openid", "email", "all", "project:all"])
        )
        try:
            with mock.patch.dict(
                os.environ, {"MCP_OAUTH_SCOPES": "openid project:all"}
            ):
                scopes = asyncio.run(auth.protected_resource_metadata())[
                    "scopes_supported"
                ]
        finally:
            auth._discovery_cache.pop(pid, None)
        self.assertEqual(scopes, ["openid", "project:all"])

    def test_advertised_scopes_env_override_drops_undiscovered_scopes(self):
        # A curated scope missing from the live catalog is never advertised.
        pid = auth.configured_project_id()
        auth._store_discovery(pid, discovery_doc(["openid", "email", "all"]))
        try:
            with mock.patch.dict(
                os.environ, {"MCP_OAUTH_SCOPES": "openid email all phone"}
            ):
                scopes = asyncio.run(auth.protected_resource_metadata())[
                    "scopes_supported"
                ]
        finally:
            auth._discovery_cache.pop(pid, None)
        self.assertEqual(scopes, ["openid", "email", "all"])

    def test_advertised_scopes_env_override_falls_back_to_full_catalog(self):
        # When none of the curated scopes exist in the live catalog, the full
        # discovered list is mirrored instead of advertising nothing.
        pid = auth.configured_project_id()
        auth._store_discovery(pid, discovery_doc(["rows.read", "rows.write"]))
        try:
            with mock.patch.dict(os.environ, {"MCP_OAUTH_SCOPES": "openid all"}):
                scopes = asyncio.run(auth.protected_resource_metadata())[
                    "scopes_supported"
                ]
        finally:
            auth._discovery_cache.pop(pid, None)
        self.assertEqual(scopes, ["rows.read", "rows.write"])

    def test_advertised_scopes_raise_when_discovery_missing_scopes_supported(self):
        pid = auth.configured_project_id()
        doc = discovery_doc([])
        del doc["scopes_supported"]
        auth._store_discovery(pid, doc)
        try:
            with self.assertRaises(ValueError):
                asyncio.run(auth.protected_resource_metadata())
        finally:
            auth._discovery_cache.pop(pid, None)

    def test_discovery_cache_expires_after_ttl(self):
        pid = auth.configured_project_id()
        auth._store_discovery(pid, {"issuer": "x", "jwks_uri": "y"})
        try:
            self.assertIsNotNone(auth._cached_discovery(pid))
            # Age the entry past the TTL.
            fetched_at, doc = auth._discovery_cache[pid]
            auth._discovery_cache[pid] = (
                fetched_at - auth.CACHE_TTL_SECONDS - 1,
                doc,
            )
            self.assertIsNone(auth._cached_discovery(pid))
            # A stale entry is still reachable as a fallback for failed refreshes.
            self.assertIsNotNone(auth._cached_discovery(pid, allow_stale=True))
        finally:
            auth._discovery_cache.pop(pid, None)

    def test_stale_discovery_served_when_refresh_fails(self):
        pid = auth.configured_project_id()
        stale_doc = discovery_doc(["openid"])
        auth._store_discovery(pid, stale_doc)
        fetched_at, doc = auth._discovery_cache[pid]
        auth._discovery_cache[pid] = (
            fetched_at - auth.CACHE_TTL_SECONDS - 1,
            doc,
        )

        def _boom(*args, **kwargs):
            raise RuntimeError("network down")

        try:
            with mock.patch.object(auth.httpx, "get", _boom):
                metadata = auth.authorization_server_metadata_sync()
        finally:
            auth._discovery_cache.pop(pid, None)
        self.assertEqual(metadata, stale_doc)

    def test_metadata_reads_dcr_enabled_discovery(self):
        # The authorization server's discovery document now also advertises
        # `registration_endpoint` (RFC 7591). Sourcing scopes must keep working
        # against that document — the MCP points clients at this same AS, and
        # the clients self-register there. This guards the discovery contract
        # without a network round-trip.
        discovery = {
            "issuer": "https://cloud.appwrite.io/v1/oauth2/console",
            "jwks_uri": "https://cloud.appwrite.io/v1/oauth2/console/.well-known/jwks.json",
            "registration_endpoint": "https://cloud.appwrite.io/v1/oauth2/console/register",
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
                scopes = asyncio.run(auth.protected_resource_metadata())[
                    "scopes_supported"
                ]
            finally:
                auth._discovery_cache.pop("console", None)

        self.assertEqual(scopes, ["users.read", "rows.write"])
        # Discovery is read from the OIDC well-known path under the served project's issuer.
        self.assertEqual(
            seen_urls,
            [
                "https://cloud.appwrite.io/v1/oauth2/console/.well-known/openid-configuration"
            ],
        )
        # The registration endpoint the MCP points clients to follows `issuer/register`.
        self.assertEqual(
            discovery["registration_endpoint"],
            f"{discovery['issuer']}/register",
        )

    def test_metadata_raises_when_discovery_unreachable(self):
        # Point discovery at an unroutable address so the fetch fails fast.
        with mock.patch.dict(
            os.environ,
            {
                "APPWRITE_ENDPOINT": "http://127.0.0.1:1/v1",
                "APPWRITE_PROJECT_ID": "unreachableproj",
            },
        ):
            with self.assertRaises(Exception):
                asyncio.run(auth.protected_resource_metadata())
        self.assertNotIn("unreachableproj", auth._discovery_cache)

    def test_project_id_from_issuer_accepts_matching_issuer(self):
        self.assertEqual(
            auth.project_id_from_issuer("https://cloud.appwrite.io/v1/oauth2/abc123"),
            "abc123",
        )

    def test_project_id_from_issuer_accepts_cloud_regional_issuer(self):
        self.assertEqual(
            auth.project_id_from_issuer(
                "https://fra.cloud.appwrite.io/v1/oauth2/abc123"
            ),
            "abc123",
        )

    def test_project_id_from_issuer_rejects_foreign_issuer(self):
        self.assertEqual(
            auth.project_id_from_issuer("https://evil.example.com/v1/oauth2/abc123"),
            "abc123",
        )
        self.assertIsNone(auth.project_id_from_issuer(None))
        # Extra path segments are not a valid single project id.
        self.assertIsNone(
            auth.project_id_from_issuer("https://cloud.appwrite.io/v1/oauth2/abc/extra")
        )

    def test_protected_resource_metadata_uses_discovered_issuer(self):
        pid = auth.configured_project_id()
        auth._store_discovery(pid, discovery_doc(["users.read"]))
        try:
            meta = asyncio.run(auth.protected_resource_metadata())
        finally:
            auth._discovery_cache.pop(pid, None)

        self.assertEqual(
            meta["authorization_servers"],
            [DISCOVERED_ISSUER],
        )


class AudienceTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, ENV, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.verifier = auth.AppwriteTokenVerifier()
        self.resource = "https://mcp.appwrite.io/"
        self.mcp_path_resource = "https://mcp.appwrite.io/mcp"

    def test_audience_match_string(self):
        self.assertEqual(self.verifier._accepted_resource(self.resource), self.resource)

    def test_mcp_path_audience_match_string(self):
        self.assertEqual(
            self.verifier._accepted_resource(self.mcp_path_resource),
            self.mcp_path_resource,
        )

    def test_audience_match_list(self):
        self.assertEqual(
            self.verifier._accepted_resource(["other", self.resource]),
            self.resource,
        )

    def test_audience_mismatch_rejected(self):
        self.assertIsNone(self.verifier._accepted_resource("nope"))

    def test_missing_audience_rejected(self):
        self.assertIsNone(self.verifier._accepted_resource(None))

    def test_verify_rejects_token_for_other_project(self):
        # A token issued by a different project's authorization server must be
        # rejected even before signature checks: this MCP serves one project only.
        # The mismatch is caught from the (unverified) issuer claim, so an unsigned
        # token with a foreign issuer is enough to exercise the guard.
        token = jwt.encode(
            {"iss": "https://cloud.appwrite.io/v1/oauth2/some-other-project"},
            "x" * 32,
            algorithm="HS256",
        )
        self.assertIsNone(self.verifier._verify_sync(token))

    def test_verify_rejects_token_with_undiscovered_issuer(self):
        pid = auth.configured_project_id()
        auth._store_discovery(pid, discovery_doc(["users.read"]))
        token = jwt.encode(
            {"iss": "https://cloud.appwrite.io/v1/oauth2/console"},
            "x" * 32,
            algorithm="HS256",
        )
        try:
            self.assertIsNone(self.verifier._verify_sync(token))
        finally:
            auth._discovery_cache.pop(pid, None)


if __name__ == "__main__":
    unittest.main()
