"""OAuth 2.1 resource-server layer for the hosted Appwrite MCP.

The MCP server acts as an OAuth 2.1 Resource Server (per the MCP authorization
spec): it validates the bearer access token issued by Appwrite Cloud's per-project
OAuth authorization server and then lets the request proceed. The same token is
later forwarded to the Appwrite REST API, which natively accepts it.

Routing is multi-tenant via the URL path (``/{project_id}/mcp``), so a single
deployment serves every project. Because the JWT ``iss`` claim already encodes the
project (``<endpoint>/oauth2/<project_id>``), the project is derived from the token
itself — no path is needed inside :meth:`verify_token`.
"""

from __future__ import annotations

import os
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import anyio
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

DEFAULT_ENDPOINT = "https://cloud.appwrite.io/v1"

# Full read+write scope set the MCP advertises in its protected-resource metadata.
# The connecting client requests the subset it needs; the cloud consent screen and the
# Appwrite REST API (per-route scope checks) enforce what is actually granted.
SCOPES_SUPPORTED: list[str] = [
    "users.read",
    "users.write",
    "sessions.read",
    "sessions.write",
    "teams.read",
    "teams.write",
    "databases.read",
    "databases.write",
    "tables.read",
    "tables.write",
    "columns.read",
    "columns.write",
    "indexes.read",
    "indexes.write",
    "rows.read",
    "rows.write",
    "buckets.read",
    "buckets.write",
    "files.read",
    "files.write",
    "functions.read",
    "functions.write",
    "executions.read",
    "executions.write",
    "providers.read",
    "providers.write",
    "topics.read",
    "topics.write",
    "subscribers.read",
    "subscribers.write",
    "targets.read",
    "targets.write",
    "messages.read",
    "messages.write",
    "sites.read",
    "sites.write",
    "locale.read",
    "avatars.read",
]


def _log(message: str) -> None:
    print(f"[appwrite-mcp][auth] {message}", file=sys.stderr, flush=True)


def appwrite_endpoint() -> str:
    return os.getenv("APPWRITE_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")


def public_base_url() -> str:
    """External base URL of this MCP server, used to build canonical resource URIs.
    Falls back to APPWRITE-independent ``MCP_PUBLIC_URL``."""
    base = os.getenv("MCP_PUBLIC_URL", "http://localhost:8000")
    return base.rstrip("/")


def issuer_url(project_id: str) -> str:
    """The per-project Appwrite OAuth authorization server (issuer)."""
    return f"{appwrite_endpoint()}/oauth2/{project_id}"


def canonical_resource(project_id: str) -> str:
    """RFC 8707 canonical resource URI for this MCP server / project."""
    return f"{public_base_url()}/{project_id}/mcp"


def resource_metadata_url(project_id: str) -> str:
    """RFC 9728 protected-resource metadata URL (well-known path + resource path)."""
    parts = urlsplit(public_base_url())
    path = f"/.well-known/oauth-protected-resource/{project_id}/mcp"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def protected_resource_metadata(project_id: str) -> dict:
    """RFC 9728 Protected Resource Metadata document for a project."""
    return {
        "resource": canonical_resource(project_id),
        "authorization_servers": [issuer_url(project_id)],
        "scopes_supported": SCOPES_SUPPORTED,
        "bearer_methods_supported": ["header"],
    }


def project_id_from_issuer(iss: str | None) -> str | None:
    """Extract and validate the project ID from a token's ``iss`` claim. Returns
    None unless the issuer matches this server's configured Appwrite OAuth base."""
    if not iss:
        return None
    prefix = f"{appwrite_endpoint()}/oauth2/"
    if not iss.startswith(prefix):
        return None
    project_id = iss[len(prefix) :].strip("/")
    # Project IDs are a single path segment.
    if not project_id or "/" in project_id:
        return None
    return project_id


class AppwriteTokenVerifier(TokenVerifier):
    """Validates RS256 access tokens against the issuing project's JWKS.

    Revocation/rotation is not checked here: the Appwrite REST API re-validates the
    token (signature + rotation + expiry + identity) on every downstream call, so a
    lightweight verification at the MCP gate is sufficient and avoids an introspection
    round-trip. Short-lived public-client tokens keep the exposure window small.
    """

    def __init__(self) -> None:
        # One JWKS client per project, cached for the process lifetime.
        self._jwks_clients: dict[str, PyJWKClient] = {}

    def _jwks_client(self, project_id: str) -> PyJWKClient:
        client = self._jwks_clients.get(project_id)
        if client is None:
            jwks_uri = f"{issuer_url(project_id)}/.well-known/jwks.json"
            client = PyJWKClient(jwks_uri)
            self._jwks_clients[project_id] = client
        return client

    def _verify_sync(self, token: str) -> AccessToken | None:
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            return None

        project_id = project_id_from_issuer(unverified.get("iss"))
        if not project_id:
            _log("Rejecting token: issuer does not match this Appwrite endpoint.")
            return None

        try:
            signing_key = self._jwks_client(project_id).get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                options={"verify_aud": False, "require": ["exp"]},
            )
        except jwt.PyJWTError as exc:
            _log(f"Rejecting token: verification failed ({exc}).")
            return None

        expected_resource = canonical_resource(project_id)
        if not self._audience_ok(claims.get("aud"), expected_resource):
            return None

        scope_claim = claims.get("scope") or claims.get("scp") or ""
        scopes = (
            scope_claim.split() if isinstance(scope_claim, str) else list(scope_claim)
        )

        return AccessToken(
            token=token,
            client_id=str(
                claims.get("client_id") or claims.get("azp") or claims.get("aud") or ""
            ),
            scopes=scopes,
            expires_at=int(claims["exp"]) if "exp" in claims else None,
            resource=expected_resource,
            subject=claims.get("sub"),
            claims={**claims, "project_id": project_id},
        )

    def _audience_ok(self, aud, expected_resource: str) -> bool:
        # Tokens must be audience-bound to this MCP server (RFC 8707). The Appwrite
        # OAuth server always issues Resource Indicators, so a missing or mismatched
        # audience is a hard rejection.
        audiences = (
            [aud] if isinstance(aud, str) else list(aud) if aud is not None else []
        )
        if expected_resource in audiences:
            return True
        _log(
            f"Rejecting token: audience {audiences!r} not bound to {expected_resource!r}."
        )
        return False

    async def verify_token(self, token: str) -> AccessToken | None:
        access_token = await anyio.to_thread.run_sync(self._verify_sync, token)
        if access_token is None:
            return None
        if access_token.expires_at and access_token.expires_at < int(time.time()):
            return None
        return access_token
