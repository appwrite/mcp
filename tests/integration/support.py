from __future__ import annotations

import json
import struct
import tempfile
import time
import unittest
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp_server_appwrite.docs_search import DocsSearch
from mcp_server_appwrite.operator import Operator
from mcp_server_appwrite.server import (
    build_client,
    execute_registered_tool,
    register_services,
)


def has_live_appwrite_config() -> bool:
    try:
        build_client()
    except ValueError:
        return False
    return True


def should_run_live_integration() -> bool:
    return has_live_appwrite_config()


requires_live_integration = unittest.skipUnless(
    should_run_live_integration(),
    "Valid Appwrite credentials are required to run live integration tests.",
)


@dataclass
class ToolOutcome:
    detail: str
    outcome: str
    service: str
    tool_name: str


class LiveSurfaceRunner:
    def __init__(self):
        self.client = build_client()
        self.manager = register_services(self.client)
        docs_search = DocsSearch()
        self.docs_available = docs_search.available
        self.runtime = Operator(
            self.manager,
            lambda tool_name, arguments, *_: execute_registered_tool(
                self.manager,
                tool_name,
                arguments,
                self.client,
            ),
            docs_search=docs_search if self.docs_available else None,
        )
        self.tool_outcomes: dict[str, ToolOutcome] = {}
        self.public_outcomes: list[ToolOutcome] = []

    @property
    def expected_public_outcomes(self) -> int:
        """Number of public-surface calls run_public_smoke records."""
        return 4 if self.docs_available else 3

    def _invoke(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                return execute_registered_tool(
                    self.manager, tool_name, arguments or {}, self.client
                )
            except Exception as exc:  # pragma: no cover - exercised live
                message = str(exc).lower()
                transient = (
                    "connection reset by peer" in message
                    or "connection aborted" in message
                    or "remote end closed connection" in message
                )
                if not transient or attempt == 3:
                    raise
                last_exc = exc
                time.sleep(attempt)

        raise last_exc or RuntimeError(
            f"{tool_name} failed without raising a concrete exception"
        )

    def unique_id(self, prefix: str) -> str:
        return (prefix + uuid4().hex)[:36]

    def make_email(self, user_id: str) -> str:
        return f"{user_id}@example.test"

    def _service_name(self, tool_name: str) -> str:
        if tool_name.startswith("tables_db_"):
            return "tables_db"
        return tool_name.split("_", 1)[0]

    def _register(self, tool_name: str, outcome: str, detail: str) -> None:
        replacement = ToolOutcome(
            detail=detail,
            outcome=outcome,
            service=self._service_name(tool_name),
            tool_name=tool_name,
        )
        existing = self.tool_outcomes.get(tool_name)
        if existing is None or outcome == "success" or existing.outcome != "success":
            self.tool_outcomes[tool_name] = replacement

    def _register_public(self, tool_name: str, outcome: str, detail: str) -> None:
        self.public_outcomes.append(
            ToolOutcome(
                detail=detail,
                outcome=outcome,
                service="public",
                tool_name=tool_name,
            )
        )

    def _decode_result(self, content: Any) -> Any:
        if isinstance(content, list) and len(content) == 1:
            item = content[0]
            if item.type == "text":
                try:
                    return json.loads(item.text)
                except json.JSONDecodeError:
                    return item.text
        return content

    def _summarize_result(self, result: Any) -> str:
        if isinstance(result, dict):
            keys = ",".join(sorted(result.keys())[:6])
            return f"dict keys={keys}"
        if isinstance(result, list):
            if result and isinstance(result[0], dict):
                return f"list len={len(result)} first_keys={','.join(sorted(result[0].keys())[:6])}"
            return f"list len={len(result)}"
        return str(result)[:200]

    def call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any:
        try:
            content = self._invoke(tool_name, arguments)
        except Exception as exc:  # pragma: no cover - exercised live
            self._register(tool_name, "unexpected_error", str(exc))
            raise
        result = self._decode_result(content)
        self._register(tool_name, "success", self._summarize_result(result))
        return result

    def call_or_expect_error(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        matcher: str | list[str] | tuple[str, ...],
    ) -> Any:
        try:
            return self.call(tool_name, arguments)
        except Exception as exc:  # pragma: no cover - exercised live
            message = str(exc)
            matchers = [matcher] if isinstance(matcher, str) else list(matcher)
            if not any(item.lower() in message.lower() for item in matchers):
                raise
            self._register(tool_name, "expected_error", message)
            return None

    def wait_for_database_resource(
        self,
        getter,
        *,
        key: str,
        wanted: str = "available",
        timeout_seconds: float = 30.0,
    ) -> dict[str, Any]:
        deadline = time.time() + timeout_seconds
        while True:
            current = getter()
            if current.get(key) == wanted:
                return current
            if time.time() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for database resource status {wanted!r}: {current}"
                )
            time.sleep(1)

    def create_png_file(self) -> str:
        root = Path(tempfile.mkdtemp())
        path = root / "pixel.png"

        def chunk(chunk_type: bytes, data: bytes) -> bytes:
            return (
                struct.pack("!I", len(data))
                + chunk_type
                + data
                + struct.pack("!I", zlib.crc32(chunk_type + data) & 0xFFFFFFFF)
            )

        ihdr = struct.pack("!IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        raw_image = b"\x00\xff\x00\x00\xff"
        png_bytes = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw_image))
            + chunk(b"IEND", b"")
        )
        path.write_bytes(png_bytes)
        return str(path)

    def run_public_smoke(self) -> None:
        try:
            search = self.runtime.execute_public_tool(
                "appwrite_search_tools",
                {"query": "list users", "service_hints": ["users"]},
            )
            self._register_public(
                "appwrite_search_tools", "success", str(search[0].text[:200])
            )
        except Exception as exc:  # pragma: no cover - exercised live
            self._register_public("appwrite_search_tools", "unexpected_error", str(exc))
            raise

        try:
            call = self.runtime.execute_public_tool(
                "appwrite_call_tool",
                {"tool_name": "users_list"},
            )
            self._register_public(
                "appwrite_call_tool.users_list", "success", str(call[0])
            )
        except Exception as exc:  # pragma: no cover - exercised live
            self._register_public(
                "appwrite_call_tool.users_list", "unexpected_error", str(exc)
            )
            raise

        try:
            self.runtime.execute_public_tool(
                "appwrite_call_tool",
                {
                    "tool_name": "users_create",
                    "arguments": {"user_id": self.unique_id("u")},
                },
            )
        except Exception as exc:  # pragma: no cover - exercised live
            if "confirm_write=true" not in str(exc):
                self._register_public(
                    "appwrite_call_tool.confirm_write", "unexpected_error", str(exc)
                )
                raise
            self._register_public(
                "appwrite_call_tool.confirm_write", "expected_error", str(exc)
            )
        else:  # pragma: no cover - exercised live
            self._register_public(
                "appwrite_call_tool.confirm_write",
                "unexpected_error",
                "confirm_write guard did not fire",
            )
            raise RuntimeError(
                "confirm_write guard unexpectedly allowed a mutating call"
            )

        if not self.docs_available:
            return

        try:
            docs = self.runtime.execute_public_tool(
                "appwrite_search_docs",
                {"query": "create a database collection"},
            )
            self._register_public(
                "appwrite_search_docs", "success", str(docs[0].text[:200])
            )
        except Exception as exc:  # pragma: no cover - exercised live
            self._register_public("appwrite_search_docs", "unexpected_error", str(exc))
            raise


class LiveIntegrationTestCase(unittest.TestCase):
    def new_runner(self) -> LiveSurfaceRunner:
        return LiveSurfaceRunner()

    def assert_no_unexpected_errors(self, runner: LiveSurfaceRunner) -> None:
        unexpected_errors = {
            name: outcome.detail
            for name, outcome in runner.tool_outcomes.items()
            if outcome.outcome == "unexpected_error"
        }
        self.assertFalse(unexpected_errors, unexpected_errors)
