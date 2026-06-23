"""OAuth 2.1 resource-server layer for the hosted Appwrite MCP.

The MCP server acts as an OAuth 2.1 Resource Server (per the MCP authorization
spec): it validates the bearer access token issued by Appwrite Cloud's OAuth
authorization server and then lets the request proceed. The same token is later
forwarded to the Appwrite REST API, which natively accepts it.

This deployment is single-tenant: it serves one Appwrite project — the Cloud
console project by default, overridable via ``APPWRITE_PROJECT_ID`` — so the MCP
endpoint is simply ``/mcp`` with no project in the path. Tokens must be issued by
that project's authorization server (``<endpoint>/oauth2/<project_id>``); a token
whose issuer names any other project is rejected.
"""

from __future__ import annotations

import os
import sys
import time
from urllib.parse import urlsplit, urlunsplit

import anyio
import httpx
import jwt
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

DEFAULT_ENDPOINT = "https://cloud.appwrite.io/v1"
DEFAULT_PROJECT_ID = "console"


def _log(message: str) -> None:
    print(f"[appwrite-mcp][auth] {message}", file=sys.stderr, flush=True)


def appwrite_endpoint() -> str:
    return os.getenv("APPWRITE_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")


def configured_project_id() -> str:
    """The single Appwrite project this MCP serves. Defaults to the Cloud console
    project; override with ``APPWRITE_PROJECT_ID`` for other deployments/tests."""
    return os.getenv("APPWRITE_PROJECT_ID", DEFAULT_PROJECT_ID)


def public_base_url() -> str:
    """External base URL of this MCP server, used to build canonical resource URIs.
    Falls back to APPWRITE-independent ``MCP_PUBLIC_URL``."""
    base = os.getenv("MCP_PUBLIC_URL", "http://localhost:8000")
    return base.rstrip("/")


def issuer_url() -> str:
    """The Appwrite OAuth authorization server (issuer) for the served project."""
    return f"{appwrite_endpoint()}/oauth2/{configured_project_id()}"


def canonical_resource() -> str:
    """RFC 8707 canonical resource URI for this MCP server."""
    return f"{public_base_url()}/mcp"


def resource_metadata_url() -> str:
    """RFC 9728 protected-resource metadata URL (well-known path + resource path)."""
    parts = urlsplit(public_base_url())
    path = "/.well-known/oauth-protected-resource/mcp"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


# Cache of scopes_supported, keyed by served project id (process lifetime; the
# project OAuth config is effectively static). Failed lookups raise and are not
# cached, so they retry.
_scopes_cache: dict[str, list[str]] = {}


async def supported_scopes() -> list[str]:
    """Scopes advertised in the protected-resource metadata, sourced live from the
    served project's authorization-server discovery (`scopes_supported`). This is
    exactly the set the project's OAuth server will grant, so it never drifts from
    the tool surface. Raises if discovery is unreachable or malformed (the
    authorization server is the same Appwrite deployment this MCP depends on)."""
    project_id = configured_project_id()
    cached = _scopes_cache.get(project_id)
    if cached is not None:
        return cached

    url = f"{issuer_url()}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        scopes = resp.json().get("scopes_supported")
    if not isinstance(scopes, list):
        raise ValueError(
            f"authorization server discovery missing scopes_supported: {url}"
        )

    _scopes_cache[project_id] = scopes
    return scopes


def build_resource_metadata(scopes: list[str]) -> dict:
    """RFC 9728 Protected Resource Metadata document."""
    return {
        "resource": canonical_resource(),
        "authorization_servers": [issuer_url()],
        "scopes_supported": scopes,
        "bearer_methods_supported": ["header"],
    }


async def protected_resource_metadata() -> dict:
    """RFC 9728 Protected Resource Metadata, with scopes sourced from AS discovery."""
    return build_resource_metadata(await supported_scopes())


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
    """Validates RS256 access tokens against the served project's JWKS.

    Revocation/rotation is not checked here: the Appwrite REST API re-validates the
    token (signature + rotation + expiry + identity) on every downstream call, so a
    lightweight verification at the MCP gate is sufficient and avoids an introspection
    round-trip. Short-lived public-client tokens keep the exposure window small.
    """

    def __init__(self) -> None:
        # One JWKS client per project, cached for the process lifetime. In practice
        # only the served project's client is ever created.
        self._jwks_clients: dict[str, PyJWKClient] = {}

    def _jwks_client(self, project_id: str) -> PyJWKClient:
        client = self._jwks_clients.get(project_id)
        if client is None:
            jwks_uri = f"{appwrite_endpoint()}/oauth2/{project_id}/.well-known/jwks.json"
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
        if project_id != configured_project_id():
            _log(
                f"Rejecting token: issuer project {project_id!r} is not the served "
                f"project {configured_project_id()!r}."
            )
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

        expected_resource = canonical_resource()
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
