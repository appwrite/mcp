import base64
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp.types as types
from appwrite.enums.browser import Browser
from appwrite.input_file import InputFile

from mcp_server_appwrite.server import (
    _coerce_argument,
    _format_tool_result,
    _prepare_arguments,
    _validate_service,
    build_client,
    parse_args,
    register_services,
    validate_services,
)
from mcp_server_appwrite.tool_manager import ToolManager


class ServerHelperTests(unittest.TestCase):
    def test_coerce_input_file_from_path(self):
        with tempfile.NamedTemporaryFile(suffix=".txt") as handle:
            coerced = _coerce_argument("file", handle.name, InputFile)

        self.assertIsInstance(coerced, InputFile)
        self.assertEqual(coerced.source_type, "path")

    def test_coerce_input_file_from_inline_content(self):
        coerced = _coerce_argument(
            "file",
            {
                "filename": "hello.txt",
                "content": base64.b64encode(b"hello").decode("ascii"),
                "encoding": "base64",
                "mime_type": "text/plain",
            },
            InputFile,
        )

        self.assertEqual(coerced.source_type, "bytes")
        self.assertEqual(coerced.data, b"hello")
        self.assertEqual(coerced.filename, "hello.txt")

    def test_build_client_loads_dotenv_from_current_working_directory(self):
        previous_cwd = Path.cwd()
        with (
            tempfile.TemporaryDirectory() as tmpdir,
            patch.dict(os.environ, {}, clear=True),
        ):
            tmp_path = Path(tmpdir)
            (tmp_path / ".env").write_text(
                "APPWRITE_PROJECT_ID=test-project\n"
                "APPWRITE_API_KEY=test-key\n"
                "APPWRITE_ENDPOINT=https://example.test/v1\n"
            )
            os.chdir(tmp_path)
            try:
                client = build_client()
            finally:
                os.chdir(previous_cwd)

        self.assertEqual(client._endpoint, "https://example.test/v1")
        self.assertEqual(client._global_headers["x-appwrite-project"], "test-project")
        self.assertEqual(client._global_headers["x-appwrite-key"], "test-key")

    def test_coerce_enum_returns_raw_value_string(self):
        self.assertEqual(_coerce_argument("code", "ch", Browser), "ch")
        self.assertEqual(_coerce_argument("code", Browser.GOOGLE_CHROME, Browser), "ch")

    def test_prepare_arguments_accepts_camel_case_aliases(self):
        tool_info = {
            "parameter_types": {
                "database_id": str,
                "table_id": str,
                "row_security": bool,
                "file_security": bool,
                "maximum_file_size": int,
            }
        }

        prepared = _prepare_arguments(
            tool_info,
            {
                "databaseId": "main",
                "tableId": "posts",
                "rowSecurity": True,
                "fileSecurity": False,
                "maximumFileSize": 10_485_760,
            },
        )

        self.assertEqual(
            prepared,
            {
                "database_id": "main",
                "table_id": "posts",
                "row_security": True,
                "file_security": False,
                "maximum_file_size": 10_485_760,
            },
        )

    def test_prepare_arguments_accepts_appwrite_response_style_keys(self):
        tool_info = {
            "parameter_types": {
                "bucket_id": str,
                "permissions": list[str],
                "file_security": bool,
            }
        }

        prepared = _prepare_arguments(
            tool_info,
            {
                "$id": "bucket-123",
                "$permissions": ['read("any")'],
                "fileSecurity": True,
            },
        )

        self.assertEqual(
            prepared,
            {
                "bucket_id": "bucket-123",
                "permissions": ['read("any")'],
                "file_security": True,
            },
        )

    def test_prepare_arguments_rejects_conflicting_alias_values(self):
        tool_info = {
            "parameter_types": {
                "row_security": bool,
            }
        }

        with self.assertRaisesRegex(
            ValueError, "Conflicting values provided for 'row_security'"
        ):
            _prepare_arguments(
                tool_info,
                {
                    "row_security": True,
                    "rowSecurity": False,
                },
            )

    def test_prepare_arguments_rejects_unsupported_copied_response_fields(self):
        tool_info = {
            "parameter_types": {
                "bucket_id": str,
                "permissions": list[str],
            }
        }

        with self.assertRaisesRegex(
            ValueError,
            "Unsupported arguments for storage_update_bucket: maximumFileSize",
        ):
            _prepare_arguments(
                {
                    **tool_info,
                    "definition": types.Tool(
                        name="storage_update_bucket",
                        description="Update a bucket.",
                        inputSchema={
                            "type": "object",
                            "properties": {
                                "bucket_id": {"type": "string"},
                                "permissions": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                        },
                    ),
                },
                {
                    "bucketId": "bucket-123",
                    "maximumFileSize": 10_485_760,
                },
            )

    def test_format_tool_result_serializes_json(self):
        result = _format_tool_result(
            "tables_db_list_rows", {"total": 1, "rows": []}, {}
        )

        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], types.TextContent)
        self.assertIn('"total": 1', result[0].text)

    def test_format_tool_result_returns_binary_resource(self):
        result = _format_tool_result("storage_get_file_download", b"plain-bytes", {})

        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], types.EmbeddedResource)
        self.assertEqual(result[0].resource.mimeType, "application/octet-stream")

    def test_register_services_returns_fresh_manager(self):
        manager_a = register_services(object())
        manager_b = register_services(object())

        self.assertIsNot(manager_a, manager_b)
        self.assertEqual(len(manager_a.get_all_tools()), len(manager_b.get_all_tools()))
        from mcp_server_appwrite.server import SERVICE_CLASSES

        self.assertEqual(
            {service.service_name for service in manager_a.services},
            set(SERVICE_CLASSES),
        )
        # Every advertised service is registered (the SDK currently ships 14).
        self.assertGreaterEqual(len(manager_a.services), 14)

    def test_validate_services_raises_with_service_name(self):
        class FailingSdkService:
            def list(self):
                raise Exception("boom")

        manager = ToolManager()
        manager.services = [
            type(
                "StubService",
                (),
                {
                    "service_name": "tables_db",
                    "service": FailingSdkService(),
                },
            )()
        ]

        with self.assertRaisesRegex(RuntimeError, "tables_db: boom"):
            validate_services(manager)

    def test_validate_services_accepts_successful_probe(self):
        class SuccessfulSdkService:
            def list(self):
                return {"total": 0}

        manager = ToolManager()
        manager.services = [
            type(
                "StubService",
                (),
                {
                    "service_name": "tables_db",
                    "service": SuccessfulSdkService(),
                },
            )()
        ]

        validate_services(manager)

    def test_validate_services_logs_progress(self):
        class SuccessfulSdkService:
            def list(self):
                return {"total": 0}

        manager = ToolManager()
        manager.services = [
            type(
                "StubService",
                (),
                {
                    "service_name": "tables_db",
                    "service": SuccessfulSdkService(),
                },
            )()
        ]

        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            validate_services(manager)

        output = stderr.getvalue()
        self.assertIn("Validating startup access via tables_db", output)
        self.assertIn("Validated startup access via tables_db", output)

    def test_validate_services_only_probes_first_registered_service(self):
        calls = []

        class FirstService:
            def list(self):
                calls.append("first")
                return {"total": 0}

        class SecondService:
            def list(self):
                calls.append("second")
                return {"total": 0}

        manager = ToolManager()
        manager.services = [
            type(
                "StubService",
                (),
                {"service_name": "tables_db", "service": FirstService()},
            )(),
            type(
                "StubService", (), {"service_name": "users", "service": SecondService()}
            )(),
        ]

        validate_services(manager)

        self.assertEqual(calls, ["first"])

    def test_validate_service_avatars_uses_raw_browser_code(self):
        captured = {}

        class AvatarService:
            def get_browser(self, code, width=None, height=None):
                captured["code"] = code
                captured["width"] = width
                captured["height"] = height
                return b"ok"

        service = type(
            "StubService",
            (),
            {
                "service_name": "avatars",
                "service": AvatarService(),
            },
        )()

        _validate_service(service)

        self.assertEqual(captured["code"], "ch")
        self.assertEqual(captured["width"], 1)
        self.assertEqual(captured["height"], 1)

    def test_parse_args_rejects_removed_flags(self):
        with (
            patch.object(sys, "argv", ["mcp-server-appwrite", "--users"]),
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            with self.assertRaises(SystemExit):
                parse_args()


if __name__ == "__main__":
    unittest.main()
