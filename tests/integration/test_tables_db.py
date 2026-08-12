from support import LiveIntegrationTestCase, requires_live_integration


@requires_live_integration
class TablesDbIntegrationTests(LiveIntegrationTestCase):
    def test_tables_db_smoke(self):
        runner = self.new_runner()
        database_id = runner.unique_id("db")
        table_id = runner.unique_id("tbl")

        try:
            runner.call(
                "tables_db_create",
                {"database_id": database_id, "name": "MCP Smoke Database"},
            )
            runner.call(
                "tables_db_create_table",
                {"database_id": database_id, "table_id": table_id, "name": "Smoke"},
            )
            runner.call(
                "tables_db_create_string_column",
                {
                    "database_id": database_id,
                    "table_id": table_id,
                    "key": "name",
                    "size": 255,
                    "required": True,
                },
            )
            runner.wait_for_database_resource(
                lambda: runner.call(
                    "tables_db_get_column",
                    {"database_id": database_id, "table_id": table_id, "key": "name"},
                ),
                key="status",
            )
            runner.call(
                "tables_db_create_row",
                {
                    "database_id": database_id,
                    "table_id": table_id,
                    "row_id": "row1",
                    "data": {"name": "smoke"},
                },
            )
            runner.call(
                "tables_db_get_row",
                {"database_id": database_id, "table_id": table_id, "row_id": "row1"},
            )

            listed = runner.call(
                "tables_db_list_rows",
                {"database_id": database_id, "table_id": table_id},
            )
            self.assertEqual(listed["rows"][0]["$id"], "row1")
            self.assertEqual(listed["rows"][0]["data"]["name"], "smoke")
        finally:
            try:
                runner.call(
                    "tables_db_delete_table",
                    {"database_id": database_id, "table_id": table_id},
                )
            except Exception:
                pass
            try:
                runner.call("tables_db_delete", {"database_id": database_id})
            except Exception:
                pass

        self.assert_no_unexpected_errors(runner)
