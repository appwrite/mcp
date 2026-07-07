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

import httpx
import jwt
from anyio import to_thread
from jwt import PyJWKClient
from mcp.server.auth.provider import AccessToken, TokenVerifier

from . import telemetry
from .constants import (
    CACHE_TTL_SECONDS,
    DEFAULT_ENDPOINT,
    DEFAULT_PROJECT_ID,
    PREFERRED_SCOPES,
)


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


def preferred_scopes() -> list[str]:
    override = os.getenv("MCP_OAUTH_SCOPES", "").split()
    return override or list(PREFERRED_SCOPES)


# Discovery cache keyed by served project id: (monotonic fetch time, document).
# Entries are refreshed after a TTL so authorization-server changes (issuer host,
# scope model) propagate without a redeploy; if a refresh fails, the stale copy
# keeps serving so an authorization-server blip doesn't take the MCP down.
_discovery_cache: dict[str, tuple[float, dict]] = {}
_deprecated_scope_cache: dict[str, tuple[float, set[str]]] = {}


def _cached_discovery(project_id: str, *, allow_stale: bool = False) -> dict | None:
    entry = _discovery_cache.get(project_id)
    if entry is None:
        return None
    fetched_at, document = entry
    if allow_stale or time.monotonic() - fetched_at < CACHE_TTL_SECONDS:
        return document
    return None


def _store_discovery(project_id: str, document: dict) -> None:
    _discovery_cache[project_id] = (time.monotonic(), document)


def _scope_catalog_cache_key(kind: str) -> str:
    return f"{appwrite_endpoint()}:{kind}"


def _cached_deprecated_scopes(kind: str) -> set[str] | None:
    entry = _deprecated_scope_cache.get(_scope_catalog_cache_key(kind))
    if entry is None:
        return None
    fetched_at, scopes = entry
    if time.monotonic() - fetched_at < CACHE_TTL_SECONDS:
        return scopes
    return None


def _store_deprecated_scopes(kind: str, scopes: set[str]) -> None:
    _deprecated_scope_cache[_scope_catalog_cache_key(kind)] = (time.monotonic(), scopes)


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
    cached = _cached_discovery(project_id)
    if cached is not None:
        return cached

    url = discovery_url()
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            metadata = _validate_discovery(resp.json(), url)
    except Exception as exc:
        stale = _cached_discovery(project_id, allow_stale=True)
        if stale is not None:
            _log(f"Discovery refresh failed ({exc}); serving stale metadata.")
            return stale
        raise

    _store_discovery(project_id, metadata)
    return metadata


def authorization_server_metadata_sync() -> dict:
    project_id = configured_project_id()
    cached = _cached_discovery(project_id)
    if cached is not None:
        return cached

    url = discovery_url()
    try:
        resp = httpx.get(url, timeout=10.0, follow_redirects=True)
        resp.raise_for_status()
        metadata = _validate_discovery(resp.json(), url)
    except Exception as exc:
        stale = _cached_discovery(project_id, allow_stale=True)
        if stale is not None:
            _log(f"Discovery refresh failed ({exc}); serving stale metadata.")
            return stale
        raise

    _store_discovery(project_id, metadata)
    return metadata


def _advertised_scopes(metadata: dict) -> list[str]:
    """The scope set to advertise. By default (no curated list) this mirrors the
    authorization server's full live ``scopes_supported`` catalog — clients then
    request everything and consent-time narrowing is the control point. When a
    curated list is configured (``MCP_OAUTH_SCOPES``), it is intersected with
    the discovered catalog so a renamed/removed scope is never advertised,
    falling back to the full discovered list when the intersection is empty. A
    later pass removes discovered scopes marked deprecated in Cloud's public
    scope catalog."""
    discovered = metadata.get("scopes_supported")
    if not isinstance(discovered, list):
        raise ValueError(
            f"authorization server discovery missing scopes_supported: {discovery_url()}"
        )
    preferred = preferred_scopes()
    if not preferred:
        return discovered
    scopes = [scope for scope in preferred if scope in discovered]
    if scopes:
        return scopes
    _log(
        "None of the preferred scopes are in the authorization server's "
        "scopes_supported; advertising the full discovered list."
    )
    return discovered


async def _load_deprecated_scopes(client: httpx.AsyncClient, kind: str) -> set[str]:
    cached = _cached_deprecated_scopes(kind)
    if cached is not None:
        return cached

    resp = await client.get(f"{appwrite_endpoint()}/console/scopes/{kind}")
    resp.raise_for_status()
    scopes = resp.json().get("scopes")
    if not isinstance(scopes, list):
        raise ValueError("scope catalog response missing scopes list")

    deprecated = {
        scope["$id"]
        for scope in scopes
        if isinstance(scope, dict)
        and isinstance(scope.get("$id"), str)
        and scope.get("deprecated") is True
    }
    _store_deprecated_scopes(kind, deprecated)
    return deprecated


async def _filter_deprecated_scopes(scopes: list[str]) -> list[str]:
    """Drop deprecated Console scopes while preserving the discovery contract.

    OIDC discovery remains the supported-scope source of truth. The public
    Console scope catalogs only add deprecation metadata, and failures are
    fail-open so a catalog outage does not break OAuth discovery.
    """
    needs_project_catalog = any(
        isinstance(scope, str)
        and scope.startswith("project:")
        and scope != "project:all"
        for scope in scopes
    )
    needs_organization_catalog = any(
        isinstance(scope, str)
        and scope.startswith("organization:")
        and scope != "organization:all"
        for scope in scopes
    )
    if not needs_project_catalog and not needs_organization_catalog:
        return scopes

    project_cached = (
        _cached_deprecated_scopes("project") if needs_project_catalog else set()
    )
    organization_cached = (
        _cached_deprecated_scopes("organization")
        if needs_organization_catalog
        else set()
    )

    try:
        if project_cached is not None and organization_cached is not None:
            deprecated_project_scopes = project_cached
            deprecated_organization_scopes = organization_cached
        else:
            async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
                deprecated_project_scopes = (
                    project_cached
                    if project_cached is not None
                    else await _load_deprecated_scopes(client, "project")
                )
                deprecated_organization_scopes = (
                    organization_cached
                    if organization_cached is not None
                    else await _load_deprecated_scopes(client, "organization")
                )
    except Exception as exc:
        _log(f"Scope catalog refresh failed ({exc}); advertising discovered scopes.")
        return scopes

    filtered = []
    for scope in scopes:
        if not isinstance(scope, str):
            filtered.append(scope)
            continue
        if scope.startswith("project:"):
            if (
                scope != "project:all"
                and scope.removeprefix("project:") in deprecated_project_scopes
            ):
                continue
        elif scope.startswith("organization:"):
            if (
                scope != "organization:all"
                and scope.removeprefix("organization:")
                in deprecated_organization_scopes
            ):
                continue
        filtered.append(scope)
    return filtered


def build_resource_metadata(scopes: list[str], authorization_servers=None) -> dict:
    """RFC 9728 Protected Resource Metadata document."""
    return {
        "resource": canonical_resource(),
        "authorization_servers": authorization_servers or [issuer_url()],
        "scopes_supported": scopes,
        "bearer_methods_supported": ["header"],
    }


async def protected_resource_metadata() -> dict:
    """RFC 9728 Protected Resource Metadata, with scopes validated against AS
    discovery."""
    metadata = await authorization_server_metadata()
    scopes = await _filter_deprecated_scopes(_advertised_scopes(metadata))
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
        access_token = await to_thread.run_sync(self._verify_sync, token)
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
