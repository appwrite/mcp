import asyncio
import unittest

import httpx

from mcp_server_appwrite.docs_source import (
    CHUNK_SIZE,
    Entry,
    build_page,
    chunk_markdown,
    fetch_pages,
    parse_front_matter,
    parse_manifest,
)

MANIFEST = """# Appwrite Docs

Top-level docs index: https://appwrite.io/docs.md

## Billing

- [Overview](https://appwrite.io/docs/advanced/billing.md): Plans and policies.
- [Free](https://appwrite.io/docs/advanced/billing/free.md): Free plan.
- [Overview](https://appwrite.io/docs/advanced/billing.md): Listed twice.

## Auth

- [Overview](https://appwrite.io/docs/products/auth.md): Authentication.
- [Partners](https://appwrite.io/docs/partners/apps.md): Gated page.
- [Blog post](https://appwrite.io/blog/post.md): Not a docs page.
"""

AUTH_PAGE = """---
layout: article
title: Authentication
description: Explore Appwrite's authentication: sessions, tokens, and more.
back: /docs
---

Appwrite **Authentication** delivers more than sign up and log in.

# Methods

Email, phone, OAuth.
"""


class ParseManifestTests(unittest.TestCase):
    def test_returns_docs_links_in_order_without_duplicates(self):
        entries = parse_manifest(MANIFEST)
        self.assertEqual(
            [entry.path for entry in entries],
            [
                "docs/advanced/billing",
                "docs/advanced/billing/free",
                "docs/products/auth",
                "docs/partners/apps",
            ],
        )
        self.assertEqual(entries[0].title, "Overview")
        self.assertEqual(entries[0].description, "Plans and policies.")
        self.assertEqual(entries[1].title, "Free")

    def test_accepts_any_origin(self):
        text = "- [Root](http://localhost:3000/docs.md)\n- [X](http://localhost:3000/docs/x.md)"
        self.assertEqual(
            [entry.path for entry in parse_manifest(text)], ["docs", "docs/x"]
        )

    def test_link_without_description(self):
        entries = parse_manifest("- [Root](https://appwrite.io/docs.md)\n")
        self.assertEqual(entries[0].description, "")

    def test_ignores_non_docs_links(self):
        self.assertEqual(parse_manifest("- [Blog](https://appwrite.io/blog.md)"), [])


class ParseFrontMatterTests(unittest.TestCase):
    def test_splits_scalar_fields_from_body(self):
        attributes, body = parse_front_matter(AUTH_PAGE)
        self.assertEqual(attributes["title"], "Authentication")
        self.assertEqual(
            attributes["description"],
            "Explore Appwrite's authentication: sessions, tokens, and more.",
        )
        self.assertEqual(attributes["layout"], "article")
        self.assertTrue(body.startswith("Appwrite **Authentication**"))

    def test_strips_quotes_from_values(self):
        attributes, _ = parse_front_matter('---\ntitle: "Quoted: yes"\n---\nbody')
        self.assertEqual(attributes["title"], "Quoted: yes")

    def test_no_front_matter_returns_text_unchanged(self):
        self.assertEqual(parse_front_matter("# Hello\n"), ({}, "# Hello\n"))


class ChunkMarkdownTests(unittest.TestCase):
    def test_splits_on_headings(self):
        chunks = chunk_markdown("intro\n\n# A\nbody a\n\n## B\nbody b")
        self.assertEqual(chunks, ["intro", "# A\nbody a", "## B\nbody b"])

    def test_oversized_section_is_split_with_overlap(self):
        chunks = chunk_markdown("x" * (CHUNK_SIZE * 2))
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= CHUNK_SIZE for chunk in chunks))

    def test_empty_text_produces_no_chunks(self):
        self.assertEqual(chunk_markdown("  \n"), [])


class BuildPageTests(unittest.TestCase):
    def test_builds_page_from_rendered_markdown(self):
        entry = Entry("docs/products/auth", "Auth link", "Link description")
        page = build_page(entry, AUTH_PAGE)
        assert page is not None
        self.assertEqual(page.path, "docs/products/auth")
        self.assertEqual(page.title, "Authentication")
        self.assertTrue(page.content.startswith("Appwrite **Authentication**"))
        self.assertIn("# Methods", page.content)

    def test_manifest_fills_in_missing_front_matter(self):
        entry = Entry("docs/references/account", "Account", "Account API reference.")
        page = build_page(entry, "# Account\n\nMethods.\n")
        assert page is not None
        self.assertEqual(page.title, "Account")
        self.assertEqual(page.description, "Account API reference.")

    def test_empty_body_is_dropped(self):
        entry = Entry("docs/x", "X", "")
        self.assertIsNone(build_page(entry, "---\ntitle: X\n---\n\n"))


class FetchPagesTests(unittest.TestCase):
    def test_fetches_listed_pages_and_skips_unpublished(self):
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            if request.url.path == "/docs/llms.txt":
                return httpx.Response(200, text=MANIFEST)
            if request.url.path == "/docs/partners/apps.md":
                return httpx.Response(404)
            if request.url.path == "/docs/products/auth.md":
                return httpx.Response(
                    200,
                    text="<!DOCTYPE html><html></html>",
                    headers={"content-type": "text/html; charset=utf-8"},
                )
            slug = request.url.path.removesuffix(".md").rsplit("/", 1)[-1]
            return httpx.Response(
                200,
                text=f"---\ntitle: {slug}\n---\n# {slug}\n",
                headers={"content-type": "text/markdown; charset=utf-8"},
            )

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                return await fetch_pages("https://example.test", client=client)

        pages, skipped = asyncio.run(run())

        self.assertEqual(
            [page.path for page in pages],
            ["docs/advanced/billing", "docs/advanced/billing/free"],
        )
        self.assertEqual(pages[0].title, "billing")
        self.assertEqual(skipped, ["docs/products/auth", "docs/partners/apps"])
        self.assertIn("/docs/advanced/billing/free.md", requested)
        self.assertNotIn("/blog/post.md", requested)

    def test_server_error_is_raised(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/docs/llms.txt":
                return httpx.Response(200, text=MANIFEST)
            return httpx.Response(500)

        async def run():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)
            ) as client:
                return await fetch_pages("https://example.test", client=client)

        with self.assertRaises(httpx.HTTPStatusError):
            asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
