"""Appwrite Zero: a temporary Appwrite project for an agent with no account.

The stdio server boots in Zero mode when neither ``APPWRITE_PROJECT_ID`` nor
``APPWRITE_API_KEY`` is set. Instead of failing at startup it exposes
``appwrite_create_zero_project``, which asks the Appwrite console API for a
fresh project (``POST /zero``, no authentication) and points the server's shared
SDK client at the returned endpoint, project and API key. Every hidden tool
re-binds that same client on each call (see ``server.execute_registered_tool``),
so the whole catalog works against the new project for the rest of the session.

The API key is valid until ``expiresAt``. The project is kept only if the human
opens ``claimUrl`` and signs in before ``claimBy``; otherwise it is deleted, so
the agent must hand that link to the user.

The project lands in whichever region serves ``POST /zero``; the default
endpoint is geo-routed. An optional ``region`` sends the request to that
region's host instead (``https://<region>.cloud.appwrite.io/v1``), so no API
change is needed to pick one.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import mcp.types as types
from appwrite_console.client import Client
from appwrite_console.enums.region import Region

from .constants import (
    CALL_TOOL_NAME,
    CONTEXT_TOOL_NAME,
    DEFAULT_ENDPOINT,
    DEFAULT_PROJECT_ID,
    SEARCH_TOOL_NAME,
    SERVER_VERSION,
    WRITE_TOOL_NAME,
    ZERO_CREATE_TOOL_NAME,
    ZERO_GET_TOOL_NAME,
    ZERO_SOURCE,
    ZERO_TIMEOUT_SECONDS,
)

ToolContent = types.TextContent | types.ImageContent | types.EmbeddedResource

_CREATE_TITLE = "Create Appwrite Zero project"
_GET_TITLE = "Get Appwrite Zero project"
_REGIONS = [region.value for region in Region]

_ERRORS = {
    "zero_unavailable": (
        "Appwrite Zero has no capacity right now. Try again in a few minutes, or "
        "ask the user to sign up for Appwrite and configure APPWRITE_PROJECT_ID "
        "and APPWRITE_API_KEY."
    ),
    "zero_disabled": "Appwrite Zero is not enabled on this Appwrite endpoint.",
}


class _NotFound(Exception):
    """``zero_not_found``: the project is no longer an unclaimed Zero project."""


class Zero:
    """Creates Zero projects and re-points ``client`` at the latest one."""

    def __init__(self, client: Client, endpoint: str):
        self._client = client
        # The configured endpoint; a regional create leaves it untouched.
        self._configured = endpoint
        # Where /zero is called: the regional host once a project was created there.
        self._endpoint = endpoint
        self._manifest: dict[str, Any] | None = None

    @property
    def ready(self) -> bool:
        return self._manifest is not None

    def get_tools(self) -> list[types.Tool]:
        return [
            types.Tool(
                name=ZERO_CREATE_TOOL_NAME,
                title=_CREATE_TITLE,
                annotations=types.ToolAnnotations(
                    title=_CREATE_TITLE,
                    read_only_hint=False,
                    open_world_hint=True,
                    destructive_hint=False,
                    idempotent_hint=False,
                ),
                description=(
                    "Create a free, temporary Appwrite project. No Appwrite account "
                    "or credentials are needed. Returns the project's endpoint, "
                    "projectId and an apiKey with every project scope, valid for 60 "
                    "minutes (until expiresAt). After this call "
                    f"{CONTEXT_TOOL_NAME}, {SEARCH_TOOL_NAME}, {CALL_TOOL_NAME} and "
                    f"{WRITE_TOOL_NAME} operate on the new project; the same values also work with any Appwrite SDK or the "
                    "REST API. You MUST give the user the claimUrl: opening it and "
                    "signing in keeps the project and its API key. An unclaimed "
                    "project stops working at expiresAt and is deleted after claimBy. "
                    "Until it is claimed, its Sites and function domains open only with "
                    "the accessToken: send it in the X-Appwrite-Zero-Token header when "
                    "you fetch a domain, and give the user "
                    "https://<domain>/__appwrite/zero?token=<accessToken> to open one "
                    "in a browser. "
                    "Use this only when the user has no Appwrite project of their own. "
                    "Pass region to choose where the project lives; by default it "
                    "is the region nearest to this machine."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "region": {
                            "type": "string",
                            "enum": _REGIONS,
                            "description": "Appwrite Cloud region for the project. Defaults to the nearest one.",
                        },
                    },
                    "additionalProperties": False,
                },
            ),
            types.Tool(
                name=ZERO_GET_TOOL_NAME,
                title=_GET_TITLE,
                annotations=types.ToolAnnotations(
                    title=_GET_TITLE,
                    read_only_hint=True,
                    open_world_hint=True,
                    destructive_hint=False,
                    idempotent_hint=True,
                ),
                description=(
                    "Get the status (active or expired), expiresAt and claimBy of "
                    "an unclaimed Appwrite Zero project. Defaults to the project "
                    f"{ZERO_CREATE_TOOL_NAME} created in this session. A project "
                    "that was claimed or deleted is reported as no longer an "
                    "unclaimed Zero project."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "zero_id": {
                            "type": "string",
                            "description": "Zero project ID ($id, the same as projectId). Defaults to this session's.",
                        },
                        "secret": {
                            "type": "string",
                            "description": "Secret returned with the Zero project. Defaults to this session's.",
                        },
                    },
                    "additionalProperties": False,
                },
            ),
        ]

    def has_tool(self, name: str) -> bool:
        return name in {ZERO_CREATE_TOOL_NAME, ZERO_GET_TOOL_NAME}

    def execute(self, name: str, arguments: dict[str, Any]) -> list[ToolContent]:
        if name == ZERO_CREATE_TOOL_NAME:
            return self._create(arguments.get("region"))
        if name == ZERO_GET_TOOL_NAME:
            return self._get(arguments)
        raise ValueError(f"Unknown Appwrite Zero tool {name}")

    def _create(self, region: Any) -> list[ToolContent]:
        base = self._configured
        if region is not None:
            if region not in _REGIONS:
                raise ValueError(
                    f"Unknown region {region!r}. Use one of: {', '.join(_REGIONS)}."
                )
            if self._configured != DEFAULT_ENDPOINT:
                raise ValueError(
                    f"region works only with the Appwrite Cloud endpoint "
                    f"{DEFAULT_ENDPOINT}; APPWRITE_ENDPOINT is {self._configured}. "
                    "Unset it or omit region."
                )
            base = DEFAULT_ENDPOINT.replace("://", f"://{region}.", 1)
        manifest = self._request(
            "post", "/zero", endpoint=base, json={"source": ZERO_SOURCE}
        )
        self._endpoint = base
        endpoint = str(manifest.get("endpoint") or base).rstrip("/")
        self._client.set_endpoint(endpoint)
        self._client.set_project(manifest["projectId"])
        self._client.set_key(manifest["apiKey"])
        self._manifest = manifest
        return [
            types.TextContent(
                type="text",
                text=(
                    "Created a temporary Appwrite project. The other Appwrite tools "
                    "now operate on it. Give the user this link to keep the project: "
                    f"{manifest.get('claimUrl')}\n{json.dumps(manifest, indent=2)}"
                ),
            )
        ]

    def _get(self, arguments: dict[str, Any]) -> list[ToolContent]:
        manifest = self._manifest or {}
        zero_id = arguments.get("zero_id", arguments.get("zeroId")) or manifest.get(
            "$id"
        )
        secret = arguments.get("secret") or manifest.get("secret")
        if not zero_id or not secret:
            raise ValueError(
                f"zero_id and secret are required until {ZERO_CREATE_TOOL_NAME} "
                "has created a project in this session."
            )
        try:
            status = self._request("get", f"/zero/{zero_id}", params={"secret": secret})
        except _NotFound:
            # A uniform 404 covers claimed, deleted, unknown ID and wrong secret,
            # so the endpoint cannot say which.
            return [
                types.TextContent(
                    type="text",
                    text=(
                        f"{zero_id} is no longer an unclaimed Zero project: it was "
                        "claimed or deleted (or the ID or secret is wrong). If the "
                        "user claimed it, it lives on in their Appwrite account."
                    ),
                )
            ]
        return [types.TextContent(type="text", text=json.dumps(status, indent=2))]

    def _request(
        self, method: str, path: str, endpoint: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        # A plain HTTP client rather than the SDK one: the SDK client carries the
        # current Zero project's API key, and /zero must be called without auth.
        with httpx.Client(timeout=ZERO_TIMEOUT_SECONDS) as http:
            response = http.request(
                method,
                f"{endpoint or self._endpoint}{path}",
                headers={
                    "x-appwrite-project": DEFAULT_PROJECT_ID,
                    "x-sdk-name": "mcp",
                    "user-agent": f"AppwriteMCP/{SERVER_VERSION}",
                },
                **kwargs,
            )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code < 400 and isinstance(body, dict):
            return body

        error_type = body.get("type", "") if isinstance(body, dict) else ""
        if error_type == "zero_not_found":
            raise _NotFound()
        message = body.get("message", "") if isinstance(body, dict) else ""
        if response.status_code == 429:
            guidance = (
                "Too many Appwrite Zero projects were created from this network. "
                "Ask the user to sign up for Appwrite and configure "
                "APPWRITE_PROJECT_ID and APPWRITE_API_KEY, or try again later."
            )
        else:
            guidance = _ERRORS.get(error_type, "Appwrite Zero request failed.")
        details = [f"code={response.status_code}"]
        if error_type:
            details.append(f"type={error_type}")
        suffix = f": {message}" if message else ""
        raise RuntimeError(f"{guidance} ({', '.join(details)}){suffix}")
