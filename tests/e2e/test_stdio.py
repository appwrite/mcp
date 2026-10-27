"""The self-hosted stdio server, launched the way an MCP client launches it.

The real ``mcp-server-appwrite`` entry point runs as a subprocess and is driven
with the MCP SDK's stdio client, against :class:`support.Cloud` as the Appwrite
API reached with a project API key.

Plugins and client configs written for older releases still pass per-service
flags such as ``--users``. They used to make the server exit before the
handshake; it must start, ignore them, and tell the agent its launch config is
outdated where the agent reads it (see ``legacy.py``).
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from typing import Any

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from support import Cloud, Project

from mcp_server_appwrite.legacy import ISSUES_URL

PROJECT = "stdio-project"
KEY = "standard_stdio_key"


class StdioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cloud = Cloud()
        self.addCleanup(self.cloud.close)
        self.cloud.add(Project(PROJECT))
        self.cloud.grant(KEY, [PROJECT])
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def launch(self, *args: str) -> dict[str, Any]:
        """Start the server with ``args``, initialize, and call
        ``appwrite_get_context`` the way an agent's first call does."""
        environment = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("APPWRITE_", "MCP_", "OPENAI_", "OTEL_"))
        }
        environment |= {
            "APPWRITE_ENDPOINT": self.cloud.endpoint,
            "APPWRITE_PROJECT_ID": PROJECT,
            "APPWRITE_API_KEY": KEY,
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


if __name__ == "__main__":
    unittest.main()
