"""The operator tools through the real hosted server, as a directory reviewer
drives them.

Anthropic's Connectors Directory and OpenAI's app review read every tool's
``title`` and hints from ``tools/list``, then call the tools to check that the
hints match what they do. Reads and mutations therefore run through separate
public tools: ``appwrite_call_tool`` is read-only and idempotent and refuses
anything else, ``appwrite_call_write_tool`` is destructive and refuses reads.
:class:`support.Cloud` is the Appwrite API both reach with the caller's token,
so a refused call is checked to have made no Appwrite request at all.
"""

import base64
import unittest
from typing import Any

from support import PNG, SCOPES, TOKEN, Client, Cloud, Project, Server, collector

PROJECT = "6630f1a2b3c4d5e6f7a8"
HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
READ = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
}
WRITE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
}
WEBHOOK = {
    "webhook_id": "e2e-tools",
    "name": "e2e tools",
    "url": "https://example.com/hook",
    "events": ["users.*.create"],
}

cloud: Cloud


def setUpModule() -> None:
    global cloud
    cloud = Cloud()
    cloud.add(Project(PROJECT, buckets={"photos"}))
    cloud.grant(TOKEN, {PROJECT}, SCOPES | {"files.read"})


def tearDownModule() -> None:
    cloud.close()


class ToolsFlow(unittest.TestCase):
    server: Server
    client: Client

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = Server({"APPWRITE_ENDPOINT": cloud.endpoint}).__enter__()
        cls.client = cls.server.client()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)
        cls.server.__exit__(None, None, None)

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        status, response = self.client.call(
            "tools/call", {"name": name, "arguments": arguments}
        )
        self.assertEqual(status, 200)
        return response["result"]

    def text(self, result: dict[str, Any]) -> str:
        return result["content"][0]["text"]

    def test_every_tool_has_a_title_and_explicit_hints(self):
        _, response = self.client.call("tools/list")
        tools = {tool["name"]: tool for tool in response["result"]["tools"]}

        self.assertLessEqual(
            {
                "appwrite_get_context",
                "appwrite_search_tools",
                "appwrite_call_tool",
                "appwrite_call_write_tool",
            },
            set(tools),
        )
        for name, tool in tools.items():
            with self.subTest(tool=name):
                annotations = tool["annotations"]
                self.assertTrue(tool["title"])
                self.assertEqual(annotations["title"], tool["title"])
                for hint in HINTS:
                    self.assertIsInstance(annotations[hint], bool, hint)

        for name in ("appwrite_get_context", "appwrite_search_tools"):
            self.assertLessEqual(READ.items(), tools[name]["annotations"].items())
        self.assertLessEqual(
            READ.items(), tools["appwrite_call_tool"]["annotations"].items()
        )
        self.assertLessEqual(
            WRITE.items(), tools["appwrite_call_write_tool"]["annotations"].items()
        )
        # Both forward caller-built requests, so review requires them to name
        # the API they call.
        for name in ("appwrite_call_tool", "appwrite_call_write_tool"):
            self.assertIn(
                "Appwrite REST API, see https://appwrite.io/docs/references",
                tools[name]["description"],
            )
        self.assertNotIn(
            "confirm_write", tools["appwrite_call_tool"]["inputSchema"]["properties"]
        )
        self.assertIn(
            "confirm_write",
            tools["appwrite_call_write_tool"]["inputSchema"]["properties"],
        )

    def test_search_names_the_call_tool_for_each_match(self):
        result = self.call(
            "appwrite_search_tools",
            {"query": "create webhook", "include_mutating": True},
        )
        text = self.text(result)

        self.assertIn(
            "tool=webhooks_create service=webhooks class=write "
            "call_tool=appwrite_call_write_tool",
            text,
        )

        result = self.call("appwrite_search_tools", {"query": "list webhooks"})
        self.assertIn(
            "tool=webhooks_list service=webhooks class=read "
            "call_tool=appwrite_call_tool",
            self.text(result),
        )

    def test_reads_and_writes_run_only_through_their_own_tool(self):
        wrong = collector().count("mcp.tool.errors", error_category="wrong_call_tool")
        listed = cloud.calls("GET", "/v1/webhooks")
        created = cloud.calls("POST", "/v1/webhooks")

        result = self.call(
            "appwrite_call_tool",
            {"tool_name": "webhooks_list", "project_id": PROJECT},
        )
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(cloud.calls("GET", "/v1/webhooks"), listed + 1)

        # A mutation through the read-only tool never reaches Appwrite, even
        # with confirm_write, and names the tool that runs it.
        result = self.call(
            "appwrite_call_tool",
            {
                "tool_name": "webhooks_create",
                "project_id": PROJECT,
                "confirm_write": True,
                "arguments": WEBHOOK,
            },
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            self.text(result),
            "Tool webhooks_create is write. Call it through "
            "appwrite_call_write_tool with confirm_write=true.",
        )

        # So does a read through the write tool.
        result = self.call(
            "appwrite_call_write_tool",
            {"tool_name": "webhooks_list", "project_id": PROJECT},
        )
        self.assertTrue(result["isError"])
        self.assertEqual(
            self.text(result),
            "Tool webhooks_list is read. Call it through appwrite_call_tool.",
        )
        self.assertEqual(cloud.calls("GET", "/v1/webhooks"), listed + 1)

        result = self.call(
            "appwrite_call_write_tool",
            {
                "tool_name": "webhooks_create",
                "project_id": PROJECT,
                "arguments": WEBHOOK,
            },
        )
        self.assertTrue(result["isError"])
        self.assertIn("confirm_write=true", self.text(result))
        self.assertEqual(cloud.calls("POST", "/v1/webhooks"), created)

        result = self.call(
            "appwrite_call_write_tool",
            {
                "tool_name": "webhooks_create",
                "project_id": PROJECT,
                "confirm_write": True,
                "arguments": WEBHOOK,
            },
        )
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(cloud.calls("POST", "/v1/webhooks"), created + 1)
        self.assertIn(WEBHOOK["webhook_id"], cloud.documents(PROJECT))

        self.assertEqual(
            collector().wait(
                "mcp.tool.errors", wrong + 2, error_category="wrong_call_tool"
            ),
            wrong + 2,
        )

    def test_binary_reads_reach_the_project(self):
        # The SDK sends the project header in the call's own headers too; sent
        # twice, Appwrite reads "id, id" and finds no such project.
        result = self.call(
            "appwrite_call_tool",
            {
                "tool_name": "storage_get_file_preview",
                "project_id": PROJECT,
                "arguments": {"bucket_id": "photos", "file_id": "sunrise", "width": 64},
            },
        )
        self.assertFalse(result.get("isError"), result)
        (image,) = result["content"]
        self.assertEqual(image["type"], "image")
        self.assertEqual(image["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(image["data"]), PNG)


if __name__ == "__main__":
    unittest.main()
