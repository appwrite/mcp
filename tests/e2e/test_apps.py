"""MCP Apps through the real hosted server, as a host that renders them drives it.

The host lists tools, finds ``appwrite_show`` pointing at ``ui://appwrite/view``
and reads that resource. It then calls the tool for the model, and calls it
again (and the app-only ``appwrite_view_write``) for the view, which is how
the view pages, refreshes, follows a build and saves an edit.
:class:`support.Cloud` is the Appwrite API behind it, so every refusal is also
checked to have reached no Appwrite write at all.
"""

import json
import unittest
from typing import Any

from support import (
    APPS_SCOPES,
    SCOPES,
    TOKEN,
    Client,
    Cloud,
    Project,
    Server,
    column,
    deployment,
    row,
)

PROJECT = "6630f1a2b3c4d5e6f7a8"
DATABASE = "commerce"
TABLE = "orders"
FUNCTION = "checkout"
EXTENSION = "io.modelcontextprotocol/ui"
MIME_TYPE = "text/html;profile=mcp-app"
VIEW_URI = "ui://appwrite/view"
UI = {"extensions": {EXTENSION: {"mimeTypes": [MIME_TYPE]}}}
ROWS = 30
LOG_LINES = 250

cloud: Cloud


def setUpModule() -> None:
    global cloud
    cloud = Cloud()
    project = cloud.add(
        Project(PROJECT, tables={(DATABASE, TABLE)}, functions={FUNCTION})
    )
    project.columns[(DATABASE, TABLE)] = [
        column("customer", "string"),
        column("total", "double"),
        column("status", "string"),
        column("paid", "boolean"),
    ]
    project.rows[(DATABASE, TABLE)] = [
        row(
            f"ord_{index:03d}",
            DATABASE,
            TABLE,
            index,
            customer=f"Customer {index}",
            total=index * 1.5,
            status="paid",
            paid=True,
        )
        for index in range(1, ROWS + 1)
    ]
    project.deployments[FUNCTION] = [
        deployment("dep_old", FUNCTION, "ready", "done"),
        deployment(
            "dep_new",
            FUNCTION,
            "building",
            "\n".join(f"line {index}" for index in range(1, LOG_LINES + 1)),
            providerBranch="main",
            providerCommitHash="a91c3e0f00",
        ),
    ]
    cloud.grant(TOKEN, {PROJECT}, SCOPES | APPS_SCOPES)


def tearDownModule() -> None:
    cloud.close()


def tools(client: Client) -> dict[str, Any]:
    _, response = client.call("tools/list")
    return {tool["name"]: tool for tool in response["result"]["tools"]}


class AppsDisabledFlow(unittest.TestCase):
    """Without the flag nothing changes, even for a host that renders apps."""

    def test_nothing_is_offered(self):
        with Server({"APPWRITE_ENDPOINT": cloud.endpoint}) as server:
            with server.client(capabilities=UI) as client:
                listed = tools(client)
                self.assertNotIn("appwrite_show", listed)
                self.assertNotIn("appwrite_view_write", listed)

                _, response = client.call("server/discover")
                extensions = response["result"]["capabilities"].get("extensions", {})
                self.assertNotIn(EXTENSION, extensions)

                status, response = client.call("resources/read", {"uri": VIEW_URI})
                self.assertIn("error", response, status)

                result = client.call(
                    "tools/call", {"name": "appwrite_show", "arguments": {}}
                )[1]["result"]
                self.assertTrue(result["isError"])
                self.assertEqual(
                    result["content"][0]["text"], "Tool appwrite_show not found"
                )


class AppsFlow(unittest.TestCase):
    server: Server
    host: Client

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = Server(
            {"APPWRITE_ENDPOINT": cloud.endpoint, "MCP_APPS": "1"}
        ).__enter__()
        cls.host = cls.server.client(capabilities=UI)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.host.__exit__(None, None, None)
        cls.server.__exit__(None, None, None)

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        status, response = self.host.call(
            "tools/call", {"name": name, "arguments": arguments}
        )
        self.assertEqual(status, 200, response)
        return response["result"]

    def show_table(self, **arguments: Any) -> dict[str, Any]:
        result = self.call(
            "appwrite_show",
            {
                "view": "table",
                "project_id": PROJECT,
                "database_id": DATABASE,
                "table_id": TABLE,
                **arguments,
            },
        )
        self.assertFalse(result.get("isError"), result)
        return result

    def test_host_discovers_the_view(self):
        _, response = self.host.call("server/discover")
        extensions = response["result"]["capabilities"]["extensions"]
        self.assertEqual(extensions[EXTENSION], {"mimeTypes": [MIME_TYPE]})

        listed = tools(self.host)
        show = listed["appwrite_show"]
        self.assertEqual(
            show["_meta"]["ui"],
            {"resourceUri": VIEW_URI, "visibility": ["model", "app"]},
        )
        self.assertEqual(show["_meta"]["ui/resourceUri"], VIEW_URI)
        self.assertTrue(show["annotations"]["readOnlyHint"])
        write = listed["appwrite_view_write"]
        self.assertEqual(
            write["_meta"]["ui"], {"resourceUri": VIEW_URI, "visibility": ["app"]}
        )
        self.assertTrue(write["annotations"]["destructiveHint"])
        # The operator tools are unchanged.
        self.assertLessEqual(
            {"appwrite_call_tool", "appwrite_call_write_tool"}, set(listed)
        )

        _, response = self.host.call("resources/list")
        view = next(
            each for each in response["result"]["resources"] if each["uri"] == VIEW_URI
        )
        self.assertEqual(view["mimeType"], MIME_TYPE)

        _, response = self.host.call("resources/read", {"uri": VIEW_URI})
        (contents,) = response["result"]["contents"]
        self.assertEqual(contents["mimeType"], MIME_TYPE)
        # No CSP domains: the host's restrictive default, no network access.
        self.assertEqual(contents["_meta"]["ui"], {"csp": {}, "prefersBorder": True})
        html = contents["text"]
        self.assertTrue(html.startswith("<!doctype html>"))
        self.assertIn('"ui/initialize"', html)
        # Appwrite data reaches the DOM as text only.
        self.assertNotIn("innerHTML", html)
        self.assertNotIn("insertAdjacentHTML", html)

    def test_hosts_without_apps_are_not_offered_the_view(self):
        with self.server.client() as plain:
            listed = tools(plain)
            self.assertNotIn("appwrite_show", listed)
            self.assertNotIn("appwrite_view_write", listed)
            _, response = plain.call("resources/list")
            uris = {each["uri"] for each in response["result"]["resources"]}
            self.assertNotIn(VIEW_URI, uris)

        # A 2025-era client on the stateless transport lists tools without a
        # session, so its capabilities are unknown and the view is offered.
        with self.server.client() as legacy:
            status, response = legacy.legacy("tools/list")
            self.assertEqual(status, 200, response)
            names = {tool["name"] for tool in response["result"]["tools"]}
            self.assertIn("appwrite_show", names)

    def test_table_view_pages_through_rows(self):
        listed = cloud.calls("GET", rf"/v1/tablesdb/{DATABASE}/tables/{TABLE}/rows")
        result = self.show_table()
        data = result["structuredContent"]
        self.assertEqual(data["view"], "table")
        self.assertEqual(data["name"], TABLE)
        self.assertEqual(data["total"], ROWS)
        self.assertEqual(
            [each["key"] for each in data["columns"]],
            ["customer", "total", "status", "paid"],
        )
        # Newest first, one page.
        ids = [each["$id"] for each in data["rows"]]
        self.assertEqual(ids[0], f"ord_{ROWS:03d}")
        self.assertEqual(len(ids), 25)
        self.assertEqual(data["next"], ids[-1])

        text = result["content"][0]["text"]
        self.assertIn(f"Table {TABLE} ({DATABASE}/{TABLE}): {ROWS} rows", text)
        self.assertIn(f"ord_{ROWS:03d} | Customer {ROWS}", text)
        self.assertIn(f"cursor={data['next']}", text)

        second = self.show_table(cursor=data["next"])["structuredContent"]
        self.assertEqual(
            [each["$id"] for each in second["rows"]],
            [f"ord_{index:03d}" for index in range(5, 0, -1)],
        )
        self.assertIsNone(second["next"])
        self.assertEqual(
            cloud.calls("GET", rf"/v1/tablesdb/{DATABASE}/tables/{TABLE}/rows"),
            listed + 2,
        )

    def test_view_writes_only_its_allowlist_and_only_when_confirmed(self):
        row_path = rf"/v1/tablesdb/{DATABASE}/tables/{TABLE}/rows/ord_001"
        writes = cloud.calls("PATCH", row_path)
        update = {
            "tool_name": "tables_db_update_row",
            "project_id": PROJECT,
            "arguments": {
                "database_id": DATABASE,
                "table_id": TABLE,
                "row_id": "ord_001",
                "data": {"status": "refunded"},
            },
        }

        result = self.call("appwrite_view_write", {**update, "confirm_write": False})
        self.assertTrue(result["isError"])
        self.assertIn("confirm_write=true", result["content"][0]["text"])

        result = self.call(
            "appwrite_view_write",
            {
                **update,
                "tool_name": "tables_db_delete_row",
                "confirm_write": True,
            },
        )
        self.assertTrue(result["isError"])
        self.assertIn(
            "appwrite_view_write runs only tables_db_update_row",
            result["content"][0]["text"],
        )
        self.assertEqual(cloud.calls("DELETE", row_path), 0)
        self.assertEqual(cloud.calls("PATCH", row_path), writes)

        result = self.call("appwrite_view_write", {**update, "confirm_write": True})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(result["structuredContent"]["status"], "refunded")
        self.assertEqual(cloud.calls("PATCH", row_path), writes + 1)
        last = self.show_table(cursor="ord_002")["structuredContent"]["rows"]
        self.assertEqual(last[0]["$id"], "ord_001")
        self.assertEqual(last[0]["status"], "refunded")

    def test_deployment_view_follows_a_build_and_cancels_it(self):
        show = {"view": "deployment", "project_id": PROJECT, "function_id": FUNCTION}
        result = self.call("appwrite_show", show)
        self.assertFalse(result.get("isError"), result)
        data = result["structuredContent"]
        self.assertEqual(data["view"], "deployment")
        self.assertEqual(data["kind"], "function")
        self.assertEqual(data["resourceId"], FUNCTION)
        build = data["deployment"]
        self.assertEqual(build["id"], "dep_new")
        self.assertEqual(build["status"], "building")
        self.assertEqual(build["branch"], "main")
        self.assertFalse(data["done"])
        # Only the end of a long log is carried.
        self.assertEqual(len(build["logs"]), 200)
        self.assertEqual(build["logs"][-1], f"line {LOG_LINES}")
        self.assertTrue(build["truncated"])
        text = result["content"][0]["text"]
        self.assertIn(f"Deployment dep_new of function {FUNCTION} is building.", text)
        self.assertIn("Source: main @ a91c3e0.", text)
        self.assertTrue(text.endswith(f"line {LOG_LINES}"))

        status_path = rf"/v1/functions/{FUNCTION}/deployments/dep_new/status"
        result = self.call(
            "appwrite_view_write",
            {
                "tool_name": "functions_update_deployment_status",
                "project_id": PROJECT,
                "confirm_write": True,
                "arguments": {"function_id": FUNCTION, "deployment_id": "dep_new"},
            },
        )
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(cloud.calls("PATCH", status_path), 1)

        result = self.call("appwrite_show", {**show, "deployment_id": "dep_new"})
        data = result["structuredContent"]
        self.assertEqual(data["deployment"]["status"], "canceled")
        self.assertTrue(data["done"])

    def test_bad_requests_are_tool_errors(self):
        cases = [
            ({"view": "chart"}, "view must be one of: table, deployment"),
            (
                {"view": "table", "project_id": PROJECT, "table_id": TABLE},
                "database_id is required for view=table.",
            ),
            (
                {"view": "deployment", "project_id": PROJECT},
                "Pass exactly one of function_id or site_id.",
            ),
        ]
        for arguments, error in cases:
            with self.subTest(arguments=json.dumps(arguments)):
                result = self.call("appwrite_show", arguments)
                self.assertTrue(result["isError"])
                self.assertIn(error, result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
