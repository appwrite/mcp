"""The operator tools through the real hosted server, as a directory reviewer
drives them.

Anthropic's Connectors Directory and OpenAI's app review read every tool's
``title`` and hints from ``tools/list``, then call the tools to check that the
hints match what they do. Reads and mutations therefore run through separate
public tools: ``appwrite_call_tool`` is read-only and idempotent and refuses
anything else, ``appwrite_call_write_tool`` is destructive and refuses reads.
:class:`support.Cloud` is the Appwrite API both reach with the caller's token,
so a refused call is checked to have made no Appwrite request at all.

Writes give a ``reason``, which Appwrite records on the audit log from the
``X-Appwrite-Reason`` header, and any call can act in one of a project's
environments, which Appwrite reaches through ``X-Appwrite-Environment`` next to
the project's own ID.
"""

import base64
import hashlib
import json
import unittest
from typing import Any
from unittest import mock
from urllib.parse import unquote

import httpx
from support import (
    PNG,
    PUBLIC_URL,
    SCOPES,
    TOKEN,
    Client,
    Cloud,
    Project,
    Server,
    collector,
)

from mcp_server_appwrite import docs_search

PROJECT = "6630f1a2b3c4d5e6f7a8"
STAGING = Project("internal-staging", buckets={"photos"}, name="Staging")
"""The project's ``staging`` environment, a project whose own ID is internal."""
REASON = "Notify the café's Slack: 100% of sign-ups, ticket #42"
REASON_HEADER = (
    "Notify%20the%20caf%C3%A9's%20Slack%3A%20100%25%20of%20sign-ups%2C%20ticket%20%2342"
)
"""``REASON`` as ``encodeURIComponent`` sends it."""
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
    cloud.add(Project(PROJECT, buckets={"photos"}, environments={"staging": STAGING}))
    cloud.grant(TOKEN, {PROJECT}, SCOPES | {"files.read", "project.read"})


def tearDownModule() -> None:
    cloud.close()


class DocsSearchRoute(unittest.TestCase):
    """``POST /docs/search``, which the deprecated docs MCP server calls without a
    user. Only the OpenAI query embedding is stubbed; the index is the real one."""

    def setUp(self) -> None:
        patch = mock.patch.object(
            docs_search,
            "_default_embedder",
            lambda: lambda _query: [1.0] + [0.0] * 1535,
        )
        patch.start()
        self.addCleanup(patch.stop)

    def server(self) -> Server:
        return Server(
            {
                "APPWRITE_ENDPOINT": cloud.endpoint,
                "DOCS_SEARCH_MIN_SCORE": "-1",
                "OPENAI_API_KEY": "stubbed",
            }
        )

    def test_returns_a_fixed_number_of_pages_without_auth(self):
        with self.server() as server:
            found = httpx.post(
                f"{server.url}/docs/search",
                json={"query": "create a row", "limit": 10},
            )
            self.assertEqual(found.status_code, 200)
            results = found.json()["results"]
            self.assertTrue(1 <= len(results) <= 5)
            self.assertEqual(
                set(results[0]), {"path", "title", "description", "score", "content"}
            )

    def test_rejects_invalid_queries_before_embedding_them(self):
        with self.server() as server:
            for invalid in (
                {"query": "x"},
                {"query": None},
                {"query": "x" * 501},
                ["create a row"],
            ):
                refused = httpx.post(f"{server.url}/docs/search", json=invalid)
                self.assertEqual(refused.status_code, 400, invalid)

            malformed = httpx.post(f"{server.url}/docs/search", content="nope")
            self.assertEqual(malformed.status_code, 400)

            oversized = httpx.post(
                f"{server.url}/docs/search", content=b"x" * (16 * 1024 + 1)
            )
            self.assertEqual(oversized.status_code, 413)


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

    def test_server_card_describes_the_server_without_auth(self):
        for path in ("/.well-known/mcp/server-card.json", "/server-card"):
            response = httpx.get(f"{self.server.url}{path}")
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(response.headers["access-control-allow-origin"], "*")
            card = response.json()
            self.assertEqual(card["name"], "io.github.appwrite/mcp")
            self.assertEqual(card["title"], "Appwrite")
            self.assertLessEqual(len(card["description"]), 100)
            self.assertEqual(card["icons"][0]["src"], f"{PUBLIC_URL}/favicon.svg")
            self.assertEqual(card["remotes"][0]["url"], f"{PUBLIC_URL}/")
            self.assertIn("2025-11-25", card["remotes"][0]["supportedProtocolVersions"])

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
        # Every write says why; reads have nothing to explain.
        self.assertIn(
            "reason", tools["appwrite_call_write_tool"]["inputSchema"]["required"]
        )
        self.assertNotIn(
            "reason", tools["appwrite_call_tool"]["inputSchema"]["properties"]
        )
        for name in ("appwrite_call_tool", "appwrite_call_write_tool"):
            self.assertIn(
                "environment_id", tools[name]["inputSchema"]["properties"], name
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
                "reason": "Hook sign-ups into the CRM",
                "arguments": WEBHOOK,
            },
        )
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(cloud.calls("POST", "/v1/webhooks"), created + 1)
        signing = cloud.documents(PROJECT)[WEBHOOK["webhook_id"]]["signatureKey"]

        # The signing key Appwrite returns stays out of the model's context:
        # only a fingerprint of it, and a note on how to see it if asked.
        self.assertNotIn(signing, json.dumps(result["content"]))
        webhook = json.loads(self.text(result))
        self.assertEqual(webhook["secret"], "[redacted]")
        self.assertEqual(
            webhook["secretFingerprint"],
            "sha256:" + hashlib.sha256(signing.encode()).hexdigest()[:12],
        )
        self.assertIn(
            "Secrets redacted from this result: secret.", result["content"][1]["text"]
        )

        # Asked for, a rotated key comes back in full.
        result = self.call(
            "appwrite_call_write_tool",
            {
                "tool_name": "webhooks_update_secret",
                "project_id": PROJECT,
                "confirm_write": True,
                "reason": "Show the user the rotated signing key",
                "reveal_secrets": True,
                "arguments": {"webhook_id": WEBHOOK["webhook_id"]},
            },
        )
        self.assertFalse(result.get("isError"), result)
        signing = cloud.documents(PROJECT)[WEBHOOK["webhook_id"]]["signatureKey"]
        self.assertEqual(json.loads(self.text(result))["secret"], signing)
        self.assertEqual(len(result["content"]), 1)

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

    def test_writes_give_the_reason_appwrite_records(self):
        refused = collector().count(
            "mcp.tool.errors", error_category="write_confirmation"
        )
        created = cloud.calls("POST", "/v1/webhooks")
        write = {
            "tool_name": "webhooks_create",
            "project_id": PROJECT,
            "confirm_write": True,
            "arguments": {**WEBHOOK, "webhook_id": "e2e-reason"},
        }

        # What Appwrite would refuse never reaches it, and the model is told
        # how to fix it.
        for reason, message in (
            (None, "Re-run appwrite_call_write_tool with reason set to why"),
            ("   ", "Re-run appwrite_call_write_tool with reason set to why"),
            ("x" * 257, "at most 256 characters; it has 257"),
            ("Rotate\nthe hook", "one line of text without control characters"),
            ("Rotate \u202ethe hook", "one line of text without control characters"),
        ):
            with self.subTest(reason=reason):
                arguments = write if reason is None else {**write, "reason": reason}
                result = self.call("appwrite_call_write_tool", arguments)
                self.assertTrue(result["isError"])
                self.assertIn(message, self.text(result))
        self.assertEqual(cloud.calls("POST", "/v1/webhooks"), created)

        # Surrounding whitespace is dropped; the rest is sent percent-encoded,
        # spaces as %20, which Appwrite decodes back.
        result = self.call(
            "appwrite_call_write_tool", {**write, "reason": f"  {REASON}\n"}
        )
        self.assertFalse(result.get("isError"), result)
        sent = cloud.headers("POST", "/v1/webhooks")[-1]["x-appwrite-reason"]
        self.assertEqual(sent, REASON_HEADER)
        self.assertEqual(unquote(sent), REASON)
        # Classified like a missing confirm_write: a model mistake, not an outage.
        self.assertEqual(
            collector().wait(
                "mcp.tool.errors", refused + 5, error_category="write_confirmation"
            ),
            refused + 5,
        )

        # A read sends no reason, even when given one.
        result = self.call(
            "appwrite_call_tool",
            {"tool_name": "webhooks_list", "project_id": PROJECT, "reason": REASON},
        )
        self.assertFalse(result.get("isError"), result)
        self.assertNotIn("x-appwrite-reason", cloud.headers("GET", "/v1/webhooks")[-1])

    def test_calls_act_in_a_project_environment(self):
        listed = cloud.calls("GET", "/v1/webhooks")
        result = self.call(
            "appwrite_call_tool",
            {"tool_name": "webhooks_list", "environment_id": "staging"},
        )
        self.assertTrue(result["isError"])
        self.assertIn("environment_id requires project_id", self.text(result))
        self.assertEqual(cloud.calls("GET", "/v1/webhooks"), listed)

        result = self.call(
            "appwrite_call_write_tool",
            {
                "tool_name": "webhooks_create",
                "project_id": PROJECT,
                "environment_id": "staging",
                "confirm_write": True,
                "reason": "Mirror the sign-up hook in staging",
                "arguments": {**WEBHOOK, "webhook_id": "e2e-staging"},
            },
        )
        self.assertFalse(result.get("isError"), result)
        headers = cloud.headers("POST", "/v1/webhooks")[-1]
        # The environment is reached through its project's ID, never its own.
        self.assertEqual(headers["x-appwrite-project"], PROJECT)
        self.assertEqual(headers["x-appwrite-environment"], "staging")
        self.assertIn("e2e-staging", STAGING.webhooks)
        self.assertNotIn("e2e-staging", cloud.documents(PROJECT))

        # Binary reads take their own request path.
        result = self.call(
            "appwrite_call_tool",
            {
                "tool_name": "storage_get_file_preview",
                "project_id": PROJECT,
                "environment_id": "staging",
                "arguments": {"bucket_id": "photos", "file_id": "sunrise"},
            },
        )
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(result["content"][0]["type"], "image")
        preview = cloud.headers(
            "GET", "/v1/storage/buckets/photos/files/sunrise/preview"
        )
        self.assertEqual(preview[-1]["x-appwrite-environment"], "staging")
        # Environments live in their project's region, so only the project's
        # region is ever looked up.
        self.assertEqual(
            cloud.calls("GET", "/v1/projects/(staging|internal-staging)"), 0
        )

        result = self.call(
            "appwrite_call_tool",
            {
                "tool_name": "webhooks_list",
                "project_id": PROJECT,
                "environment_id": "production",
            },
        )
        self.assertTrue(result["isError"])
        self.assertIn("project_environment_not_found", self.text(result))

        result = self.call(
            "appwrite_call_tool", {"tool_name": "webhooks_list", "project_id": PROJECT}
        )
        self.assertFalse(result.get("isError"), result)
        self.assertNotIn(
            "x-appwrite-environment", cloud.headers("GET", "/v1/webhooks")[-1]
        )

    def test_context_lists_and_summarizes_environments(self):
        result = self.call(
            "appwrite_get_context", {"project_id": PROJECT, "include_services": False}
        )
        (project,) = json.loads(self.text(result))["projects"]
        self.assertEqual(
            project["environments"], [{"$id": "staging", "name": "Staging"}]
        )
        self.assertNotIn("environmentId", project)

        result = self.call(
            "appwrite_get_context",
            {
                "project_id": PROJECT,
                "environment_id": "staging",
                "include_services": False,
            },
        )
        (project,) = json.loads(self.text(result))["projects"]
        self.assertEqual(project["environmentId"], "staging")
        self.assertEqual(
            cloud.headers("GET", "/v1/project")[-1]["x-appwrite-environment"], "staging"
        )

        result = self.call("appwrite_get_context", {"environment_id": "staging"})
        self.assertTrue(result["isError"])
        self.assertIn("environment_id requires project_id", self.text(result))


if __name__ == "__main__":
    unittest.main()
