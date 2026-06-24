from appwrite.enums.build_runtime import BuildRuntime
from appwrite.enums.framework import Framework
from support import LiveIntegrationTestCase, requires_live_integration


@requires_live_integration
class SitesIntegrationTests(LiveIntegrationTestCase):
    def test_sites_smoke(self):
        runner = self.new_runner()
        site_id = runner.unique_id("site")
        variable_id: str | None = None

        try:
            runner.call("sites_list")
            runner.call_or_expect_error(
                "sites_list_frameworks", {}, 'missing scopes (["public"])'
            )
            runner.call(
                "sites_create",
                {
                    "site_id": site_id,
                    "name": "MCP Smoke Site",
                    "framework": Framework.OTHER.value,
                    "build_runtime": BuildRuntime.STATIC_1.value,
                },
            )
            runner.call("sites_get", {"site_id": site_id})
            variable = runner.call(
                "sites_create_variable",
                {"site_id": site_id, "key": "SITE_ENV", "value": "smoke"},
            )
            variable_id = variable["$id"]
            runner.call(
                "sites_get_variable", {"site_id": site_id, "variable_id": variable_id}
            )
        finally:
            if variable_id:
                try:
                    runner.call(
                        "sites_delete_variable",
                        {"site_id": site_id, "variable_id": variable_id},
                    )
                except Exception:
                    pass
            try:
                runner.call("sites_delete", {"site_id": site_id})
            except Exception:
                pass

        self.assert_no_unexpected_errors(runner)
