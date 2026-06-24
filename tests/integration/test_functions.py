from appwrite.enums.runtime import Runtime
from support import LiveIntegrationTestCase, requires_live_integration


@requires_live_integration
class FunctionsIntegrationTests(LiveIntegrationTestCase):
    def test_functions_smoke(self):
        runner = self.new_runner()
        function_id = runner.unique_id("fn")
        variable_id: str | None = None

        try:
            runner.call("functions_list")
            runner.call_or_expect_error(
                "functions_list_runtimes", {}, 'missing scopes (["public"])'
            )
            runner.call(
                "functions_create",
                {
                    "function_id": function_id,
                    "name": "MCP Smoke Function",
                    "runtime": Runtime.NODE_22.value,
                    "entrypoint": "src/main.js",
                    "execute": ["any"],
                },
            )
            runner.call("functions_get", {"function_id": function_id})
            variable = runner.call(
                "functions_create_variable",
                {"function_id": function_id, "key": "GREETING", "value": "hello"},
            )
            variable_id = variable["$id"]
            runner.call(
                "functions_get_variable",
                {"function_id": function_id, "variable_id": variable_id},
            )
        finally:
            if variable_id:
                try:
                    runner.call(
                        "functions_delete_variable",
                        {"function_id": function_id, "variable_id": variable_id},
                    )
                except Exception:
                    pass
            try:
                runner.call("functions_delete", {"function_id": function_id})
            except Exception:
                pass

        self.assert_no_unexpected_errors(runner)
