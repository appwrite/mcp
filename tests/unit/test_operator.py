import unittest
from concurrent.futures import ThreadPoolExecutor

import mcp.types as types

from mcp_server_appwrite.constants import PREVIEW_THRESHOLD
from mcp_server_appwrite.error_classification import WriteConfirmationRequired
from mcp_server_appwrite.operator import CATALOG_URI, Operator, ResultStore
from mcp_server_appwrite.tool_manager import ToolManager

# Tracks the threshold rather than hardcoding a size, so tuning the threshold
# does not silently turn these into same-shape tests of the inline path.
OVERSIZED_TEXT = "x" * (PREVIEW_THRESHOLD + 100)


def make_tool(
    name: str,
    description: str,
    required: list[str] | None = None,
    properties: dict | None = None,
) -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties
            or {
                "parameter": {"type": "string"},
            },
            "required": required or [],
        },
    )


class FakeDocsSearch:
    """Minimal stand-in for DocsSearch used to test the operator wiring."""

    def __init__(self, content):
        self._content = content

    def get_tool(self) -> types.Tool:
        return types.Tool(
            name="appwrite_search_docs",
            description="Search the Appwrite documentation.",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )

    def search(self, arguments):
        return self._content


class OperatorTests(unittest.TestCase):
    def make_runtime(self, executor):
        manager = ToolManager()
        manager.tools_registry = {
            "tables_db_list": {
                "definition": make_tool("tables_db_list", "List all databases."),
                "function": object(),
                "parameter_types": {},
            },
            "functions_get": {
                "definition": make_tool("functions_get", "Get a function."),
                "function": object(),
                "parameter_types": {},
            },
            "tables_db_create": {
                "definition": make_tool(
                    "tables_db_create",
                    "Create a database.",
                    ["database_id", "name"],
                    properties={
                        "database_id": {
                            "type": "string",
                            "description": (
                                "Unique Id. Choose a custom ID or generate a "
                                "random ID with `ID.unique()`."
                            ),
                        },
                        "name": {
                            "type": "string",
                            "description": "Database name. Max length: 128 chars.",
                        },
                        "enabled": {
                            "type": "boolean",
                            "description": "Is the database enabled?",
                        },
                    },
                ),
                "function": object(),
                "parameter_types": {},
            },
            "functions_list": {
                "definition": make_tool("functions_list", "List all functions."),
                "function": object(),
                "parameter_types": {},
            },
            "users_list": {
                "definition": make_tool(
                    "users_list",
                    "Get a list of all the project's users.",
                    properties={
                        "queries": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Array of query strings generated using the "
                                "Query class provided by the SDK."
                            ),
                        },
                        "search": {
                            "type": "string",
                            "description": "Search term to filter your list results.",
                        },
                    },
                ),
                "function": object(),
                "parameter_types": {},
            },
            "functions_create": {
                "definition": make_tool(
                    "functions_create",
                    "Create a function.",
                    ["function_id", "name", "runtime"],
                ),
                "function": object(),
                "parameter_types": {},
            },
            "tables_db_create_string_column": {
                "definition": make_tool(
                    "tables_db_create_string_column",
                    "Create a string column in a table.",
                    ["database_id", "table_id", "key", "size", "required"],
                    properties={
                        "database_id": {
                            "type": "string",
                            "description": "Database ID.",
                        },
                        "table_id": {
                            "type": "string",
                            "description": "Table ID.",
                        },
                        "key": {
                            "type": "string",
                            "description": "Column Key.",
                        },
                        "size": {
                            "type": "number",
                            "description": (
                                "Column size for text columns, in number of characters."
                            ),
                        },
                        "required": {
                            "type": "boolean",
                            "description": "Is column required?",
                        },
                        "default": {
                            "type": "string",
                            "description": "Default value for column when not provided.",
                        },
                    },
                ),
                "function": object(),
                "parameter_types": {},
            },
            "tables_db_create_index": {
                "definition": make_tool(
                    "tables_db_create_index",
                    "Create an index for a table.",
                    ["database_id", "table_id", "key", "type", "attributes"],
                ),
                "function": object(),
                "parameter_types": {},
            },
        }
        return Operator(manager, executor)

    def make_runtime_with_docs(self, docs_search):
        manager = ToolManager()
        manager.tools_registry = {
            "tables_db_list": {
                "definition": make_tool("tables_db_list", "List all databases."),
                "function": object(),
                "parameter_types": {},
            },
        }
        return Operator(manager, lambda *_: [], docs_search=docs_search)

    def test_docs_tool_absent_without_docs_search(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])
        names = {tool.name for tool in runtime.get_public_tools()}
        self.assertEqual(
            names,
            {"appwrite_get_context", "appwrite_search_tools", "appwrite_call_tool"},
        )
        self.assertFalse(runtime.has_public_tool("appwrite_search_docs"))

    def test_docs_tool_listed_and_dispatched(self):
        docs = FakeDocsSearch([types.TextContent(type="text", text='{"results": []}')])
        runtime = self.make_runtime_with_docs(docs)

        tools = runtime.get_public_tools()
        self.assertEqual(len(tools), 4)
        self.assertIn("appwrite_search_docs", {tool.name for tool in tools})
        self.assertTrue(runtime.has_public_tool("appwrite_search_docs"))

        result = runtime.execute_public_tool(
            "appwrite_search_docs", {"query": "databases"}
        )
        self.assertEqual(result[0].text, '{"results": []}')

    def test_docs_tool_large_result_is_stored_as_resource(self):
        docs = FakeDocsSearch([types.TextContent(type="text", text=OVERSIZED_TEXT)])
        runtime = self.make_runtime_with_docs(docs)

        result = runtime.execute_public_tool(
            "appwrite_search_docs", {"query": "databases"}
        )
        self.assertIn("appwrite://operator/results/", result[0].text)

    def test_search_tools_returns_ranked_match(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])

        result = runtime.execute_public_tool(
            "appwrite_search_tools",
            {"query": "list databases"},
        )

        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], types.TextContent)
        self.assertIn("tables_db_list", result[0].text)
        self.assertIn("context=console", result[0].text)
        self.assertIn(CATALOG_URI, result[0].text)

    def test_search_output_renders_enum_members(self):
        manager = ToolManager()
        manager.tools_registry = {
            "tables_db_create_relationship_column": {
                "definition": make_tool(
                    "tables_db_create_relationship_column",
                    "Create relationship column.",
                    ["type"],
                    properties={
                        "type": {
                            "type": "string",
                            "enum": ["oneToOne", "oneToMany"],
                            "description": "Relation type",
                        },
                    },
                ),
            }
        }
        runtime = Operator(manager, lambda *_: [])

        result = runtime.execute_public_tool(
            "appwrite_search_tools",
            {"query": "create relationship column", "include_mutating": True},
        )

        # Search output is the only channel carrying a hidden tool's schema, so
        # unguessable values must survive rendering.
        self.assertIn("string enum[oneToOne|oneToMany]", result[0].text)

    def test_search_output_renders_union_and_object_keys(self):
        manager = ToolManager()
        manager.tools_registry = {
            "storage_create_file": {
                "definition": make_tool(
                    "storage_create_file",
                    "Create a file.",
                    ["file"],
                    properties={
                        "file": {
                            "oneOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "url": {"type": "string"},
                                        "filename": {"type": "string"},
                                    },
                                },
                            ],
                            "description": "Binary file.",
                        },
                    },
                ),
            }
        }
        runtime = Operator(manager, lambda *_: [])

        result = runtime.execute_public_tool(
            "appwrite_search_tools",
            {"query": "create file", "include_mutating": True},
        )

        self.assertIn("file (string|object, required)", result[0].text)
        self.assertIn("object keys: url, filename", result[0].text)

    def test_search_drops_entries_sharing_no_query_token(self):
        manager = ToolManager()
        manager.tools_registry = {
            "tables_db_list": {
                "definition": make_tool("tables_db_list", "List all databases."),
            },
            "avatars_get_favicon": {
                "definition": make_tool("avatars_get_favicon", "Get a favicon."),
            },
        }
        runtime = Operator(manager, lambda *_: [])

        result = runtime.execute_public_tool(
            "appwrite_search_tools", {"query": "list databases"}
        )

        # An entry nothing in the query names is noise the model still has to
        # read and discard; loose substring overlap used to keep it in.
        self.assertIn("tables_db_list", result[0].text)
        self.assertNotIn("avatars_get_favicon", result[0].text)

    def test_search_matches_across_singular_and_plural(self):
        manager = ToolManager()
        manager.tools_registry = {
            "tables_db_list_rows": {
                "definition": make_tool("tables_db_list_rows", "List rows."),
            },
            "tables_db_get_row": {
                "definition": make_tool("tables_db_get_row", "Get a row."),
            },
            "avatars_get_favicon": {
                "definition": make_tool("avatars_get_favicon", "Get a favicon."),
            },
        }
        runtime = Operator(manager, lambda *_: [])

        for query in ("row", "rows"):
            result = runtime.execute_public_tool(
                "appwrite_search_tools", {"query": query}
            )
            # An inflection is a real match; requiring an exact token hid the
            # sibling tool entirely (searching "row" lost list_rows).
            self.assertIn("tables_db_list_rows", result[0].text, query)
            self.assertIn("tables_db_get_row", result[0].text, query)
            self.assertNotIn("avatars_get_favicon", result[0].text, query)

    def test_inflection_alone_is_enough_to_be_returned(self):
        manager = ToolManager()
        manager.tools_registry = {
            # No required params and a mutating verb, so the inflection match is
            # this entry's only source of score — the case a numeric relevance
            # floor on top of the match gate would silently discard.
            "tables_db_create_rows": {
                "definition": make_tool("tables_db_create_rows", "Create new rows."),
            },
        }
        runtime = Operator(manager, lambda *_: [])

        result = runtime.execute_public_tool(
            "appwrite_search_tools", {"query": "row", "include_mutating": True}
        )

        self.assertIn("tables_db_create_rows", result[0].text)

    def test_search_ranks_the_named_service_first(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])

        result = runtime.execute_public_tool(
            "appwrite_search_tools", {"query": "list databases"}
        )

        # Sibling list tools still match on the verb, but rank below the service
        # the query actually names, and a verb mismatch drops out entirely.
        self.assertLess(
            result[0].text.index("tables_db_list"), result[0].text.index("users_list")
        )
        self.assertNotIn("functions_get", result[0].text)

    def test_ambiguous_read_verb_does_not_bury_list_tools(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])

        result = runtime.execute_public_tool(
            "appwrite_search_tools", {"query": "read all databases"}
        )

        # "read" names neither get nor list; collapsing it onto get scored every
        # single-item tool above every list tool, so the list tool fell off the
        # page entirely. Resource tokens should decide instead.
        self.assertIn("tables_db_list", result[0].text)
        self.assertLess(
            result[0].text.index("tables_db_list"), result[0].text.index("users_list")
        )

    def test_ambiguous_read_verb_still_excludes_mutating_tools(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])

        result = runtime.execute_public_tool(
            "appwrite_search_tools", {"query": "read all databases"}
        )

        self.assertNotIn("tables_db_create", result[0].text)

    def test_unknown_tool_names_the_closest_matches(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])

        with self.assertRaises(ValueError) as caught:
            runtime.execute_public_tool(
                "appwrite_call_tool", {"tool_name": "tables_db_lst"}
            )

        # Naming near misses saves the search round trip a typo would cost.
        self.assertIn("tables_db_list", str(caught.exception))

    def test_unknown_tool_without_near_misses_points_at_search(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])

        with self.assertRaises(ValueError) as caught:
            runtime.execute_public_tool(
                "appwrite_call_tool", {"tool_name": "wholly_unrelated"}
            )

        self.assertIn("appwrite_search_tools", str(caught.exception))

    def test_catalog_carries_parameter_shapes(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])

        catalog = runtime.read_resource(CATALOG_URI)[0].content

        # Lets a client read the catalog once instead of searching per tool.
        self.assertIn('"parameters"', catalog)
        self.assertIn('"size": "number"', catalog)

    def test_catalog_and_search_surface_target_context(self):
        manager = ToolManager()
        manager.tools_registry = {
            "domains_list": {
                "definition": make_tool("domains_list", "List domains."),
                "context_scope": "organization",
            }
        }
        runtime = Operator(manager, lambda *_: [])

        result = runtime.execute_public_tool(
            "appwrite_search_tools", {"query": "list domains"}
        )
        self.assertIn("context=organization", result[0].text)

        catalog = runtime.read_resource(CATALOG_URI)[0].content
        self.assertIn('"context_scope": "organization"', catalog)

    def test_hosted_calls_require_the_catalog_target_context(self):
        manager = ToolManager()
        manager.tools_registry = {
            "tables_db_list": {
                "definition": make_tool("tables_db_list", "List databases."),
                "context_scope": "project",
            },
            "domains_list": {
                "definition": make_tool("domains_list", "List domains."),
                "context_scope": "organization",
            },
        }
        runtime = Operator(manager, lambda *_: [], require_target_context=True)

        with self.assertRaisesRegex(ValueError, "requires project_id"):
            runtime.execute_public_tool(
                "appwrite_call_tool", {"tool_name": "tables_db_list"}
            )
        with self.assertRaisesRegex(ValueError, "requires organization_id"):
            runtime.execute_public_tool(
                "appwrite_call_tool", {"tool_name": "domains_list"}
            )

    def test_stdio_calls_use_configured_project_without_target_argument(self):
        manager = ToolManager()
        manager.tools_registry = {
            "tables_db_list": {
                "definition": make_tool("tables_db_list", "List databases."),
                "context_scope": "project",
            }
        }
        runtime = Operator(manager, lambda *_: [], require_target_context=False)

        self.assertEqual(
            runtime.execute_public_tool(
                "appwrite_call_tool", {"tool_name": "tables_db_list"}
            ),
            [],
        )

    def test_get_context_dispatches_provider(self):
        runtime = Operator(
            ToolManager(),
            lambda name, arguments, *_: [],
            context_provider=lambda arguments: {
                "connection": {"mode": "api_key_project"},
                "projects": [{"$id": arguments["project_id"]}],
            },
        )

        result = runtime.execute_public_tool(
            "appwrite_get_context", {"project_id": "project-1"}
        )

        self.assertIn('"mode": "api_key_project"', result[0].text)
        self.assertIn('"$id": "project-1"', result[0].text)

    def test_get_context_returns_large_payload_inline(self):
        runtime = Operator(
            ToolManager(),
            lambda name, arguments, *_: [],
            context_provider=lambda arguments: {
                "connection": {"mode": "api_key_project"},
                "projects": [{"$id": "project-1", "description": "x" * 1200}],
            },
        )

        result = runtime.execute_public_tool("appwrite_get_context", {})

        self.assertNotIn("appwrite://operator/results/", result[0].text)
        self.assertIn("x" * 1200, result[0].text)

    def test_search_tools_infers_mutating_search_for_create_query(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])

        result = runtime.execute_public_tool(
            "appwrite_search_tools",
            {"query": "create function"},
        )

        self.assertEqual(len(result), 1)
        self.assertIn("functions_create", result[0].text)

    def test_search_tools_surfaces_required_create_tool_without_argument_hints(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])

        result = runtime.execute_public_tool(
            "appwrite_search_tools",
            {"query": "create string column"},
        )

        self.assertEqual(len(result), 1)
        self.assertIn("tables_db_create_string_column", result[0].text)

    def test_search_tools_includes_parameter_schemas(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])

        users_result = runtime.execute_public_tool(
            "appwrite_search_tools",
            {"query": "list users", "service_hints": "users"},
        )
        self.assertIn("users_list", users_result[0].text)
        self.assertIn("params:", users_result[0].text)
        self.assertIn(
            "queries (array[string], optional): Array of query strings generated",
            users_result[0].text,
        )

        create_result = runtime.execute_public_tool(
            "appwrite_search_tools",
            {"query": "create database", "service_hints": "tables_db"},
        )
        self.assertIn("tables_db_create", create_result[0].text)
        self.assertIn(
            "database_id (string, required): Unique Id. Choose a custom ID",
            create_result[0].text,
        )
        self.assertIn("name (string, required): Database name.", create_result[0].text)
        self.assertIn("enabled (boolean, optional)", create_result[0].text)

        column_result = runtime.execute_public_tool(
            "appwrite_search_tools",
            {"query": "create string column"},
        )
        self.assertIn("tables_db_create_string_column", column_result[0].text)
        self.assertIn(
            "size (number, required): Column size for text columns",
            column_result[0].text,
        )
        # Required params are listed before optional ones.
        size_pos = column_result[0].text.index("size (number, required)")
        default_pos = column_result[0].text.index("default (string, optional)")
        self.assertLess(size_pos, default_pos)

    def test_search_tools_scores_get_queries_against_get_tools(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])

        result = runtime.execute_public_tool(
            "appwrite_search_tools",
            {"query": "get function"},
        )

        self.assertEqual(len(result), 1)
        self.assertIn("functions_get", result[0].text)

    def test_call_tool_requires_confirm_write(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])

        with self.assertRaisesRegex(WriteConfirmationRequired, "confirm_write=true"):
            runtime.execute_public_tool(
                "appwrite_call_tool",
                {"tool_name": "tables_db_create", "arguments": {"database_id": "db"}},
            )

    def test_call_tool_merges_top_level_arguments(self):
        captured = {}

        def executor(name, arguments, *_):
            captured["name"] = name
            captured["arguments"] = arguments
            return [types.TextContent(type="text", text="ok")]

        runtime = self.make_runtime(executor)
        result = runtime.execute_public_tool(
            "appwrite_call_tool",
            {
                "tool_name": "tables_db_create",
                "confirm_write": True,
                "database_id": "db",
            },
        )

        self.assertEqual(captured["name"], "tables_db_create")
        self.assertEqual(captured["arguments"], {"database_id": "db"})
        self.assertEqual(result[0].text, "ok")

    def test_large_result_is_stored_as_resource(self):
        runtime = self.make_runtime(
            lambda name, arguments, *_: [
                types.TextContent(type="text", text=OVERSIZED_TEXT)
            ]
        )

        result = runtime.execute_public_tool(
            "appwrite_call_tool",
            {"tool_name": "tables_db_list"},
        )

        self.assertEqual(len(result), 1)
        self.assertIn("appwrite://operator/results/", result[0].text)

        resources = runtime.list_resources()
        result_resource = next(
            resource
            for resource in resources
            if str(resource.uri).startswith("appwrite://operator/results/")
        )
        contents = runtime.read_resource(str(result_resource.uri))
        self.assertEqual(contents[0].mime_type, "application/json")
        self.assertIn('"type": "text"', contents[0].content)

    def test_store_results_false_returns_large_result_inline(self):
        manager = ToolManager()
        manager.tools_registry = {
            "tables_db_list": {
                "definition": make_tool("tables_db_list", "List all databases."),
                "function": object(),
                "parameter_types": {},
            },
        }
        runtime = Operator(
            manager,
            lambda name, arguments, *_: [
                types.TextContent(type="text", text=OVERSIZED_TEXT)
            ],
            store_results=False,
        )

        result = runtime.execute_public_tool(
            "appwrite_call_tool",
            {"tool_name": "tables_db_list"},
        )

        self.assertEqual(result[0].text, OVERSIZED_TEXT)
        self.assertNotIn("appwrite://operator/results/", result[0].text)

    def test_store_results_false_returns_image_inline(self):
        manager = ToolManager()
        manager.tools_registry = {
            "avatars_get_qr": {
                "definition": make_tool("avatars_get_qr", "Get a QR code."),
                "function": object(),
                "parameter_types": {},
            },
        }
        runtime = Operator(
            manager,
            lambda name, arguments, *_: [
                types.ImageContent(type="image", data="aW1hZ2U=", mime_type="image/png")
            ],
            store_results=False,
        )

        result = runtime.execute_public_tool(
            "appwrite_call_tool",
            {"tool_name": "avatars_get_qr"},
        )

        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], types.ImageContent)
        self.assertEqual(result[0].mime_type, "image/png")


class ResultStoreTests(unittest.TestCase):
    def test_concurrent_save_and_list_are_thread_safe(self):
        store = ResultStore(max_size=50)
        content = [types.TextContent(type="text", text="ok")]

        def save_many():
            for index in range(500):
                store.save("tables_db_list", content, f"result {index}")

        def list_many():
            for _ in range(500):
                store.list()

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(save_many),
                executor.submit(save_many),
                executor.submit(list_many),
                executor.submit(list_many),
            ]
            for future in futures:
                future.result()

        self.assertLessEqual(len(store.list()), 50)


if __name__ == "__main__":
    unittest.main()
