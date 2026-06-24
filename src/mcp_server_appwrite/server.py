from __future__ import annotations

import argparse
import asyncio
import base64
import importlib
import inspect
import json
import os
import pkgutil
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

import mcp.server.stdio
import mcp.types as types
from appwrite.client import Client
from appwrite.enums.browser import Browser
from appwrite.exception import AppwriteException
from appwrite.input_file import InputFile
from appwrite.service import Service as _SdkService
from dotenv import find_dotenv, load_dotenv
from mcp.server import NotificationOptions, Server
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.models import InitializationOptions

from .context import _normalize_sample_limit, get_appwrite_context
from .docs_search import DocsSearch
from .operator import Operator
from .service import Service
from .tool_manager import ToolManager

SERVER_VERSION = "0.7.0"

DEFAULT_ENDPOINT = "https://cloud.appwrite.io/v1"
DEFAULT_TRANSPORT = "stdio"
TRANSPORTS = {"stdio", "http"}
VALIDATION_SERVICE_ORDER = (
    "tables_db",
    "users",
    "teams",
    "functions",
    "sites",
    "storage",
    "messaging",
    "locale",
    "avatars",
)

# Service modules in the Appwrite SDK to skip (none by default — every service the
# installed SDK ships is exposed). Add a module name here to hide a service.
EXCLUDED_SERVICES: frozenset[str] = frozenset()


def _discover_service_classes() -> dict[str, type]:
    """Discover every Appwrite SDK service class, keyed by its module name
    (e.g. ``"tables_db" -> TablesDB``). The module name is used as the tool-name
    prefix. The catalog/schema is built once from these classes; at execution time
    the matching class is re-instantiated on a per-request client (see
    ``resolve_client``)."""
    import appwrite.services as services_pkg

    discovered: dict[str, type] = {}
    for module_info in pkgutil.iter_modules(services_pkg.__path__):
        name = module_info.name
        if name in EXCLUDED_SERVICES:
            continue
        module = importlib.import_module(f"appwrite.services.{name}")
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(cls, _SdkService)
                and cls is not _SdkService
                and cls.__module__ == module.__name__
            ):
                discovered[name] = cls
                break
    return discovered


# Maps the MCP service name (tool-name prefix) to its Appwrite SDK service class.
SERVICE_CLASSES: dict[str, type] = _discover_service_classes()


@dataclass(frozen=True)
class AppwriteConfig:
    project_id: str
    api_key: str
    endpoint: str


def _log_startup(message: str) -> None:
    print(f"[appwrite-mcp] {message}", file=sys.stderr, flush=True)


def _transport_arg(value: str) -> str:
    if value not in TRANSPORTS:
        raise argparse.ArgumentTypeError(
            f"invalid choice: {value!r} (choose from 'http', 'stdio')"
        )
    return value


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description="Appwrite MCP Server")
    parser.add_argument(
        "--transport",
        type=_transport_arg,
        default=os.getenv("MCP_TRANSPORT", DEFAULT_TRANSPORT),
        help="MCP transport to serve (default $MCP_TRANSPORT or stdio).",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("HOST", "0.0.0.0"),
        help="Bind host for the HTTP server (default $HOST or 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "8000")),
        help="Bind port for the HTTP server (default $PORT or 8000).",
    )
    args = parser.parse_args(argv)
    try:
        args.transport = _transport_arg(args.transport)
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    return args


def load_environment() -> None:
    cwd_dotenv = Path.cwd() / ".env"
    if cwd_dotenv.exists():
        load_dotenv(dotenv_path=cwd_dotenv)
        return

    discovered_dotenv = find_dotenv(usecwd=True)
    if discovered_dotenv:
        load_dotenv(dotenv_path=discovered_dotenv)


def load_appwrite_config() -> AppwriteConfig:
    load_environment()

    project_id = os.getenv("APPWRITE_PROJECT_ID")
    api_key = os.getenv("APPWRITE_API_KEY")
    endpoint = os.getenv("APPWRITE_ENDPOINT", DEFAULT_ENDPOINT)

    if not project_id or not api_key:
        raise ValueError(
            "APPWRITE_PROJECT_ID and APPWRITE_API_KEY must be set in environment variables"
        )

    return AppwriteConfig(project_id=project_id, api_key=api_key, endpoint=endpoint)


def build_client(config: AppwriteConfig | None = None) -> Client:
    config = config or load_appwrite_config()
    client = Client()
    client.set_endpoint(config.endpoint)
    client.set_project(config.project_id)
    client.set_key(config.api_key)
    client.add_header("x-sdk-name", "mcp")
    return client


def build_introspection_client() -> Client:
    """A credential-less client used only to introspect SDK methods for schema
    generation. It never makes API calls, so no project/key is required."""
    client = Client()
    client.set_endpoint(os.getenv("APPWRITE_ENDPOINT", DEFAULT_ENDPOINT))
    client.add_header("x-sdk-name", "mcp")
    return client


def build_client_for_request(
    project_id: str,
    bearer_token: str,
    endpoint: str | None = None,
    target_project: str | None = None,
    organization_id: str | None = None,
) -> Client:
    """Build a per-request client authenticated with a user's OAuth2 access token.
    The Appwrite REST API accepts the OAuth2 access token directly as a Bearer token
    and resolves the user + granted scopes from it.

    The token authenticates against the Appwrite console project. To act on one of
    the user's own projects, pass ``target_project``: it is sent as the
    ``X-Appwrite-Project`` header so the same console token operates on that
    project's data. ``organization_id`` sets ``X-Appwrite-Organization`` for
    org-scoped console operations (e.g. creating a project).

    Targeting a real project also sends ``X-Appwrite-Mode: admin``. Admin mode is
    what lets a console-issued token be recognized across projects (resolving the
    user as the project owner); without it the token is not a valid identity on
    another project and the request falls back to the guest role. Admin mode is
    only valid against a real project — the API rejects it on the console project —
    so it is not sent for ``organization_id``-only (console) operations."""
    client = Client()
    client.set_endpoint(endpoint or os.getenv("APPWRITE_ENDPOINT", DEFAULT_ENDPOINT))
    client.set_project(target_project or project_id)
    client.add_header("Authorization", f"Bearer {bearer_token}")
    client.add_header("x-sdk-name", "mcp")
    if target_project:
        client.add_header("x-appwrite-project", target_project)
        # Admin mode lets the console-issued token be recognized on another project
        # (as the owner) instead of falling back to guest. It is only valid when
        # targeting a real project — the API rejects admin mode on the console
        # project itself — so it is gated on target_project, not organization_id.
        client.add_header("x-appwrite-mode", "admin")
    if organization_id:
        client.add_header("x-appwrite-organization", organization_id)
    return client


def resolve_client(
    target_project: str | None = None, organization_id: str | None = None
) -> Client:
    """Build the Appwrite client for the current request from its OAuth access
    token. The token is read from the request context populated by the auth
    middleware and carries the project it was issued for (the console). Pass
    ``target_project``/``organization_id`` to scope the call to one of the user's
    own projects/organizations."""
    access_token = get_access_token()
    if access_token is None:
        raise RuntimeError("No authenticated Appwrite access token in request context.")

    claims = access_token.claims or {}
    project_id = claims.get("project_id")
    if not project_id:
        raise RuntimeError("Authenticated token is missing a project identifier.")
    return build_client_for_request(
        project_id,
        access_token.token,
        target_project=target_project,
        organization_id=organization_id,
    )


def register_services(client: Client) -> ToolManager:
    tools_manager = ToolManager()
    for name, service_cls in SERVICE_CLASSES.items():
        tools_manager.register_service(Service(service_cls(client), name))
    return tools_manager


def _validate_service(service: Service) -> None:
    match service.service_name:
        case "tables_db" | "users" | "teams" | "functions" | "sites":
            service.service.list()
        case "storage":
            service.service.list_buckets()
        case "messaging":
            service.service.list_messages()
        case "locale":
            service.service.list_codes()
        case "avatars":
            service.service.get_browser(Browser.GOOGLE_CHROME.value, width=1, height=1)
        case _:
            raise ValueError(
                f"No startup validation probe configured for service '{service.service_name}'"
            )


def validate_services(tools_manager: ToolManager) -> None:
    if not tools_manager.services:
        return

    services_by_name = {
        service.service_name: service for service in tools_manager.services
    }
    service = next(
        (
            services_by_name[service_name]
            for service_name in VALIDATION_SERVICE_ORDER
            if service_name in services_by_name
        ),
        None,
    )
    if service is None:
        return

    _log_startup(f"Validating startup access via {service.service_name}")

    try:
        _validate_service(service)
    except AppwriteException as exc:
        raise RuntimeError(
            "Appwrite startup validation failed during the minimal startup probe. "
            "Check your endpoint, project ID, API key, and required scopes.\n"
            f"- {service.service_name}: {_format_appwrite_error(exc)}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            "Appwrite startup validation failed during the minimal startup probe. "
            "Check your endpoint, project ID, API key, and required scopes.\n"
            f"- {service.service_name}: {exc}"
        ) from exc

    _log_startup(f"Validated startup access via {service.service_name}")


def _unwrap_optional_type(py_type: Any) -> Any:
    origin = get_origin(py_type)
    if origin not in (UnionType, Union):
        return py_type

    args = [arg for arg in get_args(py_type) if arg is not type(None)]
    if len(args) == 1:
        return args[0]
    return py_type


def _coerce_enum(enum_type: type[Enum], value: Any, param_name: str) -> Any:
    if isinstance(value, enum_type):
        return value.value

    try:
        return enum_type(value).value
    except ValueError as exc:
        valid_values = ", ".join(str(member.value) for member in enum_type)
        raise ValueError(
            f"Invalid value for '{param_name}'. Expected one of: {valid_values}"
        ) from exc


def _coerce_input_file(value: Any, param_name: str) -> InputFile:
    if isinstance(value, InputFile):
        return value

    if isinstance(value, str):
        return InputFile.from_path(value)

    if not isinstance(value, Mapping):
        raise ValueError(
            f"Invalid value for '{param_name}'. Provide a file path string or an object with `path` or `filename` and `content`."
        )

    path = value.get("path")
    if path:
        return InputFile.from_path(str(path))

    filename = value.get("filename")
    content = value.get("content")
    if filename and content is not None:
        encoding = str(value.get("encoding", "utf-8")).lower()
        if encoding == "base64":
            try:
                data = base64.b64decode(content)
            except Exception as exc:
                raise ValueError(f"Invalid base64 content for '{param_name}'.") from exc
        elif encoding == "utf-8":
            data = str(content).encode("utf-8")
        else:
            raise ValueError(
                f"Invalid encoding for '{param_name}'. Expected 'utf-8' or 'base64'."
            )

        return InputFile.from_bytes(data, str(filename), value.get("mime_type"))

    raise ValueError(
        f"Invalid value for '{param_name}'. Provide `path`, or both `filename` and `content`."
    )


def _coerce_argument(param_name: str, value: Any, param_type: Any) -> Any:
    if value is None:
        return value

    param_type = _unwrap_optional_type(param_type)
    origin = get_origin(param_type)
    args = get_args(param_type)

    if param_type is InputFile:
        return _coerce_input_file(value, param_name)

    if isinstance(param_type, type) and issubclass(param_type, Enum):
        return _coerce_enum(param_type, value, param_name)

    if origin is list and isinstance(value, list) and args:
        return [_coerce_argument(param_name, item, args[0]) for item in value]

    if origin is dict and isinstance(value, dict) and len(args) >= 2:
        return {
            key: _coerce_argument(param_name, item, args[1])
            for key, item in value.items()
        }

    return value


def _to_snake_case(value: str) -> str:
    normalized = value.lstrip("$")
    normalized = normalized.replace("-", "_")
    normalized = normalized.replace(" ", "_")
    normalized = normalized.replace(".", "_")
    normalized = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", normalized)
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", normalized)
    normalized = re.sub(r"_+", "_", normalized)
    return normalized.lower().strip("_")


def _expected_argument_names(tool_info: dict) -> set[str]:
    parameter_names = set(tool_info.get("parameter_types", {}).keys())
    if parameter_names:
        return parameter_names

    input_schema = (
        tool_info.get("definition").inputSchema if tool_info.get("definition") else None
    )
    properties = (
        input_schema.get("properties", {}) if isinstance(input_schema, dict) else {}
    )
    return set(properties.keys()) if isinstance(properties, dict) else set()


def _normalize_argument_key(
    key: str, expected_names: set[str], normalized_arguments: dict[str, Any]
) -> str:
    if key in expected_names:
        return key

    candidate_key = _to_snake_case(key)
    if candidate_key in expected_names:
        return candidate_key

    if candidate_key == "id":
        id_candidates = [
            name
            for name in expected_names
            if name.endswith("_id") and name not in normalized_arguments
        ]
        if len(id_candidates) == 1:
            return id_candidates[0]

    return key


def _normalize_argument_keys(
    tool_info: dict, arguments: dict[str, Any]
) -> dict[str, Any]:
    expected_names = _expected_argument_names(tool_info)
    if not expected_names:
        return dict(arguments)

    normalized_arguments: dict[str, Any] = {}
    argument_sources: dict[str, str] = {}

    for key, value in arguments.items():
        target_key = _normalize_argument_key(key, expected_names, normalized_arguments)

        existing_source = argument_sources.get(target_key)
        if existing_source and existing_source != key:
            existing_value = normalized_arguments[target_key]
            if existing_value != value:
                raise ValueError(
                    f"Conflicting values provided for '{target_key}' via '{existing_source}' and '{key}'."
                )
            continue

        normalized_arguments[target_key] = value
        argument_sources[target_key] = key

    return normalized_arguments


def _validate_argument_keys(
    tool_name: str, tool_info: dict, arguments: dict[str, Any]
) -> None:
    expected_names = _expected_argument_names(tool_info)
    if not expected_names:
        return

    unexpected_names = sorted(name for name in arguments if name not in expected_names)
    if not unexpected_names:
        return

    hints: list[str] = []
    for name in unexpected_names:
        normalized_name = _to_snake_case(name)
        if normalized_name in expected_names:
            hints.append(f"{name} -> {normalized_name}")
            continue

        if normalized_name == "id":
            id_candidates = [
                expected for expected in expected_names if expected.endswith("_id")
            ]
            if len(id_candidates) == 1:
                hints.append(f"{name} -> {id_candidates[0]}")

    hint_text = f" Suggestions: {', '.join(hints)}." if hints else ""
    allowed_preview = ", ".join(sorted(expected_names))
    raise ValueError(
        f"Unsupported arguments for {tool_name}: {', '.join(unexpected_names)}. "
        f"Allowed arguments: {allowed_preview}.{hint_text}"
    )


def _prepare_arguments(tool_info: dict, arguments: dict[str, Any]) -> dict[str, Any]:
    prepared_arguments = _normalize_argument_keys(tool_info, arguments)
    tool_name = (
        tool_info.get("definition").name if tool_info.get("definition") else "tool"
    )
    _validate_argument_keys(tool_name, tool_info, prepared_arguments)
    for param_name, param_type in tool_info.get("parameter_types", {}).items():
        if param_name not in prepared_arguments:
            continue
        prepared_arguments[param_name] = _coerce_argument(
            param_name, prepared_arguments[param_name], param_type
        )

    return prepared_arguments


def execute_registered_tool(
    tools_manager: ToolManager,
    name: str,
    arguments: dict[str, Any] | None,
    client: Client | None = None,
    target_project: str | None = None,
    organization_id: str | None = None,
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    tool_info = tools_manager.get_tool(name)
    if not tool_info:
        raise ValueError(f"Tool {name} not found")

    prepared_arguments = _prepare_arguments(tool_info, arguments or {})

    service_name = tool_info["service_name"]
    method_name = tool_info["method_name"]
    service_cls = SERVICE_CLASSES.get(service_name)
    if service_cls is None:
        raise ValueError(f"Unknown service '{service_name}' for tool {name}")

    # Re-bind the SDK method to a client authenticated for the current request.
    # An explicit client takes precedence (used by tests); otherwise it is resolved
    # from the request's OAuth access token.
    if client is None:
        client = resolve_client(target_project, organization_id)
    bound_method = getattr(service_cls(client), method_name)

    try:
        result = bound_method(**prepared_arguments)
    except AppwriteException as exc:
        raise RuntimeError(_format_appwrite_error(exc)) from exc

    return _format_tool_result(name, result, prepared_arguments)


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _serialize_result(result: Any) -> str:
    return json.dumps(result, indent=2, ensure_ascii=False, default=_json_default)


def _guess_mime_type(data: bytes, tool_name: str, arguments: dict[str, Any]) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"\x1f\x8b"):
        return "application/gzip"
    if data.startswith(b"PK\x03\x04"):
        return "application/zip"
    if tool_name.startswith("avatars_"):
        return "image/png"
    if tool_name == "storage_get_file_preview":
        output = arguments.get("output")
        if isinstance(output, Enum):
            output = output.value
        preview_mime_types = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "gif": "image/gif",
            "webp": "image/webp",
        }
        if output in preview_mime_types:
            return preview_mime_types[output]
        return "image/png"
    return "application/octet-stream"


def _format_binary_result(
    tool_name: str, data: bytes, arguments: dict[str, Any]
) -> list[types.ImageContent | types.EmbeddedResource]:
    mime_type = _guess_mime_type(data, tool_name, arguments)
    encoded = base64.b64encode(data).decode("ascii")
    if mime_type.startswith("image/"):
        return [types.ImageContent(type="image", data=encoded, mimeType=mime_type)]

    return [
        types.EmbeddedResource(
            type="resource",
            resource=types.BlobResourceContents(
                uri=f"appwrite://tool/{tool_name}",
                blob=encoded,
                mimeType=mime_type,
            ),
        )
    ]


def _format_tool_result(
    tool_name: str, result: Any, arguments: dict[str, Any]
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    if hasattr(result, "to_dict") and callable(result.to_dict):
        result = result.to_dict()

    if isinstance(result, bytes):
        return _format_binary_result(tool_name, result, arguments)

    if isinstance(result, (dict, list, tuple, str, int, float, bool)) or result is None:
        return [types.TextContent(type="text", text=_serialize_result(result))]

    return [types.TextContent(type="text", text=str(result))]


def _format_appwrite_error(exc: AppwriteException) -> str:
    details = []
    if getattr(exc, "code", None):
        details.append(f"code={exc.code}")
    if getattr(exc, "type", None):
        details.append(f"type={exc.type}")
    detail_text = f" ({', '.join(details)})" if details else ""
    return f"Appwrite request failed{detail_text}: {exc}"


def build_instructions(transport: str = "http") -> str:
    common = (
        "Appwrite workflow: use appwrite_get_context to understand the current "
        "connection and available project resources, then use appwrite_search_tools "
        "and appwrite_call_tool for specific operations. "
        "Mutating hidden tools require confirm_write=true. "
        "For questions about Appwrite concepts, products, or guides, use "
        "appwrite_search_docs to search the documentation when available. "
        "Large results are stored as resources; read the URI returned by the tool."
    )

    if transport == "stdio":
        return (
            "This local Appwrite MCP connection uses the API key, endpoint, and "
            "project configured in the server environment. Appwrite API calls target "
            "that configured APPWRITE_PROJECT_ID by default. "
            f"{common}"
        )

    return (
        "You authenticate against the Appwrite console, which can list your "
        "organizations and projects but stores no project data itself. Project-scoped "
        "tools (TablesDB, tables, users, storage, functions, messaging, sites) need a "
        "target project: use appwrite_get_context first, then pass the selected "
        "project id as project_id to appwrite_call_tool. "
        "Organization-scoped console tools (e.g. creating a project) need organization_id. "
        f"{common}"
    )


def build_mcp_server(operator: Operator, *, transport: str = "http") -> Server:
    instructions = build_instructions(transport)

    server = Server("Appwrite MCP Server", instructions=instructions)

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return operator.get_public_tools()

    @server.call_tool()
    async def handle_call_tool(
        name: str, arguments: dict | None
    ) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
        if operator.has_public_tool(name):
            return operator.execute_public_tool(name, arguments)

        raise ValueError(f"Tool {name} not found")

    @server.list_resources()
    async def handle_list_resources() -> list[types.Resource]:
        return operator.list_resources()

    @server.list_resource_templates()
    async def handle_list_resource_templates() -> list[types.ResourceTemplate]:
        return operator.list_resource_templates()

    @server.read_resource()
    async def handle_read_resource(uri) -> list[ReadResourceContents]:
        return operator.read_resource(str(uri))

    return server


def build_operator(
    tools_manager: ToolManager, client: Client | None = None
) -> Operator:
    """Wire the operator surface to the per-request execution path. The execution
    callback re-binds each call to a per-request client via `resolve_client` in
    HTTP/OAuth mode. Pass a client for stdio/API-key mode.

    The docs-search tool is wired in only when its committed index and an
    OPENAI_API_KEY are both available; otherwise the server boots without it."""
    docs_search = DocsSearch()
    if docs_search.available:
        _log_startup("Documentation search enabled (appwrite_search_docs)")
    else:
        _log_startup(
            "Documentation search disabled: docs index or OPENAI_API_KEY not configured"
        )
        docs_search = None

    return Operator(
        tools_manager,
        lambda tool_name, tool_arguments, target_project=None, organization_id=None: execute_registered_tool(
            tools_manager,
            tool_name,
            tool_arguments,
            client=client,
            target_project=target_project,
            organization_id=organization_id,
        ),
        context_provider=lambda arguments: _get_context_for_request(arguments, client),
        docs_search=docs_search,
    )


def _get_context_for_request(
    arguments: dict[str, Any], client: Client | None = None
) -> dict[str, Any]:
    project_id = arguments.get("project_id", arguments.get("projectId"))
    organization_id = arguments.get("organization_id", arguments.get("organizationId"))
    include_services = bool(
        arguments.get("include_services", arguments.get("includeServices", True))
    )
    sample_limit = _normalize_sample_limit(
        arguments.get("sample_limit", arguments.get("sampleLimit", 5))
    )

    if client is not None:
        return get_appwrite_context(
            client,
            mode="api_key_project",
            project_id=project_id,
            include_services=include_services,
            sample_limit=sample_limit,
        )

    base_client = resolve_client()

    def client_factory(
        target_project: str | None, target_organization: str | None
    ) -> Client:
        return resolve_client(target_project, target_organization)

    return get_appwrite_context(
        base_client,
        mode="oauth_console",
        client_factory=client_factory,
        project_id=project_id,
        organization_id=organization_id,
        include_services=include_services,
        sample_limit=sample_limit,
    )


def build_catalog_tools_manager() -> ToolManager:
    """Build the tool catalog/schema once from SDK introspection. Credentials arrive
    per request (OAuth) rather than at startup, so a credential-less client suffices."""
    return register_services(build_introspection_client())


async def run_stdio() -> None:
    """Serve a local stdio MCP using APPWRITE_* API-key configuration."""
    _log_startup("Loading Appwrite configuration")
    config = load_appwrite_config()
    client = build_client(config)
    _log_startup(f"Using Appwrite endpoint: {config.endpoint}")
    _log_startup("Registering Appwrite services")
    tools_manager = register_services(client)
    _log_startup("Starting Appwrite service validation")
    validate_services(tools_manager)
    _log_startup("Building Appwrite operator surface")
    operator = build_operator(tools_manager, client=client)
    server = build_mcp_server(operator, transport="stdio")

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        _log_startup("MCP transport: stdio")
        _log_startup("Appwrite MCP server ready")
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="appwrite",
                server_version=SERVER_VERSION,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main():
    """Entry point: stdio by default, or Streamable HTTP when requested."""
    load_environment()
    args = parse_args()

    if args.transport == "stdio":
        asyncio.run(run_stdio())
        return 0

    from .http_app import run_http

    run_http(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    main()
