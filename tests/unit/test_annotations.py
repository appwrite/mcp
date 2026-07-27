"""Unit tests for MCP ToolAnnotations on the public tool surface.

These tests verify the *shape* and *semantic consistency* of the
annotations on the 4 public tools exposed by Appwrite MCP
(see AGENTS.md §Tool surface, registered via `server.handle_list_tools`
which calls `Operator.get_public_tools()`):

- appwrite_get_context
- appwrite_search_tools
- appwrite_call_tool
- appwrite_search_docs

Annotations follow the MCP 2025-06-18 spec: `readOnlyHint`,
`destructiveHint`, `idempotentHint`, `openWorldHint`. The tests
guard against accidental regression (e.g. a future change that
silently flips a read-only tool to read-only=False, or that
leaves a hint unset when the spec requires explicit declaration).

Style: unittest (matches the rest of tests/unit/ and CI's
`python -m unittest discover` runner).
"""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from mcp_server_appwrite.docs_search import DocsSearch
from mcp_server_appwrite.operator import Operator
from mcp_server_appwrite.tool_manager import ToolManager


def _build_operator() -> Operator:
    manager = ToolManager()
    executor = ThreadPoolExecutor(max_workers=1)
    docs_search = DocsSearch()
    return Operator(manager, executor, docs_search=docs_search)


class PublicToolSurfaceTests(unittest.TestCase):
    """AGENTS.md §Tool surface promises 'up to 4' public tools.

    All 4 are registered through Operator.get_public_tools() (see
    server.handle_list_tools). If a 5th tool is added, the developer
    should also add an annotation test case for the new tool below.
    """

    def test_four_public_tools(self) -> None:
        tools = _build_operator().get_public_tools()
        names = sorted(t.name for t in tools)
        self.assertEqual(
            names,
            [
                "appwrite_call_tool",
                "appwrite_get_context",
                "appwrite_search_docs",
                "appwrite_search_tools",
            ],
        )

    def test_each_public_tool_has_distinct_name(self) -> None:
        tools = _build_operator().get_public_tools()
        names = [t.name for t in tools]
        self.assertEqual(
            len(names),
            len(set(names)),
            msg=f"Duplicate tool names in public surface: {names}",
        )


class ToolAnnotationShapeTests(unittest.TestCase):
    """Every public tool must declare all 4 MCP annotation hints explicitly.

    Per the MCP spec, leaving a hint unset means 'unknown', which forces
    conservative behavior in clients (they must prompt the user). We want
    every public tool to be unambiguous so clients can make informed
    decisions about auto-execution.
    """

    REQUIRED_HINTS = (
        "readOnlyHint",
        "destructiveHint",
        "idempotentHint",
        "openWorldHint",
    )

    def _all_public_tools(self):
        return _build_operator().get_public_tools()

    def test_every_public_tool_has_annotations(self) -> None:
        for tool in self._all_public_tools():
            self.assertIsNotNone(
                tool.annotations,
                msg=f"Tool {tool.name} has no annotations object",
            )

    def test_every_hint_is_explicit_bool(self) -> None:
        for tool in self._all_public_tools():
            ann = tool.annotations
            for hint in self.REQUIRED_HINTS:
                with self.subTest(tool=tool.name, hint=hint):
                    value = getattr(ann, hint, None)
                    self.assertIsInstance(
                        value,
                        bool,
                        msg=(
                            f"Tool {tool.name}.annotations.{hint} must be an "
                            f"explicit bool, got {value!r}"
                        ),
                    )

    def test_every_public_tool_has_human_readable_title(self) -> None:
        for tool in self._all_public_tools():
            ann = tool.annotations
            self.assertTrue(
                ann.title,
                msg=f"Tool {tool.name}.annotations.title is empty",
            )
            self.assertIsInstance(ann.title, str)
            self.assertGreaterEqual(
                len(ann.title),
                4,
                msg=f"Tool {tool.name}.annotations.title too short: {ann.title!r}",
            )


class ToolAnnotationSemanticTests(unittest.TestCase):
    """Cross-hint invariants that catch semantic regressions."""

    def _all_public_tools(self):
        return _build_operator().get_public_tools()

    def test_readonly_implies_not_destructive(self) -> None:
        for tool in self._all_public_tools():
            ann = tool.annotations
            if ann.readOnlyHint is True:
                with self.subTest(tool=tool.name):
                    self.assertFalse(
                        ann.destructiveHint,
                        msg=(
                            f"Tool {tool.name} declares readOnlyHint=True but "
                            f"destructiveHint={ann.destructiveHint}; "
                            f"these are mutually exclusive"
                        ),
                    )

    def test_readonly_implies_idempotent(self) -> None:
        for tool in self._all_public_tools():
            ann = tool.annotations
            if ann.readOnlyHint is True:
                with self.subTest(tool=tool.name):
                    self.assertTrue(
                        ann.idempotentHint,
                        msg=(
                            f"Tool {tool.name} declares readOnlyHint=True but "
                            f"idempotentHint={ann.idempotentHint}; read-only "
                            f"tools should be idempotent by default"
                        ),
                    )


class AppwriteCallToolAnnotationTests(unittest.TestCase):
    """appwrite_call_tool dispatches to mutating Appwrite SDK methods.

    Therefore:
    - readOnlyHint=False (it can mutate)
    - destructiveHint=False explicit (the runtime gate is confirm_write=true,
      not the MCP-level hint; setting it to default-True would mislead
      clients into requiring destructive-action confirmation even for reads)
    - idempotentHint=False (repeated calls can produce different results)
    - openWorldHint=True (dispatches to live Appwrite APIs)
    """

    def _get_call_tool(self):
        tools = _build_operator().get_public_tools()
        return next(t for t in tools if t.name == "appwrite_call_tool")

    def test_call_tool_is_not_readonly(self) -> None:
        ann = self._get_call_tool().annotations
        self.assertFalse(ann.readOnlyHint)

    def test_call_tool_destructive_hint_is_explicit_false(self) -> None:
        ann = self._get_call_tool().annotations
        self.assertFalse(
            ann.destructiveHint,
            msg=(
                "destructiveHint=False must be explicit on appwrite_call_tool; "
                "the runtime gate is confirm_write=true, not the MCP-level hint"
            ),
        )

    def test_call_tool_is_not_idempotent(self) -> None:
        ann = self._get_call_tool().annotations
        self.assertFalse(ann.idempotentHint)

    def test_call_tool_is_open_world(self) -> None:
        ann = self._get_call_tool().annotations
        self.assertTrue(ann.openWorldHint)


class LocalSearchToolAnnotationTests(unittest.TestCase):
    """appwrite_search_tools and appwrite_search_docs are local index searches.

    They should NOT claim openWorldHint=True — they don't make external
    API calls for the search itself. This protects against clients
    treating them as 'might call out to the internet' for permission
    purposes.
    """

    def _search_tools(self):
        return [t for t in _build_operator().get_public_tools() if "search" in t.name]

    def test_two_search_tools(self) -> None:
        tools = self._search_tools()
        names = sorted(t.name for t in tools)
        self.assertEqual(
            names,
            ["appwrite_search_docs", "appwrite_search_tools"],
            msg=f"Expected exactly 2 search tools in public surface, got {names}",
        )

    def test_search_tools_are_not_open_world(self) -> None:
        for tool in self._search_tools():
            with self.subTest(tool=tool.name):
                self.assertFalse(
                    tool.annotations.openWorldHint,
                    msg=(
                        f"Tool {tool.name} is a local index search but "
                        f"claims openWorldHint=True"
                    ),
                )


if __name__ == "__main__":
    unittest.main()
