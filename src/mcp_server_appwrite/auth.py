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

from . import telemetry

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
_discovery_cache: dict[str, dict] = {}


def discovery_url() -> str:
    return f"{issuer_url()}/.well-known/openid-configuration"


def _validate_discovery(doc: dict, url: str) -> dict:
    issuer = doc.get("issuer")
    jwks_uri = doc.get("jwks_uri")
    if not isinstance(issuer, str) or not issuer:
        raise ValueError(f"authorization server discovery missing issuer: {url}")
    if not isinstance(jwks_uri, str) or not jwks_uri:
        raise ValueError(f"authorization server discovery missing jwks_uri: {url}")
    return doc


async def authorization_server_metadata() -> dict:
    project_id = configured_project_id()
    cached = _discovery_cache.get(project_id)
    if cached is not None:
        return cached

    url = discovery_url()
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        metadata = _validate_discovery(resp.json(), url)

    _discovery_cache[project_id] = metadata
    return metadata


def authorization_server_metadata_sync() -> dict:
    project_id = configured_project_id()
    cached = _discovery_cache.get(project_id)
    if cached is not None:
        return cached

    url = discovery_url()
    resp = httpx.get(url, timeout=10.0, follow_redirects=True)
    resp.raise_for_status()
    metadata = _validate_discovery(resp.json(), url)
    _discovery_cache[project_id] = metadata
    return metadata


async def supported_scopes() -> list[str]:
    """Scopes advertised in the protected-resource metadata, sourced live from the
    served project's authorization-server discovery (`scopes_supported`). This is
    exactly the set the project's OAuth server will grant, so it never drifts from
    the tool surface. Raises if discovery is unreachable or malformed (the
    authorization server is the same Appwrite deployment this MCP depends on)."""
    metadata = await authorization_server_metadata()
    scopes = metadata.get("scopes_supported")
    if not isinstance(scopes, list):
        raise ValueError(
            f"authorization server discovery missing scopes_supported: {discovery_url()}"
        )
    return scopes


def build_resource_metadata(scopes: list[str], authorization_servers=None) -> dict:
    """RFC 9728 Protected Resource Metadata document."""
    return {
        "resource": canonical_resource(),
        "authorization_servers": authorization_servers or [issuer_url()],
        "scopes_supported": scopes,
        "bearer_methods_supported": ["header"],
    }


async def protected_resource_metadata() -> dict:
    """RFC 9728 Protected Resource Metadata, with scopes sourced from AS discovery."""
    metadata = await authorization_server_metadata()
    scopes = metadata.get("scopes_supported")
    if not isinstance(scopes, list):
        raise ValueError(
            f"authorization server discovery missing scopes_supported: {discovery_url()}"
        )
    return build_resource_metadata(scopes, [metadata["issuer"]])


def project_id_from_issuer(iss: str | None) -> str | None:
    """Extract the project ID from an Appwrite OAuth issuer."""
    if not isinstance(iss, str) or not iss:
        return None
    issuer = urlsplit(iss)
    endpoint = urlsplit(appwrite_endpoint())
    if issuer.scheme != endpoint.scheme:
        return None
    endpoint_path = endpoint.path.rstrip("/")
    prefix = f"{endpoint_path}/oauth2/" if endpoint_path else "/oauth2/"
    issuer_path = issuer.path.rstrip("/")
    if not issuer_path.startswith(prefix):
        return None
    project_id = issuer_path[len(prefix) :].strip("/")
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

    def _jwks_client(self, issuer: str, jwks_uri: str) -> PyJWKClient:
        client = self._jwks_clients.get(issuer)
        if client is None:
            client = PyJWKClient(jwks_uri)
            self._jwks_clients[issuer] = client
        return client

    def _verify_sync(self, token: str) -> AccessToken | None:
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError:
            telemetry.record_auth(outcome="rejected", reason="malformed")
            return None

        issuer = unverified.get("iss")
        try:
            metadata = authorization_server_metadata_sync()
        except Exception as exc:
            _log(f"Rejecting token: authorization server discovery failed ({exc}).")
            telemetry.record_auth(outcome="rejected", reason="discovery_failed")
            return None

        expected_issuer = metadata["issuer"]
        if issuer != expected_issuer:
            _log(
                f"Rejecting token: issuer {issuer!r} does not match discovered "
                f"issuer {expected_issuer!r}."
            )
            telemetry.record_auth(outcome="rejected", reason="issuer_mismatch")
            return None

        project_id = project_id_from_issuer(issuer)
        if not project_id:
            _log("Rejecting token: issuer is not an Appwrite OAuth issuer.")
            telemetry.record_auth(outcome="rejected", reason="issuer_mismatch")
            return None
        if project_id != configured_project_id():
            _log(
                f"Rejecting token: issuer project {project_id!r} is not the served "
                f"project {configured_project_id()!r}."
            )
            telemetry.record_auth(outcome="rejected", reason="project_mismatch")
            return None

        try:
            signing_key = self._jwks_client(
                expected_issuer, metadata["jwks_uri"]
            ).get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                options={"verify_aud": False, "require": ["exp"]},
            )
        except jwt.PyJWTError as exc:
            _log(f"Rejecting token: verification failed ({exc}).")
            telemetry.record_auth(outcome="rejected", reason="signature")
            return None

        expected_resource = canonical_resource()
        if not self._audience_ok(claims.get("aud"), expected_resource):
            telemetry.record_auth(outcome="rejected", reason="audience")
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
        start = time.monotonic()
        access_token = await anyio.to_thread.run_sync(self._verify_sync, token)
        duration = time.monotonic() - start
        if access_token is None:
            # The specific rejection reason was already counted in _verify_sync;
            # here we only attach the duration to the rejected outcome.
            telemetry.record_auth(outcome="rejected", duration_s=duration, count=False)
            return None
        if access_token.expires_at and access_token.expires_at < int(time.time()):
            telemetry.record_auth(
                outcome="rejected", reason="expired", duration_s=duration
            )
            return None
        telemetry.record_auth(outcome="success", duration_s=duration)
        return access_token
