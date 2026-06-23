import unittest

import mcp.types as types

from mcp_server_appwrite.operator import CATALOG_URI, Operator
from mcp_server_appwrite.tool_manager import ToolManager


def make_tool(
    name: str, description: str, required: list[str] | None = None
) -> types.Tool:
    return types.Tool(
        name=name,
        description=description,
        inputSchema={
            "type": "object",
            "properties": {
                "parameter": {"type": "string"},
            },
            "required": required or [],
        },
    )


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
                    "tables_db_create", "Create a database.", ["database_id"]
                ),
                "function": object(),
                "parameter_types": {},
            },
            "functions_list": {
                "definition": make_tool("functions_list", "List all functions."),
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
                    ["database_id", "table_id", "key", "size"],
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

    def test_search_tools_returns_ranked_match(self):
        runtime = self.make_runtime(lambda name, arguments, *_: [])

        result = runtime.execute_public_tool(
            "appwrite_search_tools",
            {"query": "list databases"},
        )

        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], types.TextContent)
        self.assertIn("tables_db_list", result[0].text)
        self.assertIn(CATALOG_URI, result[0].text)

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

        with self.assertRaisesRegex(RuntimeError, "confirm_write=true"):
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
            lambda name, arguments, *_: [types.TextContent(type="text", text="x" * 1200)]
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


if __name__ == "__main__":
    unittest.main()
