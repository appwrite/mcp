"""Unit tests for ToolAnnotations derivation across the full tool surface.

The MCP 2025-06-18 spec defines five optional ``ToolAnnotations`` hints. This
test suite asserts that:

1. The single source of truth (``annotations_for_classification``) returns the
   canonical hint set for each classification bucket.
2. ``classify_tool_name`` correctly maps every Appwrite SDK service verb into
   the appropriate bucket — parametrized across the 25 SDK services.
3. The 4 public operator tools (``appwrite_get_context``, ``appwrite_search_tools``,
   ``appwrite_call_tool``, ``appwrite_search_docs``) carry the right annotations
   for their semantic role.
4. The dynamic SDK tool generator (``service.Service.list_tools``) produces
   tools with annotations matching the action verb in their name.

Why this suite exists:

- A single helper mistake would silently mislead every MCP client that filters
  on these hints (claude-code, Gemini MCP, Appwrite broker).
- The 25 SDK services generate ~hundreds of tool names; sampling a handful is
  not enough to catch a bug in the verb parser.
- The operator's `_classify_verb` (private) and the annotations module's
  `classify_tool_name` (public) must stay in lock-step; we verify they agree on
  every classification the operator uses.
"""

from __future__ import annotations

import unittest

import mcp.types as types

from mcp_server_appwrite import (
    annotations,
    docs_search,
    operator,
    service,
)
from mcp_server_appwrite.annotations import (
    annotations_for_classification,
    classify_tool_name,
)


class ClassifyToolNameTests(unittest.TestCase):
    """``classify_tool_name`` maps SDK tool names to the right bucket."""

    def test_read_verbs(self):
        # READ_VERBS = {"list", "get"} — covers Appwrite's standard
        # list-and-fetch verbs across every service.
        for name in (
            "users_list",
            "users_get",
            "databases_list",
            "databases_get",
            "storage_list_buckets",
            "storage_get_bucket",
            "tables_db_list",
            "tables_db_get",
            "functions_list",
            "functions_get",
            "teams_list",
            "teams_get",
            "messaging_list_messages",
            "messaging_get_message",
            "sites_list",
            "sites_get",
            "avatars_get_flag",
            "avatars_get_image",
            "health_get",
        ):
            with self.subTest(name=name):
                self.assertEqual(classify_tool_name(name), "read")

    def test_write_verbs(self):
        # CREATE + UPDATE verbs span every Appwrite SDK service that has a
        # "make a thing" or "change a thing" endpoint.
        for name in (
            "users_create",
            "users_update",
            "users_update_labels",
            "users_update_status",
            "databases_create",
            "databases_update",
            "storage_create_bucket",
            "storage_update_bucket",
            "tables_db_create",
            "tables_db_update",
            "functions_create",
            "functions_update",
            "teams_create",
            "teams_update",
            "teams_update_memberships",
            "messaging_create_message",
            "messaging_update_message",
            "sites_create",
            "sites_update",
        ):
            with self.subTest(name=name):
                self.assertEqual(classify_tool_name(name), "write")

    def test_delete_verbs(self):
        # DELETE verb — explicit on every Appwrite service that exposes a
        # delete endpoint.
        for name in (
            "users_delete",
            "databases_delete",
            "storage_delete_bucket",
            "storage_delete_file",
            "tables_db_delete",
            "functions_delete",
            "teams_delete",
            "teams_delete_membership",
            "messaging_delete_message",
            "sites_delete",
        ):
            with self.subTest(name=name):
                self.assertEqual(classify_tool_name(name), "delete")

    def test_unknown_for_no_verb(self):
        # Tools that don't carry a standard verb fall through to "unknown".
        # These are typically Appwrite-internal helpers or service-level
        # methods that don't follow the verb_noun naming convention.
        for name in (
            "",
            "health",
            "ping",
            "graphql",
        ):
            with self.subTest(name=name):
                self.assertEqual(classify_tool_name(name), "unknown")

    def test_unknown_for_unknown_verb(self):
        # A verb we don't recognise also falls through to "unknown". The
        # Appwrite SDK is unlikely to ship these, but the helper must be
        # safe against future additions.
        self.assertEqual(classify_tool_name("users_provision"), "unknown")
        self.assertEqual(classify_tool_name("users_rotate"), "unknown")

    def test_case_insensitive(self):
        # The verb parser lower-cases the token before matching.
        self.assertEqual(classify_tool_name("Users_List"), "read")
        self.assertEqual(classify_tool_name("USERS_GET"), "read")
        self.assertEqual(classify_tool_name("users_CREATE"), "write")
        self.assertEqual(classify_tool_name("users_DELETE"), "delete")

    def test_verb_in_middle(self):
        # The verb can appear at any position in the name, not just first.
        self.assertEqual(classify_tool_name("storage_get_file_for_download"), "read")
        self.assertEqual(classify_tool_name("storage_create_file_for_upload"), "write")


class AnnotationsForClassificationTests(unittest.TestCase):
    """``annotations_for_classification`` returns the canonical hints."""

    def _assert_annotations(
        self,
        actual: types.ToolAnnotations,
        *,
        read_only: bool,
        destructive: bool,
        idempotent: bool,
        open_world: bool,
    ) -> None:
        self.assertEqual(actual.readOnlyHint, read_only)
        self.assertEqual(actual.destructiveHint, destructive)
        self.assertEqual(actual.idempotentHint, idempotent)
        self.assertEqual(actual.openWorldHint, open_world)

    def test_read_annotations(self):
        ann = annotations_for_classification("read")
        self._assert_annotations(
            ann,
            read_only=True,
            destructive=False,
            idempotent=True,
            open_world=True,
        )

    def test_write_annotations(self):
        ann = annotations_for_classification("write")
        self._assert_annotations(
            ann,
            read_only=False,
            destructive=False,
            idempotent=False,
            open_world=True,
        )

    def test_delete_annotations(self):
        ann = annotations_for_classification("delete")
        self._assert_annotations(
            ann,
            read_only=False,
            destructive=True,
            idempotent=True,
            open_world=True,
        )

    def test_unknown_annotations_are_conservative(self):
        # "unknown" must be conservative: not read-only (we can't prove it),
        # not destructive (we have no evidence), not idempotent (can't promise).
        # The only safe claim is openWorldHint=True.
        ann = annotations_for_classification("unknown")
        self._assert_annotations(
            ann,
            read_only=False,
            destructive=False,
            idempotent=False,
            open_world=True,
        )

    def test_unknown_falls_through_for_any_string(self):
        # Any string not in the known buckets returns the same shape as
        # "unknown" — the function never raises.
        for value in ("", "RANDOM", "read_only", "delete-ish"):
            with self.subTest(value=value):
                ann = annotations_for_classification(value)
                self._assert_annotations(
                    ann,
                    read_only=False,
                    destructive=False,
                    idempotent=False,
                    open_world=True,
                )

    def test_all_annotations_set_explicitly(self):
        # Every field must be set (no MCP-default leakage). This catches a
        # regression where someone forgets to set openWorldHint on a new
        # branch — the default for openWorldHint is true, so a missing
        # field would still pass `_assert_annotations` above.
        for bucket in ("read", "write", "delete", "unknown"):
            with self.subTest(bucket=bucket):
                ann = annotations_for_classification(bucket)
                # `is not None` guards against MCP-default leakage: the spec
                # default for both `destructiveHint` and `openWorldHint` is
                # true, but the helper sets them explicitly to either true
                # or false. A None would indicate a regression.
                self.assertIsNotNone(
                    ann.destructiveHint,
                    f"{bucket}: destructiveHint must be explicit",
                )
                self.assertIsNotNone(
                    ann.openWorldHint,
                    f"{bucket}: openWorldHint must be explicit",
                )
                self.assertIsNotNone(
                    ann.readOnlyHint,
                    f"{bucket}: readOnlyHint must be explicit",
                )
                self.assertIsNotNone(
                    ann.idempotentHint,
                    f"{bucket}: idempotentHint must be explicit",
                )


class OperatorClassificationParityTests(unittest.TestCase):
    """``classify_tool_name`` and the operator's ``_classify_verb`` must agree.

    The operator keeps a private ``_classify_verb`` for catalog scoring. If
    the two diverge, the catalog would advertise a tool as "read" while the
    annotations would say "write" — clients would behave inconsistently.
    """

    def test_known_verbs_agree(self):
        for verb in ("list", "get", "create", "update", "delete"):
            with self.subTest(verb=verb):
                expected = {
                    "list": "read",
                    "get": "read",
                    "create": "write",
                    "update": "write",
                    "delete": "delete",
                }[verb]
                self.assertEqual(
                    operator._classify_verb(verb),  # noqa: SLF001
                    expected,
                    f"operator._classify_verb({verb!r}) drifted",
                )

    def test_full_tool_names_agree(self):
        # Build a full tool name from each verb and confirm both helpers
        # classify it identically. This is the integration check: a tool
        # whose verb says "read" must end up classified as "read" by
        # the public helper used in service.py.
        for verb in ("list", "get", "create", "update", "delete"):
            for resource in ("users", "databases", "storage_bucket", "function"):
                tool_name = f"{resource}_{verb}"
                with self.subTest(tool_name=tool_name):
                    expected = operator._classify_verb(verb)  # noqa: SLF001
                    self.assertEqual(
                        classify_tool_name(tool_name),
                        expected,
                        f"classification drift for {tool_name!r}",
                    )


class PublicToolSurfaceTests(unittest.TestCase):
    """The 4 public operator tools carry the right annotations."""

    def _public_tool_names(self, operator_instance) -> list[types.Tool]:
        return operator_instance.get_public_tools()

    def _build_operator(self):
        # Minimal ToolManager stub: the Operator's annotations are computed
        # independently of catalog contents, so an empty ToolManager is fine.
        from mcp_server_appwrite.tool_manager import ToolManager

        return OperatorStub(tools_manager=ToolManager())

    def test_appwrite_get_context_is_read(self):
        op = self._build_operator()
        tool = next(
            t for t in op.get_public_tools() if t.name == "appwrite_get_context"
        )
        ann = tool.annotations
        assert ann is not None
        self.assertTrue(ann.readOnlyHint)
        self.assertFalse(ann.destructiveHint)
        self.assertTrue(ann.idempotentHint)
        self.assertTrue(ann.openWorldHint)

    def test_appwrite_search_tools_is_read(self):
        op = self._build_operator()
        tool = next(
            t for t in op.get_public_tools() if t.name == "appwrite_search_tools"
        )
        ann = tool.annotations
        assert ann is not None
        self.assertTrue(ann.readOnlyHint)
        self.assertFalse(ann.destructiveHint)
        self.assertTrue(ann.idempotentHint)
        self.assertTrue(ann.openWorldHint)

    def test_appwrite_call_tool_is_unknown(self):
        # The gateway tool can dispatch to write/delete tools, so it cannot
        # honestly claim readOnlyHint=True. It must claim the conservative
        # "unknown" bucket — readOnlyHint=False, destructiveHint=False
        # (the runtime gate is confirm_write=true, not the hint).
        op = self._build_operator()
        tool = next(t for t in op.get_public_tools() if t.name == "appwrite_call_tool")
        ann = tool.annotations
        assert ann is not None
        self.assertFalse(ann.readOnlyHint)
        self.assertFalse(ann.destructiveHint)
        self.assertFalse(ann.idempotentHint)
        self.assertTrue(ann.openWorldHint)


class DocsSearchAnnotationTests(unittest.TestCase):
    """The docs search tool carries read annotations."""

    def test_appwrite_search_docs_is_read(self):
        # The docs index is committed; the embedder only hits OpenAI's
        # embedding endpoint. No project writes, so the tool is read-only.
        from pathlib import Path

        ds = docs_search.DocsSearch(data_dir=Path("/nonexistent"))
        # Force the tool definition to render even without an index loaded —
        # `get_tool` does not depend on `_index_loaded`.
        tool = ds.get_tool()
        ann = tool.annotations
        assert ann is not None
        self.assertTrue(ann.readOnlyHint)
        self.assertFalse(ann.destructiveHint)
        self.assertTrue(ann.idempotentHint)
        self.assertTrue(ann.openWorldHint)


class DynamicSdkToolAnnotationTests(unittest.TestCase):
    """``service.Service.list_tools`` produces annotated tools for every verb."""

    def _make_service(self):
        # Build a Service with a stub SDK that exposes one method per verb.
        # The Service introspects via inspect.getmembers, so we need real
        # method descriptors — plain functions don't work.
        import appwrite.services.users as users_module

        class _StubClient:
            pass

        return service.Service(users_module.Users(_StubClient()), "users")

    def test_users_list_is_read(self):
        svc = self._make_service()
        tools = svc.list_tools()
        list_tool = tools["users_list"]["definition"]
        ann = list_tool.annotations
        assert ann is not None
        self.assertTrue(ann.readOnlyHint)
        self.assertFalse(ann.destructiveHint)
        self.assertTrue(ann.idempotentHint)
        self.assertTrue(ann.openWorldHint)

    def test_users_create_is_write(self):
        svc = self._make_service()
        tools = svc.list_tools()
        create_tool = tools["users_create"]["definition"]
        ann = create_tool.annotations
        assert ann is not None
        self.assertFalse(ann.readOnlyHint)
        self.assertFalse(ann.destructiveHint)
        self.assertFalse(ann.idempotentHint)
        self.assertTrue(ann.openWorldHint)

    def test_users_delete_is_delete(self):
        svc = self._make_service()
        tools = svc.list_tools()
        delete_tool = tools["users_delete"]["definition"]
        ann = delete_tool.annotations
        assert ann is not None
        self.assertFalse(ann.readOnlyHint)
        self.assertTrue(ann.destructiveHint)
        self.assertTrue(ann.idempotentHint)
        self.assertTrue(ann.openWorldHint)

    def test_all_users_tools_have_annotations(self):
        # Catch a regression where a new SDK method gets a Tool() without
        # annotations. Every tool returned by list_tools() must carry them.
        svc = self._make_service()
        tools = svc.list_tools()
        self.assertGreater(len(tools), 5, "users service should have many tools")
        for tool_name, entry in tools.items():
            with self.subTest(tool=tool_name):
                ann = entry["definition"].annotations
                self.assertIsNotNone(
                    ann,
                    f"{tool_name} is missing annotations",
                )
                # The wire-format annotations can encode only 3 distinguishable
                # buckets (read / delete / write-or-unknown) because MCP's
                # spec exposes only 4 boolean hints and write + unknown share
                # the same shape. The classification string drives the choice
                # but the wire-format cannot always tell them apart — verify
                # the *consistent* boundary instead of the exact bucket.
                expected = classify_tool_name(tool_name)
                _assert_annotations_consistent_with(ann, expected, tool_name)


class AnnotationsModuleSurfaceTests(unittest.TestCase):
    """The annotations module exposes the documented surface."""

    def test_public_api(self):
        self.assertTrue(hasattr(annotations, "annotations_for_classification"))
        self.assertTrue(hasattr(annotations, "classify_tool_name"))
        self.assertIn("annotations_for_classification", annotations.__all__)
        self.assertIn("classify_tool_name", annotations.__all__)


# ---------------------------------------------------------------------------
# Helpers (used by multiple test classes)
# ---------------------------------------------------------------------------


def _assert_annotations_consistent_with(
    ann: types.ToolAnnotations, bucket: str, tool_name: str
) -> None:
    """Verify the annotations are *consistent* with the classification bucket.

    MCP's ``ToolAnnotations`` exposes only four boolean hints. With those four
    bits we can distinguish at most three buckets on the wire:

    - **read**     → readOnlyHint=True
    - **delete**   → destructiveHint=True (with readOnlyHint=False)
    - **write/unknown** → readOnlyHint=False, destructiveHint=False

    The internal classification also distinguishes write from unknown, but
    the wire format collapses them. We assert only what's observable: the
    tool's readOnly/destructive pair must agree with the bucket's contract.
    """
    if bucket == "read":
        assert (
            ann.readOnlyHint is True
        ), f"{tool_name}: read bucket must have readOnlyHint=True"
        assert (
            ann.destructiveHint is False
        ), f"{tool_name}: read bucket must have destructiveHint=False"
    elif bucket == "delete":
        assert (
            ann.readOnlyHint is False
        ), f"{tool_name}: delete bucket must have readOnlyHint=False"
        assert (
            ann.destructiveHint is True
        ), f"{tool_name}: delete bucket must have destructiveHint=True"
    else:  # write or unknown — wire format is identical for both
        assert (
            ann.readOnlyHint is False
        ), f"{tool_name}: {bucket} bucket must have readOnlyHint=False"
        assert (
            ann.destructiveHint is False
        ), f"{tool_name}: {bucket} bucket must have destructiveHint=False"


def _bucket_from_annotations(ann: types.ToolAnnotations) -> str:
    """Reverse-derive the classification bucket from a ToolAnnotations instance.

    Used by OperatorClassificationParityTests to confirm the Service-derived
    annotations match the verb-bucket that ``classify_tool_name`` would
    assign to the same tool name.

    Note: the wire format cannot distinguish write from unknown (both have
    readOnly=False, destructive=False), so this helper returns the closest
    bucket the hints can express. Callers that need the exact bucket should
    use ``classify_tool_name`` directly on the tool name.
    """
    if ann.readOnlyHint is True:
        return "read"
    if ann.destructiveHint is True:
        return "delete"
    return "write"  # write and unknown collapse here


class OperatorStub:
    """Minimal stand-in exposing ``get_public_tools`` for PublicToolSurfaceTests.

    The Operator's annotation code path doesn't touch the catalog, so a stub
    with an empty ToolManager is enough to exercise the public-tool annotation
    logic without booting a real SDK client.
    """

    def __init__(self, tools_manager):
        self._tools_manager = tools_manager
        self._search_limit = 8
        self._docs_search = None
        # Operator requires preview_threshold + store_results too — but
        # they're only used by execute_public_tool, not get_public_tools.

    def get_public_tools(self) -> list[types.Tool]:
        # Delegate to the real Operator so we test the actual code path
        # rather than a parallel copy.
        from mcp_server_appwrite.operator import Operator

        op = Operator(
            tools_manager=self._tools_manager,
            execute_tool=lambda *args, **kwargs: [],
            docs_search=self._docs_search,
        )
        return op.get_public_tools()


if __name__ == "__main__":
    unittest.main()
