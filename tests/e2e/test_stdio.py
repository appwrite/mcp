"""The self-hosted stdio server, launched the way an MCP client launches it.

The real ``mcp-server-appwrite`` entry point runs as a subprocess and is driven
with the MCP SDK's stdio client, against :class:`support.Cloud` as the Appwrite
API reached with a project API key.

Plugins and client configs written for older releases still pass per-service
flags such as ``--users``. They used to make the server exit before the
handshake; it must start, ignore them, and tell the agent its launch config is
outdated where the agent reads it (see ``legacy.py``).

``APPWRITE_ENVIRONMENT_ID`` points the server at one of the project's
environments, and a write's ``reason`` goes with that write alone, although
every call shares one client.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from collections.abc import Awaitable, Callable
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from support import SCOPES, Cloud, Project, user

from mcp_server_appwrite.legacy import ISSUES_URL

PROJECT = "stdio-project"
KEY = "standard_stdio_key"


class StdioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cloud = Cloud()
        self.addCleanup(self.cloud.close)
        self.staging = Project(
            "internal-staging", users=[user("lin", "Lin Tester", "lin@staging.example")]
        )
        self.cloud.add(Project(PROJECT, environments={"staging": self.staging}))
        self.cloud.grant(KEY, [PROJECT], SCOPES | {"users.write"})
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def launch(self, *args: str) -> dict[str, Any]:
        """Start the server with ``args``, initialize, and call
        ``appwrite_get_context`` the way an agent's first call does."""

        async def first_call(client: ClientSession) -> dict[str, Any]:
            initialized = await client.initialize()
            tools = await client.list_tools()
            result = await client.call_tool(
                "appwrite_get_context", {"include_services": False}
            )
            return {
                "instructions": initialized.instructions or "",
                "tools": [tool.name for tool in tools.tools],
                "context": json.loads(result.content[0].text),
            }

        return self.run_session(first_call, args)

    def run_session(
        self,
        script: Callable[[ClientSession], Awaitable[dict[str, Any]]],
        args: tuple[str, ...] = (),
        variables: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Start the server with ``args`` and ``variables`` and run ``script``
        against it as the MCP client."""
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("APPWRITE_", "MCP_", "OPENAI_", "OTEL_"))
        }
        environment |= {
            "APPWRITE_ENDPOINT": self.cloud.endpoint,
            "APPWRITE_PROJECT_ID": PROJECT,
            "APPWRITE_API_KEY": KEY,
            **(variables or {}),
        }
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "mcp_server_appwrite", *args],
            env=environment,
            # Away from the repo, so no local .env is loaded.
            cwd=self.directory.name,
        )

        async def session() -> dict[str, Any]:
            with open(os.devnull, "w") as stderr:
                async with stdio_client(parameters, errlog=stderr) as streams:
                    async with ClientSession(*streams) as client:
                        return await script(client)

        return asyncio.run(asyncio.wait_for(session(), timeout=60))

    def test_starts_with_legacy_service_flags_and_tells_the_agent(self):
        session = self.launch("--users", "--databases", "--users")

        self.assertIn("appwrite_call_tool", session["tools"])
        for text in (session["instructions"], session["context"]["notice"]):
            self.assertIn("deprecated flags (--users --databases)", text)
            self.assertIn(ISSUES_URL, text)
        # Leads the instructions, so clients that truncate them still show it.
        self.assertTrue(session["instructions"].startswith("This Appwrite MCP"))
        self.assertEqual(next(iter(session["context"])), "notice")
        self.assertGreater(self.cloud.calls("GET", "/v1/users"), 0)

    def test_starts_without_a_notice_when_launched_cleanly(self):
        session = self.launch()

        self.assertIn("appwrite_call_tool", session["tools"])
        self.assertIn("appwrite_call_write_tool", session["tools"])
        self.assertNotIn("deprecated flags", session["instructions"])
        self.assertNotIn("notice", session["context"])

    def test_acts_in_the_configured_environment(self):
        block = {
            "tool_name": "users_update_status",
            "confirm_write": True,
            "arguments": {"user_id": "lin", "status": False},
        }

        async def script(client: ClientSession) -> dict[str, Any]:
            await client.initialize()
            context = await client.call_tool(
                "appwrite_get_context", {"include_services": False}
            )
            other = await client.call_tool(
                "appwrite_call_write_tool",
                {**block, "environment_id": "qa", "reason": "Block the test user"},
            )
            written = await client.call_tool(
                "appwrite_call_write_tool", {**block, "reason": "Block the test user"}
            )
            read = await client.call_tool(
                "appwrite_call_tool", {"tool_name": "users_list"}
            )
            return {
                "context": json.loads(context.content[0].text),
                "other": other,
                "written": written,
                "read": read,
            }

        session = self.run_session(
            script, variables={"APPWRITE_ENVIRONMENT_ID": "staging"}
        )

        (project,) = session["context"]["projects"]
        self.assertEqual(project["environmentId"], "staging")
        # An environment has no environments of its own to list.
        self.assertNotIn("environments", project)

        # A call meant for another environment is refused, not run here.
        self.assertTrue(session["other"].is_error)
        self.assertIn("APPWRITE_ENVIRONMENT_ID=qa", session["other"].content[0].text)

        self.assertFalse(session["written"].is_error, session["written"])
        self.assertFalse(self.staging.users[0]["status"])
        (patched,) = self.cloud.headers("PATCH", "/v1/users/lin/status")
        self.assertEqual(patched["x-appwrite-project"], PROJECT)
        self.assertEqual(patched["x-appwrite-environment"], "staging")
        self.assertEqual(patched["x-appwrite-reason"], "Block%20the%20test%20user")

        # Every request is in the environment; the reason went with its write alone.
        self.assertFalse(session["read"].is_error, session["read"])
        # Password hashes never reach the model, only their fingerprints.
        listed_users = session["read"].content[0].text
        self.assertNotIn("$argon2", listed_users)
        for each in json.loads(listed_users)["users"]:
            self.assertEqual(each["password"], "[redacted]")
            self.assertTrue(each["passwordFingerprint"].startswith("sha256:"))
        listed = self.cloud.headers("GET", "/v1/users")
        self.assertTrue(listed)
        for headers in listed:
            self.assertEqual(headers["x-appwrite-environment"], "staging")
        self.assertNotIn("x-appwrite-reason", listed[-1])


if __name__ == "__main__":
    unittest.main()
