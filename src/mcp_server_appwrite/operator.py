from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import uuid4

import mcp.types as types
from mcp.server.lowlevel.helper_types import ReadResourceContents

from . import telemetry
from .constants import (
    CATALOG_URI,
    CREATE_HINTS,
    DELETE_HINTS,
    PREVIEW_THRESHOLD,
    READ_HINTS,
    READ_VERBS,
    RESULT_STORE_SIZE,
    RESULT_URI_TEMPLATE,
    SEARCH_LIMIT,
    UPDATE_HINTS,
    VERBS,
)
from .docs_search import DocsSearch
from .error_classification import (
    ErrorCategory,
    WriteConfirmationRequired,
    classify_tool_error,
)
from .tool_manager import ToolManager

ToolContent = types.TextContent | types.ImageContent | types.EmbeddedResource
# (tool_name, arguments, project_id, organization_id) -> content
ToolExecutor = Callable[
    [str, dict[str, Any], str | None, str | None], list[ToolContent]
]
ContextProvider = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class CatalogEntry:
    action_verb: str
    classification: str
    context_scope: str
    description: str
    input_schema: dict[str, Any]
    required: list[str]
    resource_name: str
    service_name: str
    tool_name: str


@dataclass(frozen=True)
class SearchResult:
    entry: CatalogEntry
    missing_required: list[str]
    score: int


@dataclass
class StoredResult:
    content: list[ToolContent]
    created_at: str
    result_id: str
    text: str
    tool_name: str

    @property
    def uri(self) -> str:
        return RESULT_URI_TEMPLATE.format(result_id=self.result_id)


class ResultStore:
    def __init__(self, max_size: int = RESULT_STORE_SIZE):
        self._entries: OrderedDict[str, StoredResult] = OrderedDict()
        self._max_size = max_size
        self._lock = threading.Lock()

    def get(self, result_id: str) -> StoredResult | None:
        with self._lock:
            return self._entries.get(result_id)

    def list(self) -> list[StoredResult]:
        with self._lock:
            return list(self._entries.values())

    def save(
        self, tool_name: str, content: list[ToolContent], text: str
    ) -> StoredResult:
        result = StoredResult(
            content=content,
            created_at=_now_iso(),
            result_id=str(uuid4()),
            text=text,
            tool_name=tool_name,
        )
        with self._lock:
            self._entries[result.result_id] = result
            while len(self._entries) > self._max_size:
                self._entries.popitem(last=False)
        return result


class Operator:
    def __init__(
        self,
        tools_manager: ToolManager,
        execute_tool: ToolExecutor,
        *,
        docs_search: DocsSearch | None = None,
        context_provider: ContextProvider | None = None,
        preview_threshold: int = PREVIEW_THRESHOLD,
        require_target_context: bool = False,
        store_results: bool = True,
        search_limit: int = SEARCH_LIMIT,
    ):
        self._tools_manager = tools_manager
        self._execute_tool = execute_tool
        self._docs_search = docs_search
        self._context_provider = context_provider
        self._preview_threshold = preview_threshold
        self._require_target_context = require_target_context
        self._store_results = store_results
        self._search_limit = search_limit
        self._result_store = ResultStore()
        self._catalog = self._build_catalog()
        self._cached_catalog_json = self._catalog_json()
        self._catalog_map = {entry.tool_name: entry for entry in self._catalog}

    def get_catalog_resource_uri(self) -> str:
        return CATALOG_URI

    @property
    def docs_enabled(self) -> bool:
        return self._docs_search is not None

    def get_public_tools(self) -> list[types.Tool]:
        tools = [
            types.Tool(
                name="appwrite_get_context",
                description=(
                    "Get an adaptive Appwrite account/project context summary, including "
                    "available projects and per-project service counts where the current "
                    "connection can read them. Use this before searching the hidden catalog "
                    "when orienting to a user's Appwrite workspace."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "project_id": {
                            "type": "string",
                            "description": "Optional project ID to focus the context summary.",
                        },
                        "organization_id": {
                            "type": "string",
                            "description": "Optional organization ID to focus project discovery.",
                        },
                        "include_services": {
                            "type": "boolean",
                            "description": (
                                "Include per-project service summaries. Defaults to true, "
                                "but large project sets are skipped unless project_id is provided."
                            ),
                        },
                        "service_detail": {
                            "type": "string",
                            "enum": ["totals", "samples"],
                            "description": "Service summary detail. Defaults to totals; samples includes small item previews.",
                        },
                        "sample_limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 25,
                            "description": "Maximum sample items per service when service_detail=samples. Defaults to 5.",
                        },
                    },
                    "additionalProperties": False,
                },
            ),
            types.Tool(
                name="appwrite_search_tools",
                description=(
                    "Search the hidden Appwrite tool catalog by natural language query. "
                    "Matches include parameter schemas (name, type, required/optional, "
                    "description) to use with appwrite_call_tool. "
                    "Use this before appwrite_call_tool when using the Appwrite operator surface."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural language query such as 'list databases' or 'create storage bucket'.",
                        },
                        "service_hints": {
                            "oneOf": [
                                {"type": "string"},
                                {"type": "array", "items": {"type": "string"}},
                            ],
                            "description": "Optional service filter such as 'tables_db', 'storage', or ['users', 'teams'].",
                        },
                        "argument_hints": {
                            "type": "object",
                            "description": "Known argument values used to boost matching tools and detect missing required fields.",
                        },
                        "include_mutating": {
                            "type": "boolean",
                            "description": "Include write and delete tools in the search results.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 20,
                            "description": f"Maximum number of matches to return. Defaults to {self._search_limit}.",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
            types.Tool(
                name="appwrite_call_tool",
                description=(
                    "Call a hidden Appwrite tool by name. Put Appwrite parameters inside `arguments`. "
                    "Mutating tools require confirm_write=true. Hidden Appwrite parameters accept "
                    "canonical snake_case names and common camelCase aliases."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "tool_name": {
                            "type": "string",
                            "description": "Exact hidden Appwrite tool name returned by appwrite_search_tools.",
                        },
                        "arguments": {
                            "type": "object",
                            "description": "Arguments to forward to the hidden Appwrite tool.",
                        },
                        "confirm_write": {
                            "type": "boolean",
                            "description": "Required for create, update, and delete tools.",
                        },
                        "project_id": {
                            "type": "string",
                            "description": (
                                "Appwrite project ID to act on (sent as X-Appwrite-Project). "
                                "The connection authenticates against the Appwrite console, which "
                                "can list your projects/organizations but holds no data — so "
                                "project-scoped tools (Advisor, databases, documents, vectors, users, "
                                "storage, functions, messaging, sites, usage, VCS, and WAF) "
                                "require this. Search results identify each tool's context. Discover a project "
                                "first, then pass its id. Omit for console/account-level tools."
                            ),
                        },
                        "organization_id": {
                            "type": "string",
                            "description": (
                                "Appwrite organization (team) ID to act on (sent as "
                                "X-Appwrite-Organization). Required for organization-scoped "
                                "console tools such as creating a project. Omit otherwise."
                            ),
                        },
                    },
                    "required": ["tool_name"],
                    "additionalProperties": True,
                },
            ),
        ]

        if self._docs_search is not None:
            tools.append(self._docs_search.get_tool())

        return tools

    def has_public_tool(self, name: str) -> bool:
        names = {"appwrite_get_context", "appwrite_search_tools", "appwrite_call_tool"}
        if self._docs_search is not None:
            names.add(self._docs_search.get_tool().name)
        return name in names

    def execute_public_tool(
        self, name: str, arguments: dict[str, Any] | None
    ) -> list[ToolContent]:
        start = time.monotonic()
        status = "success"
        error_type: str | None = None
        error_category: ErrorCategory | None = None
        output_chars = 0
        telemetry.tool_call_started(name)
        try:
            result = self._dispatch_public_tool(name, arguments)
            output_chars = _content_size(result)
            return result
        except Exception as exc:
            status = "error"
            error_type = type(exc).__name__
            error_category = classify_tool_error(exc)
            raise
        finally:
            telemetry.record_tool_call(
                name,
                status,
                time.monotonic() - start,
                error_type=error_type,
                error_category=error_category,
                input_chars=len(json.dumps(arguments)) if arguments else 0,
                output_chars=output_chars,
            )

    def _dispatch_public_tool(
        self, name: str, arguments: dict[str, Any] | None
    ) -> list[ToolContent]:
        if name == "appwrite_get_context":
            return self._get_context(arguments or {})
        if name == "appwrite_search_tools":
            return self._search_tools(arguments or {})
        if name == "appwrite_call_tool":
            return self._call_hidden_tool(arguments or {})
        if self._docs_search is not None and name == self._docs_search.get_tool().name:
            content = self._docs_search.search(arguments or {})
            return self._preview_or_store_result(name, content)
        raise ValueError(f"Unknown public Appwrite tool {name}")

    def _get_context(self, arguments: dict[str, Any]) -> list[ToolContent]:
        if self._context_provider is None:
            raise RuntimeError("Appwrite context provider is not configured.")
        context = self._context_provider(arguments)
        return [types.TextContent(type="text", text=json.dumps(context, indent=2))]

    def list_resources(self) -> list[types.Resource]:
        resources = [
            types.Resource(
                uri=CATALOG_URI,
                name="Appwrite Hidden Tool Catalog",
                description="Full internal Appwrite tool catalog used by the Appwrite operator surface.",
                mime_type="application/json",
                size=len(self._cached_catalog_json.encode("utf-8")),
            )
        ]

        for stored_result in self._result_store.list():
            resources.append(
                types.Resource(
                    uri=stored_result.uri,
                    name=f"{stored_result.tool_name} result",
                    description="Stored Appwrite tool result. Read this resource to inspect the full output.",
                    mime_type="application/json",
                    size=len(stored_result.text.encode("utf-8")),
                )
            )

        return resources

    def list_resource_templates(self) -> list[types.ResourceTemplate]:
        return [
            types.ResourceTemplate(
                uri_template=RESULT_URI_TEMPLATE,
                name="Stored Appwrite Tool Result",
                description="Stored result payloads created by appwrite_call_tool.",
                mime_type="application/json",
            )
        ]

    def read_resource(self, uri: str) -> list[ReadResourceContents]:
        parsed = urlparse(uri)

        if uri == CATALOG_URI:
            return [ReadResourceContents(self._cached_catalog_json, "application/json")]

        if (
            parsed.scheme == "appwrite"
            and parsed.netloc == "operator"
            and parsed.path.startswith("/results/")
        ):
            result_id = parsed.path.split("/")[-1]
            stored_result = self._result_store.get(result_id)
            if not stored_result:
                raise ValueError(f"Stored result {result_id} was not found.")
            return [ReadResourceContents(stored_result.text, "application/json")]

        raise ValueError(f"Unknown resource URI: {uri}")

    def _build_catalog(self) -> list[CatalogEntry]:
        entries: list[CatalogEntry] = []
        for tool in self._tools_manager.get_all_tools():
            parsed = _parse_tool_name(tool.name)
            input_schema = tool.input_schema or {}
            tool_info = self._tools_manager.get_tool(tool.name) or {}
            entries.append(
                CatalogEntry(
                    action_verb=parsed["action_verb"],
                    classification=parsed["classification"],
                    context_scope=str(tool_info.get("context_scope", "console")),
                    description=tool.description or "",
                    input_schema=input_schema,
                    required=list(input_schema.get("required", [])),
                    resource_name=parsed["resource_name"],
                    service_name=parsed["service_name"],
                    tool_name=tool.name,
                )
            )
        return entries

    def _catalog_json(self) -> str:
        # Parameter names and shapes are included (descriptions are not, to keep
        # the resource small) so a client can read the catalog once instead of
        # paying a search call per tool it already knows it wants.
        return json.dumps(
            [
                {
                    "action_verb": entry.action_verb,
                    "classification": entry.classification,
                    "context_scope": entry.context_scope,
                    "description": entry.description[:_CATALOG_DESCRIPTION_LIMIT],
                    "parameters": _catalog_parameters(entry),
                    "required": entry.required,
                    "resource_name": entry.resource_name,
                    "service_name": entry.service_name,
                    "tool_name": entry.tool_name,
                }
                for entry in self._catalog
            ],
            indent=2,
            ensure_ascii=False,
        )

    def _search_tools(self, arguments: dict[str, Any]) -> list[ToolContent]:
        query = str(arguments.get("query", "")).strip()
        if len(query) < 3:
            raise ValueError("query must be at least 3 characters long.")

        include_mutating = _resolve_include_mutating(
            arguments.get("include_mutating", arguments.get("includeMutating")),
            query,
        )
        matches = self._search_catalog(
            query=query,
            service_hints=_normalize_string_list(
                arguments.get("service_hints", arguments.get("serviceHints"))
            ),
            argument_hints=_normalize_object(
                arguments.get("argument_hints", arguments.get("argumentHints"))
            ),
            include_mutating=include_mutating,
            limit=_normalize_limit(arguments.get("limit"), self._search_limit),
        )

        lines: list[str] = []
        if not matches:
            lines.append("No Appwrite tools matched. Try broader terms.")
        else:
            for index, match in enumerate(matches, start=1):
                required = (
                    ", ".join(match.entry.required) if match.entry.required else "none"
                )
                missing = (
                    f" missing={', '.join(match.missing_required)}"
                    if match.missing_required
                    else ""
                )
                description = (
                    f"\n   {match.entry.description[:_TOOL_DESCRIPTION_LIMIT]}"
                    if match.entry.description
                    else ""
                )
                params = _format_params_block(match.entry)
                lines.append(
                    f"{index}. tool={match.entry.tool_name} service={match.entry.service_name} "
                    f"class={match.entry.classification} context={match.entry.context_scope} "
                    f"required={required}{missing} "
                    f"score={match.score}{description}{params}"
                )
            lines.append("")
            lines.append(
                "Call via appwrite_call_tool with tool_name and arguments. "
                f"Full catalog resource: {CATALOG_URI}"
            )

        return [types.TextContent(type="text", text="\n".join(lines))]

    def _call_hidden_tool(self, raw_arguments: dict[str, Any]) -> list[ToolContent]:
        tool_name = raw_arguments.get("tool_name", raw_arguments.get("toolName"))
        if not isinstance(tool_name, str) or not tool_name:
            raise ValueError("tool_name is required.")

        entry = self._catalog_map.get(tool_name)
        if not entry:
            telemetry.record_hallucination(tool_name)
            # Naming the near misses saves the search round trip a near-miss guess
            # would otherwise cost.
            suggestions = get_close_matches(
                tool_name, self._catalog_map, n=_SUGGESTION_LIMIT, cutoff=0.6
            )
            hint = (
                f" Closest matches: {', '.join(suggestions)}."
                if suggestions
                else " Use appwrite_search_tools to find the right tool."
            )
            raise ValueError(f"Tool {tool_name} was not found.{hint}")

        confirm_write = bool(
            raw_arguments.get("confirm_write", raw_arguments.get("confirmWrite", False))
        )
        if entry.classification != "read" and not confirm_write:
            raise WriteConfirmationRequired(
                f"Tool {tool_name} is {entry.classification}. Re-run appwrite_call_tool with confirm_write=true if you intend to mutate Appwrite state."
            )

        project_id = raw_arguments.get("project_id", raw_arguments.get("projectId"))
        organization_id = raw_arguments.get(
            "organization_id", raw_arguments.get("organizationId")
        )
        if self._require_target_context:
            if entry.context_scope == "project" and not project_id:
                raise ValueError(
                    f"Tool {tool_name} requires project_id. Use appwrite_get_context "
                    "to select a project, then retry with that project ID."
                )
            if entry.context_scope == "organization" and not organization_id:
                raise ValueError(
                    f"Tool {tool_name} requires organization_id. Use appwrite_get_context "
                    "to select an organization, then retry with that organization ID."
                )
        arguments_object = _normalize_arguments(raw_arguments)
        result_content = self._execute_tool(
            tool_name, arguments_object, project_id, organization_id
        )
        return self._preview_or_store_result(tool_name, result_content)

    def _preview_or_store_result(
        self, tool_name: str, content: list[ToolContent]
    ) -> list[ToolContent]:
        if not self._store_results:
            return content

        if all(isinstance(item, types.TextContent) for item in content):
            full_text = "\n".join(
                item.text for item in content if isinstance(item, types.TextContent)
            ).strip()
            if len(full_text) <= self._preview_threshold:
                return content

            stored_result = self._result_store.save(
                tool_name, content, _serialize_content(content)
            )
            preview = full_text[: self._preview_threshold]
            return [
                types.TextContent(
                    type="text",
                    text=(
                        f"{preview}\n...\nFull result stored at {stored_result.uri}. "
                        "Use resources/read with that URI to inspect the complete output."
                    ),
                )
            ]

        stored_result = self._result_store.save(
            tool_name, content, _serialize_content(content)
        )
        summary = ", ".join(_summarize_content_item(item) for item in content)
        return [
            types.TextContent(
                type="text",
                text=(
                    f"Result for {tool_name} was stored at {stored_result.uri}. "
                    f"Content summary: {summary}. Use resources/read with that URI to inspect the full payload."
                ),
            )
        ]

    def _search_catalog(
        self,
        *,
        query: str,
        service_hints: list[str] | None,
        argument_hints: dict[str, Any] | None,
        include_mutating: bool,
        limit: int,
    ) -> list[SearchResult]:
        query_tokens = _tokenize(query)
        query_lower = query.lower()
        service_hint_set = {_normalize_token(item) for item in (service_hints or [])}
        ranked: list[SearchResult] = []

        for entry in self._catalog:
            if not include_mutating and entry.classification != "read":
                continue

            if (
                service_hint_set
                and _normalize_token(entry.service_name) not in service_hint_set
            ):
                continue

            missing_required = _get_missing_required(entry, argument_hints)
            score = _compute_score(
                entry, query_tokens, query_lower, service_hint_set, missing_required
            )
            # Relevance is gated by whether the query names the entry at all
            # (see _compute_score), not by a score threshold: an inflection is a
            # weaker signal than an exact token but still a real match, and a
            # numeric floor on top of the gate only discards those.
            if score <= 0:
                continue
            ranked.append(
                SearchResult(
                    entry=entry, missing_required=missing_required, score=score
                )
            )

        ranked.sort(key=lambda item: (-item.score, item.entry.tool_name))
        return ranked[:limit]


_MIN_INFLECTION_TOKEN_LENGTH = 3
_MAX_INFLECTION_SUFFIX = 2
_SUGGESTION_LIMIT = 5
# A read whose query names neither "get" nor "list".
AMBIGUOUS_READ_INTENT = "read"


def _normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _tokenize(value: str) -> list[str]:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    tokens = re.split(r"[^a-zA-Z0-9]+", normalized.lower())
    return list(dict.fromkeys(token for token in tokens if len(token) >= 2))


def _is_inflection(first: str, second: str) -> bool:
    """Whether two tokens differ only by a common inflection.

    Searching "row" must still find ``list_rows``, and "policy" must find
    ``policies``. Plain containment is too loose to carry that — it also matches
    "id" against "identity" and pulled unrelated services into every result — so
    require a shared stem and a short difference in length instead.
    """
    shorter, longer = sorted((first, second), key=len)
    if len(shorter) < _MIN_INFLECTION_TOKEN_LENGTH:
        return False
    if len(longer) - len(shorter) > _MAX_INFLECTION_SUFFIX:
        return False

    shared = 0
    for shorter_char, longer_char in zip(shorter, longer):
        if shorter_char != longer_char:
            break
        shared += 1

    return shared >= _MIN_INFLECTION_TOKEN_LENGTH and shared >= len(shorter) - 2


def _classify_verb(action_verb: str) -> str:
    if action_verb in READ_VERBS:
        return "read"
    if action_verb in {"create", "update"}:
        return "write"
    if action_verb == "delete":
        return "delete"
    return "unknown"


def _parse_tool_name(tool_name: str) -> dict[str, str]:
    tokens = [token for token in tool_name.lower().split("_") if token]
    verb_index = next(
        (index for index, token in enumerate(tokens) if token in VERBS), -1
    )
    if verb_index < 0:
        return {
            "action_verb": "unknown",
            "classification": "unknown",
            "resource_name": "",
            "service_name": tool_name,
        }

    action_verb = tokens[verb_index]
    return {
        "action_verb": action_verb,
        "classification": _classify_verb(action_verb),
        "resource_name": "_".join(tokens[verb_index + 1 :]),
        "service_name": "_".join(tokens[:verb_index]),
    }


def _get_missing_required(
    entry: CatalogEntry, argument_hints: dict[str, Any] | None
) -> list[str]:
    if not argument_hints:
        return []
    return [name for name in entry.required if name not in argument_hints]


def _has_schema_property(entry: CatalogEntry, key: str) -> bool:
    properties = entry.input_schema.get("properties")
    return isinstance(properties, dict) and key in properties


_PARAM_DESCRIPTION_LIMIT = 600
_TOOL_DESCRIPTION_LIMIT = 400
_CATALOG_DESCRIPTION_LIMIT = 200


def _json_schema_type_label(schema: dict[str, Any] | Any) -> str:
    """Render a parameter's accepted shape.

    Search output is the only channel through which the model sees a hidden
    tool's schema, so enum members and union branches must survive: labelling a
    ``oneOf[string, object]`` upload parameter as ``string`` makes the object
    form undiscoverable, and dropping enum members leaves values unguessable.
    """
    if not isinstance(schema, dict):
        return "string"

    branches = schema.get("oneOf") or schema.get("anyOf")
    if isinstance(branches, list) and branches:
        labels = list(
            dict.fromkeys(_json_schema_type_label(branch) for branch in branches)
        )
        return "|".join(labels)

    schema_type = schema.get("type")
    if schema_type == "array":
        items = schema.get("items")
        if isinstance(items, dict):
            return f"array[{_json_schema_type_label(items)}]"
        return "array"

    label = schema_type if isinstance(schema_type, str) and schema_type else "string"

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and enum_values:
        members = "|".join(str(value) for value in enum_values)
        return f"{label} enum[{members}]"

    return label


def _catalog_parameters(entry: CatalogEntry) -> dict[str, str]:
    properties = entry.input_schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {
        name: _json_schema_type_label(schema) for name, schema in properties.items()
    }


def _object_keys_hint(schema: dict[str, Any] | Any) -> str:
    """Names of the properties an object parameter (or object branch) accepts."""
    if not isinstance(schema, dict):
        return ""

    keys: list[str] = []
    candidates = [schema]
    branches = schema.get("oneOf") or schema.get("anyOf")
    if isinstance(branches, list):
        candidates.extend(branch for branch in branches if isinstance(branch, dict))

    for candidate in candidates:
        properties = candidate.get("properties")
        if isinstance(properties, dict):
            keys.extend(key for key in properties if key not in keys)

    return ", ".join(keys)


def _format_params_block(entry: CatalogEntry) -> str:
    properties = entry.input_schema.get("properties")
    if not isinstance(properties, dict) or not properties:
        return ""

    required = set(entry.required)
    ordered_names = [name for name in entry.required if name in properties]
    ordered_names.extend(name for name in properties if name not in required)

    lines = ["\n   params:"]
    for name in ordered_names:
        prop_schema = properties.get(name)
        if not isinstance(prop_schema, dict):
            prop_schema = {}
        type_label = _json_schema_type_label(prop_schema)
        requirement = "required" if name in required else "optional"
        description = str(prop_schema.get("description") or "").strip()
        if description:
            lines.append(
                f"     - {name} ({type_label}, {requirement}): "
                f"{description[:_PARAM_DESCRIPTION_LIMIT]}"
            )
        else:
            lines.append(f"     - {name} ({type_label}, {requirement})")

        object_keys = _object_keys_hint(prop_schema)
        if object_keys:
            lines.append(f"       object keys: {object_keys}")

    return "\n".join(lines)


def _compute_score(
    entry: CatalogEntry,
    query_tokens: list[str],
    query_lower: str,
    service_hints: set[str],
    missing_required: list[str],
) -> int:
    haystack_tokens = set(
        _tokenize(
            " ".join(
                [
                    entry.tool_name,
                    entry.description,
                    entry.service_name,
                    entry.resource_name,
                ]
            )
        )
    )

    score = 0
    matches = 0
    for query_token in query_tokens:
        if query_token in haystack_tokens:
            matches += 1
            score += 5
        elif any(
            _is_inflection(query_token, haystack_token)
            for haystack_token in haystack_tokens
        ):
            matches += 1
            score += 3

    names_this_tool = entry.tool_name.lower() in query_lower
    hinted_service = bool(
        service_hints and _normalize_token(entry.service_name) in service_hints
    )
    # An entry nothing in the query actually names is noise, however much
    # generic credit the heuristics below would award it.
    if not matches and not hinted_service and not names_this_tool:
        return 0

    if hinted_service:
        score += 8

    query_intent = _infer_query_intent(query_tokens)
    if query_intent == AMBIGUOUS_READ_INTENT:
        # Let the resource tokens decide between get and list.
        score += 6 if entry.classification == "read" else -5
    elif query_intent == entry.action_verb:
        score += 12
    elif query_intent:
        if query_intent in READ_VERBS and entry.classification != "read":
            score -= 5
        elif query_intent not in READ_VERBS and entry.classification == "read":
            score -= 5

    if entry.classification == "read" and not query_intent:
        score += 2

    if missing_required:
        score -= 2 * len(missing_required)
    elif entry.required:
        score += 3

    if names_this_tool:
        score += 10

    return score


def _infer_query_intent(query_tokens: list[str]) -> str | None:
    """Infer the action the query is asking for.

    ``AMBIGUOUS_READ_INTENT`` means "a read, but get or list is undecided" —
    words like "read" or "fetch" name neither. Collapsing them onto ``get``
    scored every single-item tool above every list tool, so "read rows with
    pagination" returned eight get-one-row tools and no list tool at all.
    """
    token_set = set(query_tokens)
    if token_set & CREATE_HINTS:
        return "create"
    if token_set & UPDATE_HINTS:
        return "update"
    if token_set & DELETE_HINTS:
        return "delete"
    if token_set & {"list"}:
        return "list"
    if token_set & {"get"}:
        return "get"
    if token_set & READ_HINTS:
        return AMBIGUOUS_READ_INTENT
    return None


def _resolve_include_mutating(value: Any, query: str) -> bool:
    if value is not None:
        return bool(value)

    query_intent = _infer_query_intent(_tokenize(query))
    return query_intent not in {None, "list", "get", AMBIGUOUS_READ_INTENT}


def _normalize_string_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    raise ValueError("Expected a string or list of strings.")


def _normalize_object(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    raise ValueError("Expected an object.")


def _normalize_limit(value: Any, default: int) -> int:
    if value is None:
        return default
    limit = int(value)
    if limit < 1:
        raise ValueError("limit must be at least 1.")
    return min(limit, 20)


def _normalize_arguments(raw_arguments: dict[str, Any]) -> dict[str, Any]:
    merged_arguments: dict[str, Any] = {}

    arguments_value = raw_arguments.get("arguments", raw_arguments.get("args"))
    if isinstance(arguments_value, dict):
        merged_arguments.update(arguments_value)
    elif isinstance(arguments_value, str):
        try:
            parsed = json.loads(arguments_value)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "arguments must be valid JSON when passed as a string."
            ) from exc
        if not isinstance(parsed, dict):
            raise ValueError("arguments JSON must decode to an object.")
        merged_arguments.update(parsed)

    for key, value in raw_arguments.items():
        if key in {
            "tool_name",
            "toolName",
            "arguments",
            "args",
            "confirm_write",
            "confirmWrite",
            "project_id",
            "projectId",
            "organization_id",
            "organizationId",
        }:
            continue
        if value is not None:
            merged_arguments[key] = value

    return merged_arguments


def _content_size(content: list[ToolContent]) -> int:
    total = 0
    for item in content:
        if isinstance(item, types.TextContent):
            total += len(item.text)
        elif isinstance(item, types.ImageContent):
            total += len(item.data)
        else:
            text = getattr(item.resource, "text", None)
            blob = getattr(item.resource, "blob", None)
            total += len(text or blob or "")
    return total


def _serialize_content(content: list[ToolContent]) -> str:
    return json.dumps(
        [item.model_dump(mode="json", by_alias=True) for item in content],
        indent=2,
        ensure_ascii=False,
    )


def _summarize_content_item(item: ToolContent) -> str:
    if isinstance(item, types.TextContent):
        preview = item.text.strip().splitlines()[0] if item.text.strip() else "text"
        return f"text:{preview[:60]}"
    if isinstance(item, types.ImageContent):
        return f"image:{item.mime_type}"
    return f"resource:{item.resource.mime_type or 'application/octet-stream'}"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
