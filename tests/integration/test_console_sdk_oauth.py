"""Read-only live coverage for every service added by appwrite-console.

These tests use a console OAuth token and are skipped unless the explicit OAuth
environment is present. Embeddings are billable and require a separate opt-in.
"""

from __future__ import annotations

import os
import unittest

from mcp_server_appwrite.catalog_policy import OAUTH_PROFILE
from mcp_server_appwrite.constants import DEFAULT_ENDPOINT
from mcp_server_appwrite.server import (
    _lookup_project_region,
    build_client_for_request,
    build_introspection_client,
    execute_registered_tool,
    register_services,
    resolve_region_endpoint,
)

TOKEN = os.getenv("APPWRITE_OAUTH_ACCESS_TOKEN")
ORGANIZATION_ID = os.getenv("APPWRITE_OAUTH_ORGANIZATION_ID")
PROJECT_ID = os.getenv("APPWRITE_OAUTH_PROJECT_ID")


@unittest.skipUnless(
    TOKEN and ORGANIZATION_ID and PROJECT_ID,
    "Console OAuth token, organization ID, and project ID are required.",
)
class ConsoleSdkOAuthIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        assert TOKEN is not None
        assert ORGANIZATION_ID is not None
        assert PROJECT_ID is not None

        cls.manager = register_services(
            build_introspection_client(), profile=OAUTH_PROFILE
        )
        base_endpoint = os.getenv("APPWRITE_ENDPOINT", DEFAULT_ENDPOINT)
        console_project_id = os.getenv("APPWRITE_CONSOLE_PROJECT_ID", "console")
        cls.console_client = build_client_for_request(
            console_project_id, TOKEN, endpoint=base_endpoint
        )
        cls.organization_client = build_client_for_request(
            console_project_id,
            TOKEN,
            endpoint=base_endpoint,
            organization_id=ORGANIZATION_ID,
        )

        project_endpoint = os.getenv("APPWRITE_OAUTH_PROJECT_ENDPOINT")
        if not project_endpoint:
            region = _lookup_project_region(console_project_id, TOKEN, PROJECT_ID)
            project_endpoint = resolve_region_endpoint(base_endpoint, region)
        cls.project_client = build_client_for_request(
            console_project_id,
            TOKEN,
            endpoint=project_endpoint,
            target_project=PROJECT_ID,
        )

    def _call(self, tool_name: str, arguments: dict | None, client) -> None:
        result = execute_registered_tool(
            self.manager, tool_name, arguments or {}, client=client
        )
        self.assertTrue(result, tool_name)

    def test_new_console_services_are_usable(self):
        probes = (
            ("console_list_regions", {}, self.console_client),
            ("organizations_list", {}, self.console_client),
            ("notifications_list", {}, self.console_client),
            (
                "projects_list_stages",
                {"project_id": PROJECT_ID},
                self.console_client,
            ),
            ("domains_list", {}, self.organization_client),
            ("documents_db_list", {}, self.project_client),
            ("migrations_list", {}, self.project_client),
            ("mongo_list", {}, self.project_client),
            ("mysql_list", {}, self.project_client),
            ("postgresql_list", {}, self.project_client),
            (
                "usage_list_events",
                {"metrics": ["executions"], "limit": 1},
                self.project_client,
            ),
            ("vcs_list_installations", {}, self.project_client),
            ("vectors_db_list", {}, self.project_client),
            ("waf_list_rules", {}, self.project_client),
        )

        for tool_name, arguments, client in probes:
            with self.subTest(tool_name=tool_name):
                self._call(tool_name, arguments, client)

    @unittest.skipUnless(
        os.getenv("APPWRITE_TEST_BILLABLE_EMBEDDINGS") == "1",
        "Set APPWRITE_TEST_BILLABLE_EMBEDDINGS=1 to run the billable probe.",
    )
    def test_embeddings_are_usable_when_billable_probe_is_enabled(self):
        self._call(
            "embeddings_create_text_embeddings",
            {"texts": ["Appwrite MCP integration probe"]},
            self.project_client,
        )


if __name__ == "__main__":
    unittest.main()
