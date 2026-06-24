import unittest

from appwrite.client import Client

from mcp_server_appwrite.context import get_appwrite_context


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
        self.assertIn((None, None), seen)
        self.assertIn((None, "org-1"), seen)
        self.assertIn(("project-1", "org-1"), seen)


if __name__ == "__main__":
    unittest.main()
