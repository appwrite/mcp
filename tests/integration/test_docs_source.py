"""Live contract test for the documentation exports the docs index is built from.

``scripts/build_docs_index.py`` relies on appwrite.io publishing a page manifest at
``/docs/llms.txt`` and a Markdown export for every page at ``/docs/<slug>.md``.
This locks that contract end-to-end against the live site: the manifest is
substantial, published pages come back as Markdown with front matter, gated pages
are skipped rather than indexed, and a full fetch yields a usable index input.

Unlike the other integration tests this needs no Appwrite credentials, only network
access to the public site, so it is not gated on live configuration.
"""

from __future__ import annotations

import asyncio
import unittest

import httpx

from mcp_server_appwrite.docs_source import (
    DEFAULT_ORIGIN,
    Entry,
    chunk_markdown,
    fetch_manifest,
    fetch_page,
    fetch_pages,
)

# Well below the ~650 pages published today, but high enough to catch a broken or
# truncated manifest before it gets embedded and shipped.
MINIMUM_PAGES = 400
MAXIMUM_SKIPPED_RATIO = 0.25


def run(coroutine):
    async def with_client():
        async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
            return await coroutine(client)

    return asyncio.run(with_client())


class DocsSourceContractTests(unittest.TestCase):
    def test_manifest_lists_documentation_pages(self):
        entries = run(lambda client: fetch_manifest(client, DEFAULT_ORIGIN))

        paths = [entry.path for entry in entries]
        self.assertGreaterEqual(len(paths), MINIMUM_PAGES)
        self.assertEqual(len(paths), len(set(paths)), "manifest has duplicate paths")
        self.assertIn("docs/products/auth", paths)
        self.assertIn("docs/products/databases", paths)
        self.assertTrue(all(path.startswith("docs") for path in paths))
        self.assertTrue(all(entry.title for entry in entries))

    def test_published_page_exports_markdown_with_front_matter(self):
        entry = Entry("docs/products/auth", "Authentication", "")
        page = run(lambda client: fetch_page(client, DEFAULT_ORIGIN, entry))

        assert page is not None
        self.assertEqual(page.path, "docs/products/auth")
        self.assertEqual(page.title, "Authentication")
        self.assertTrue(page.description)
        self.assertFalse(page.content.startswith("---"), "front matter not stripped")
        self.assertFalse(page.content.lower().startswith("<!doctype"))
        self.assertNotIn("{% partial", page.content)
        self.assertGreater(len(chunk_markdown(page.content)), 0)

    def test_unpublished_page_is_skipped(self):
        entry = Entry("docs/this-page-does-not-exist", "Missing", "")
        page = run(lambda client: fetch_page(client, DEFAULT_ORIGIN, entry))

        self.assertIsNone(page)

    def test_full_fetch_produces_index_input(self):
        pages, skipped = asyncio.run(fetch_pages(DEFAULT_ORIGIN))

        self.assertGreaterEqual(len(pages), MINIMUM_PAGES)
        self.assertLessEqual(
            len(skipped) / (len(pages) + len(skipped)),
            MAXIMUM_SKIPPED_RATIO,
            f"too many pages skipped: {skipped}",
        )
        self.assertTrue(all(page.title for page in pages))
        self.assertTrue(all(page.content for page in pages))
        self.assertFalse(
            any("{% partial" in page.content for page in pages),
            "export left Markdoc partials unresolved",
        )
        # Docs legitimately embed HTML samples, so only the body start is checked.
        self.assertFalse(
            any(page.content.lower().startswith("<!doctype") for page in pages),
            "an HTML page slipped through the Markdown check",
        )
