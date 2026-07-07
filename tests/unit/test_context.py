import unittest

from appwrite.client import Client
from appwrite.exception import AppwriteException

from mcp_server_appwrite.context import get_appwrite_context
from mcp_server_appwrite.server import _get_context_for_request


class FakeClient(Client):
    def __init__(self, responses, *, endpoint="https://example.test/v1", project=""):
        super().__init__()
        self._responses = responses
        self._endpoint = endpoint
        self.set_project(project)
        self.calls = []

    def call(self, method, path="", headers=None, params=None, response_type="json"):
        self.calls.append((method, path, params or {}))
        response = self._responses.get((method, path))
        if callable(response):
            return response(params or {})
        if response is None:
            return {"total": 0}
        return response


class AppwriteContextTests(unittest.TestCase):
    def test_api_key_project_context_summarizes_configured_project(self):
        client = FakeClient(
            {
                ("get", "/project"): {
                    "$id": "project-1",
                    "name": "Project One",
                    "teamId": "team-1",
                },
                ("get", "/tablesdb"): {
                    "total": 1,
                    "databases": [{"$id": "db-1", "name": "Main"}],
                },
                ("get", "/users"): {"total": 2, "users": []},
            },
            project="project-1",
        )

        context = get_appwrite_context(client, mode="api_key_project")

        self.assertEqual(context["connection"]["mode"], "api_key_project")
        self.assertEqual(context["connection"]["projectId"], "project-1")
        self.assertNotIn("organizations", context)
        self.assertNotIn("account", context)
        self.assertEqual(context["projects"][0]["$id"], "project-1")
        self.assertEqual(context["projects"][0]["services"]["tablesdb"]["total"], 1)
        self.assertEqual(context["projects"][0]["services"]["users"]["total"], 2)
        self.assertNotIn("items", context["projects"][0]["services"]["tablesdb"])
        self.assertEqual(
            context["serviceSummary"],
            {"included": True, "detail": "totals", "projectCount": 1},
        )

    def test_oauth_context_discovers_organizations_projects_and_services(self):
        console = FakeClient(
            {
                ("get", "/account"): {
                    "$id": "user-1",
                    "name": "Ada",
                    "email": "ada@example.test",
                },
                ("get", "/organizations"): {
                    "total": 1,
                    "teams": [{"$id": "org-1", "name": "Org One"}],
                },
            }
        )
        org = FakeClient(
            {
                ("get", "/organization/projects"): {
                    "total": 1,
                    "projects": [
                        {
                            "$id": "project-1",
                            "name": "Project One",
                            "teamId": "org-1",
                        }
                    ],
                }
            }
        )
        project = FakeClient(
            {
                ("get", "/tablesdb"): {"total": 0, "databases": []},
                ("get", "/functions"): {
                    "total": 1,
                    "functions": [{"$id": "fn-1", "name": "Worker"}],
                },
            },
            project="project-1",
        )
        seen = []

        def factory(project_id, organization_id):
            seen.append((project_id, organization_id))
            if project_id:
                return project
            if organization_id:
                return org
            return console

        context = get_appwrite_context(
            console,
            mode="oauth_console",
            client_factory=factory,
            sample_limit=3,
        )

        self.assertEqual(context["account"]["email"], "ada@example.test")
        self.assertEqual(context["organizations"][0]["$id"], "org-1")
        self.assertEqual(context["projects"][0]["$id"], "project-1")
        self.assertEqual(context["projects"][0]["organizationId"], "org-1")
        self.assertEqual(context["projects"][0]["services"]["functions"]["total"], 1)
        self.assertNotIn("items", context["projects"][0]["services"]["functions"])
        self.assertEqual(
            context["serviceSummary"],
            {"included": True, "detail": "totals", "projectCount": 1},
        )
        self.assertIn((None, None), seen)
        self.assertIn((None, "org-1"), seen)
        self.assertIn(("project-1", "org-1"), seen)

    def test_oauth_context_falls_back_to_accessible_resources_when_narrowed(self):
        # A consent-narrowed grant (no console-wide `all`) cannot use the
        # console-tier /organizations and per-org project listings; discovery
        # must fall back to the always-granted OAuth2 accessible-resources
        # endpoints and still surface the bound organization and project.
        def denied(_params):
            raise AppwriteException("missing scopes", 401, "general_unauthorized_scope")

        console = FakeClient(
            {
                ("get", "/account"): denied,
                ("get", "/organizations"): denied,
                ("get", "/oauth2/console/organizations"): {
                    "total": 1,
                    "organizations": [{"$id": "org-1"}],
                },
                ("get", "/oauth2/console/projects"): {
                    "total": 1,
                    "projects": [{"$id": "project-a"}],
                },
            },
            project="console",
        )
        org = FakeClient({("get", "/organization/projects"): denied})
        project = FakeClient(
            {
                ("get", "/project"): {"$id": "project-a", "name": "Project A"},
                ("get", "/tablesdb"): {
                    "total": 1,
                    "databases": [{"$id": "db-1", "name": "Main"}],
                },
                ("get", "/storage/buckets"): denied,
            },
            project="project-a",
        )

        def factory(project_id, organization_id):
            if project_id:
                return project
            if organization_id:
                return org
            return console

        context = get_appwrite_context(
            console,
            mode="oauth_console",
            client_factory=factory,
        )

        self.assertIn("error", context["account"])
        self.assertEqual(context["organizations"], [{"$id": "org-1"}])
        self.assertEqual(context["projects"][0]["$id"], "project-a")
        self.assertEqual(context["projects"][0]["name"], "Project A")
        self.assertEqual(context["projects"][0]["services"]["tablesdb"]["total"], 1)
        # Ungranted probes surface their scope error instead of failing the call.
        self.assertIn("error", context["projects"][0]["services"]["storage"])

    def test_oauth_context_skips_service_fanout_for_large_project_sets(self):
        console = FakeClient(
            {
                ("get", "/organizations"): {
                    "total": 1,
                    "teams": [{"$id": "org-1", "name": "Org One"}],
                },
            }
        )
        org = FakeClient(
            {
                ("get", "/organization/projects"): {
                    "total": 6,
                    "projects": [
                        {
                            "$id": f"project-{index}",
                            "name": f"Project {index}",
                            "teamId": "org-1",
                        }
                        for index in range(6)
                    ],
                }
            }
        )
        project_clients = {}

        def factory(project_id, organization_id):
            if project_id:
                project_clients[project_id] = FakeClient({}, project=project_id)
                return project_clients[project_id]
            if organization_id:
                return org
            return console

        context = get_appwrite_context(
            console,
            mode="oauth_console",
            client_factory=factory,
        )

        self.assertEqual(len(context["projects"]), 6)
        self.assertNotIn("services", context["projects"][0])
        self.assertEqual(
            context["serviceSummary"],
            {
                "included": False,
                "reason": "project_count_exceeds_limit",
                "projectCount": 6,
                "maxProjects": 5,
                "hint": "Pass project_id to inspect services for a specific project.",
            },
        )
        self.assertEqual(project_clients, {})

    def test_oauth_context_includes_services_for_focused_project_in_large_org(self):
        console = FakeClient(
            {
                ("get", "/organizations"): {
                    "total": 1,
                    "teams": [{"$id": "org-1", "name": "Org One"}],
                },
            }
        )
        org = FakeClient(
            {
                ("get", "/organization/projects"): {
                    "total": 6,
                    "projects": [
                        {
                            "$id": f"project-{index}",
                            "name": f"Project {index}",
                            "teamId": "org-1",
                        }
                        for index in range(6)
                    ],
                }
            }
        )
        project = FakeClient(
            {
                ("get", "/tablesdb"): {"total": 1, "databases": []},
            },
            project="project-3",
        )

        def factory(project_id, organization_id):
            if project_id:
                return project
            if organization_id:
                return org
            return console

        context = get_appwrite_context(
            console,
            mode="oauth_console",
            client_factory=factory,
            project_id="project-3",
        )

        self.assertEqual(len(context["projects"]), 1)
        self.assertEqual(context["projects"][0]["$id"], "project-3")
        self.assertEqual(context["projects"][0]["services"]["tablesdb"]["total"], 1)
        self.assertEqual(
            context["serviceSummary"],
            {"included": True, "detail": "totals", "projectCount": 1},
        )

    def test_include_services_false_skips_service_summary(self):
        client = FakeClient(
            {
                ("get", "/project"): {"$id": "project-1", "name": "Project One"},
                ("get", "/functions"): {
                    "total": 1,
                    "functions": [{"$id": "fn-1", "name": "Worker"}],
                },
            },
            project="project-1",
        )

        context = get_appwrite_context(
            client,
            mode="api_key_project",
            include_services=False,
        )

        self.assertNotIn("services", context["projects"][0])
        self.assertEqual(
            context["serviceSummary"],
            {"included": False, "reason": "disabled", "projectCount": 1},
        )

    def test_compact_output_preserves_false_resource_state(self):
        client = FakeClient(
            {
                ("get", "/project"): {"$id": "project-1", "name": "Project One"},
                ("get", "/functions"): {
                    "total": 1,
                    "functions": [
                        {
                            "$id": "fn-1",
                            "name": "Disabled Function",
                            "enabled": False,
                        }
                    ],
                },
                ("get", "/users"): {
                    "total": 1,
                    "users": [
                        {
                            "$id": "user-1",
                            "email": "user@example.test",
                            "emailVerification": False,
                        }
                    ],
                },
            },
            project="project-1",
        )

        context = get_appwrite_context(
            client, mode="api_key_project", service_detail="samples"
        )
        services = context["projects"][0]["services"]

        self.assertIs(services["functions"]["items"][0]["enabled"], False)
        self.assertIs(services["users"]["items"][0]["emailVerification"], False)

    def test_service_probe_errors_are_returned_without_failing_context(self):
        def fail(_params):
            raise OSError("connection refused")

        client = FakeClient(
            {
                ("get", "/project"): {"$id": "project-1", "name": "Project One"},
                ("get", "/functions"): fail,
            },
            project="project-1",
        )

        context = get_appwrite_context(client, mode="api_key_project")

        self.assertEqual(context["projects"][0]["$id"], "project-1")
        self.assertEqual(
            context["projects"][0]["services"]["functions"]["error"],
            "connection refused",
        )

    def test_invalid_sample_limit_falls_back_to_default(self):
        client = FakeClient(
            {
                ("get", "/project"): {"$id": "project-1", "name": "Project One"},
            },
            project="project-1",
        )

        context = _get_context_for_request(
            {"sample_limit": "five", "service_detail": "samples"}, client
        )

        self.assertEqual(context["projects"][0]["$id"], "project-1")
        self.assertIn(
            ("get", "/tablesdb", {"queries": ['{"method":"limit","values":[5]}']}),
            client.calls,
        )

    def test_invalid_service_detail_falls_back_to_totals(self):
        client = FakeClient(
            {
                ("get", "/project"): {"$id": "project-1", "name": "Project One"},
                ("get", "/tablesdb"): {
                    "total": 1,
                    "databases": [{"$id": "db-1", "name": "Main"}],
                },
            },
            project="project-1",
        )

        context = _get_context_for_request({"service_detail": "verbose"}, client)

        self.assertEqual(
            context["serviceSummary"],
            {"included": True, "detail": "totals", "projectCount": 1},
        )
        self.assertNotIn("items", context["projects"][0]["services"]["tablesdb"])
        self.assertIn(
            ("get", "/tablesdb", {"queries": ['{"method":"limit","values":[1]}']}),
            client.calls,
        )


if __name__ == "__main__":
    unittest.main()
