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

from mcp_server_appwrite import server as server_module
from mcp_server_appwrite.server import (
    _coerce_argument,
    _configure_uploads,
    _format_tool_result,
    _prepare_arguments,
    _validate_service,
    build_client,
    build_instructions,
    build_operator,
    parse_args,
    register_services,
    validate_services,
)
from mcp_server_appwrite.tool_manager import ToolManager


class _FakeResponse:
    def __init__(self, *, data=b"", headers=None, url="https://example.com/pic.png"):
        self._data = data
        self.headers = headers or {}
        self.url = url

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        for index in range(0, len(self._data), 64):
            yield self._data[index : index + 64]


class _FakeStream:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self._response

    def __exit__(self, *args):
        return False


class _FakeClient:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def stream(self, method, url):
        return _FakeStream(self._response)


class ServerHelperTests(unittest.TestCase):
    def test_parse_args_defaults_to_stdio(self):
        with patch.dict(os.environ, {}, clear=True):
            args = parse_args([])

        self.assertEqual(args.transport, "stdio")
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 8000)

    def test_parse_args_accepts_env_transport(self):
        with patch.dict(os.environ, {"MCP_TRANSPORT": "http", "PORT": "9000"}):
            args = parse_args([])

        self.assertEqual(args.transport, "http")
        self.assertEqual(args.port, 9000)

    def test_parse_args_accepts_explicit_transport(self):
        with patch.dict(os.environ, {"MCP_TRANSPORT": "http"}):
            args = parse_args(["--transport", "stdio", "--host", "127.0.0.1"])

        self.assertEqual(args.transport, "stdio")
        self.assertEqual(args.host, "127.0.0.1")

    def test_parse_args_rejects_invalid_env_transport(self):
        with patch.dict(os.environ, {"MCP_TRANSPORT": "websocket"}):
            with self.assertRaises(SystemExit):
                parse_args([])

    def test_build_instructions_are_transport_specific(self):
        stdio = build_instructions("stdio")
        http = build_instructions("http")

        self.assertIn("APPWRITE_PROJECT_ID", stdio)
        self.assertNotIn("Appwrite console", stdio)
        self.assertIn("Appwrite console", http)
        self.assertIn("project_id", http)
        self.assertIn("Large results are stored as resources", stdio)
        self.assertIn("returns tool results inline", http)

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
        self.assertEqual(client.get_config("project"), "test-project")
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

    def test_validate_services_skips_unprobed_services(self):
        class SuccessfulSdkService:
            def list(self):
                return {"total": 0}

        manager = ToolManager()
        manager.services = [
            type(
                "UnprobedService",
                (),
                {
                    "service_name": "account",
                    "service": object(),
                },
            )(),
            type(
                "StubService",
                (),
                {
                    "service_name": "users",
                    "service": SuccessfulSdkService(),
                },
            )(),
        ]

        validate_services(manager)

    def test_build_operator_uses_explicit_stdio_client(self):
        tool = types.Tool(
            name="users_list",
            description="List users.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
        )
        manager = ToolManager()
        manager.tools_registry = {
            "users_list": {
                "definition": tool,
                "service_name": "users",
                "method_name": "list",
                "parameter_types": {},
            }
        }
        client = object()
        seen = {}

        def fake_execute(
            tools_manager,
            tool_name,
            tool_arguments,
            client=None,
            target_project=None,
            organization_id=None,
        ):
            seen["client"] = client
            seen["target_project"] = target_project
            seen["organization_id"] = organization_id
            return [types.TextContent(type="text", text="ok")]

        with patch("mcp_server_appwrite.server.execute_registered_tool", fake_execute):
            operator = build_operator(manager, client=client)
            result = operator.execute_public_tool(
                "appwrite_call_tool",
                {"tool_name": "users_list", "project_id": "ignored"},
            )

        self.assertEqual(result[0].text, "ok")
        self.assertIs(seen["client"], client)
        self.assertEqual(seen["target_project"], "ignored")

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


_PUBLIC_ADDRINFO = [(None, None, None, None, ("93.184.216.34", 80))]


class UploadInputFileTests(unittest.TestCase):
    """File-upload coercion: URL fetch, SSRF guard, size caps, transport gating."""

    def setUp(self):
        _configure_uploads("http")

    def tearDown(self):
        _configure_uploads("stdio")

    def _patch_fetch(self, response, addrinfo=_PUBLIC_ADDRINFO):
        return (
            patch(
                "mcp_server_appwrite.server.socket.getaddrinfo", return_value=addrinfo
            ),
            patch(
                "mcp_server_appwrite.server.httpx.Client",
                return_value=_FakeClient(response),
            ),
        )

    def test_url_object_uses_content_disposition_filename(self):
        response = _FakeResponse(
            data=b"\x89PNG\r\n",
            headers={
                "content-type": "image/png",
                "content-disposition": 'attachment; filename="pic.png"',
            },
        )
        addr, client = self._patch_fetch(response)
        with addr, client:
            coerced = _coerce_argument(
                "file", {"url": "https://example.com/x"}, InputFile
            )

        self.assertEqual(coerced.source_type, "bytes")
        self.assertEqual(coerced.data, b"\x89PNG\r\n")
        self.assertEqual(coerced.filename, "pic.png")
        self.assertEqual(coerced.mime_type, "image/png")

    def test_bare_url_string_derives_filename_from_path(self):
        response = _FakeResponse(data=b"abc", headers={"content-type": "image/png"})
        addr, client = self._patch_fetch(response)
        with addr, client:
            coerced = _coerce_argument(
                "file", "https://example.com/dir/a.png", InputFile
            )

        self.assertEqual(coerced.source_type, "bytes")
        self.assertEqual(coerced.filename, "a.png")

    def test_url_fetch_rejects_private_ip(self):
        response = _FakeResponse(data=b"secret")
        for ip in ("127.0.0.1", "169.254.169.254", "10.0.0.1"):
            with self.subTest(ip=ip):
                addr, client = self._patch_fetch(
                    response, addrinfo=[(None, None, None, None, (ip, 80))]
                )
                with addr, client as client_mock:
                    with self.assertRaises(ValueError) as ctx:
                        _coerce_argument(
                            "file", {"url": "https://evil.example/x"}, InputFile
                        )
                self.assertIn("private", str(ctx.exception).lower())
                client_mock.assert_not_called()

    def test_url_fetch_rejects_non_http_scheme(self):
        with self.assertRaises(ValueError) as ctx:
            _coerce_argument("file", {"url": "file:///etc/passwd"}, InputFile)
        self.assertIn("scheme", str(ctx.exception).lower())

    def test_url_fetch_size_cap_via_stream(self):
        response = _FakeResponse(data=b"0123456789")  # 10 bytes, no content-length
        addr, client = self._patch_fetch(response)
        with addr, client, patch.object(server_module, "_MAX_FETCH_BYTES", 4):
            with self.assertRaises(ValueError) as ctx:
                _coerce_argument("file", {"url": "https://example.com/x"}, InputFile)
        self.assertIn("max", str(ctx.exception).lower())

    def test_inline_content_size_cap(self):
        with patch.object(server_module, "_MAX_INLINE_BYTES", 4):
            with self.assertRaises(ValueError) as ctx:
                _coerce_argument(
                    "file",
                    {
                        "filename": "big.bin",
                        "content": base64.b64encode(b"hello").decode("ascii"),
                        "encoding": "base64",
                    },
                    InputFile,
                )
        self.assertIn("url", str(ctx.exception).lower())

    def test_path_string_rejected_on_http(self):
        with self.assertRaises(ValueError) as ctx:
            _coerce_argument("file", "/home/me/pic.png", InputFile)
        message = str(ctx.exception)
        self.assertIn("url", message.lower())
        self.assertNotIn("stdio", message.lower())
        self.assertNotIn("self-host", message.lower())

    def test_path_string_allowed_on_stdio(self):
        _configure_uploads("stdio")
        with tempfile.NamedTemporaryFile(suffix=".txt") as handle:
            coerced = _coerce_argument("file", handle.name, InputFile)
        self.assertEqual(coerced.source_type, "path")

    def test_http_instructions_mention_url_upload(self):
        http = build_instructions("http")
        stdio = build_instructions("stdio")
        self.assertIn("url", http.lower())
        self.assertIn("upload", http.lower())
        self.assertNotIn("upload", stdio.lower())


if __name__ == "__main__":
    unittest.main()
