"""Fetch the Appwrite documentation as Markdown from the published site.

The docs are authored in the private ``appwrite/vibes`` repository, which also
serves them at https://appwrite.io. Rather than parsing the raw Markdoc source
(and re-implementing its partials, ``docs-local`` overrides, and feature gating),
the index is built from what the site actually publishes:

* ``/docs/llms.txt`` — a Markdown link index of every documentation page.
* ``/docs/<slug>.md`` — each page rendered to plain Markdown, front matter kept.

Pages listed in the index but not published as Markdown are skipped: feature-gated
content answers 404, and API reference pages answer with the HTML app shell.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

import httpx

DEFAULT_ORIGIN = "https://appwrite.io"
MANIFEST_PATH = "/docs/llms.txt"
DEFAULT_CONCURRENCY = 16
REQUEST_TIMEOUT = 60.0

# Header-aware sections packed to ~1500 chars with ~200 chars of overlap. Exact
# sizing is not load-bearing; retrieval quality is dominated by the embedding
# model.
CHUNK_SIZE = 1500
CHUNK_OVERLAP = 200

MARKDOWN_MEDIA_TYPE = "text/markdown"

_MANIFEST_LINK = re.compile(
    r"\[(?P<title>[^\]]*)\]"
    r"\(https?://[^/\s)]+/(?P<path>docs(?:/[^\s)]*)?)\.md\)"
    r"(?::\s*(?P<description>.*))?$",
    re.MULTILINE,
)
_FRONT_MATTER = re.compile(r"^\s*---\r?\n(.*?)\r?\n---\s*\r?\n?(.*)$", re.DOTALL)
_FRONT_MATTER_FIELD = re.compile(r"^([A-Za-z_][\w-]*):\s*(.*)$")
_HEADING = re.compile(r"^#{1,6}\s")


@dataclass(frozen=True)
class Entry:
    """One link from the ``llms.txt`` manifest."""

    path: str
    title: str
    description: str


@dataclass(frozen=True)
class Page:
    """One documentation page as stored in the index metadata."""

    path: str
    title: str
    description: str
    content: str


def parse_manifest(text: str) -> list[Entry]:
    """Return the ordered, de-duplicated page links from ``llms.txt``.

    Paths are site-relative without a leading slash or ``.md`` suffix, e.g.
    ``docs/products/auth``. The docs root is ``docs``.
    """
    entries: list[Entry] = []
    seen: set[str] = set()
    for match in _MANIFEST_LINK.finditer(text):
        path = match.group("path").rstrip("/")
        if path in seen:
            continue
        seen.add(path)
        entries.append(
            Entry(
                path=path,
                title=match.group("title").strip(),
                description=(match.group("description") or "").strip(),
            )
        )
    return entries


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split simple ``key: value`` front matter from the Markdown body.

    The rendered pages only carry flat scalar fields (``title``, ``description``,
    ``layout``), so a full YAML parser is unnecessary. Surrounding quotes are
    stripped from values.
    """
    match = _FRONT_MATTER.match(text)
    if not match:
        return {}, text
    attributes: dict[str, str] = {}
    for line in match.group(1).splitlines():
        field = _FRONT_MATTER_FIELD.match(line)
        if not field:
            continue
        value = field.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        attributes[field.group(1)] = value
    return attributes, match.group(2)


def chunk_markdown(text: str) -> list[str]:
    """Split Markdown into header-delimited chunks bounded by ``CHUNK_SIZE``."""
    text = text.strip()
    if not text:
        return []

    sections: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if _HEADING.match(line) and current:
            sections.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current).strip())

    chunks: list[str] = []
    for section in sections:
        if not section:
            continue
        if len(section) <= CHUNK_SIZE:
            chunks.append(section)
            continue
        start = 0
        while start < len(section):
            end = start + CHUNK_SIZE
            chunks.append(section[start:end].strip())
            if end >= len(section):
                break
            start = end - CHUNK_OVERLAP
    return [chunk for chunk in chunks if chunk]


def build_page(entry: Entry, markdown: str) -> Page | None:
    """Turn a rendered page into a ``Page``, or ``None`` when it has no body.

    Front matter wins for title and description; the manifest link text fills in
    when a page ships without front matter.
    """
    attributes, body = parse_front_matter(markdown)
    body = body.strip()
    if not body:
        return None
    return Page(
        path=entry.path,
        title=attributes.get("title") or entry.title,
        description=attributes.get("description") or entry.description,
        content=body,
    )


async def fetch_manifest(client: httpx.AsyncClient, origin: str) -> list[Entry]:
    response = await client.get(f"{origin}{MANIFEST_PATH}")
    response.raise_for_status()
    return parse_manifest(response.text)


async def fetch_page(
    client: httpx.AsyncClient, origin: str, entry: Entry
) -> Page | None:
    """Fetch one page; ``None`` when it is not published as Markdown or is empty."""
    response = await client.get(f"{origin}/{entry.path}.md")
    if response.status_code == httpx.codes.NOT_FOUND:
        return None
    response.raise_for_status()
    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
    if media_type != MARKDOWN_MEDIA_TYPE:
        return None
    return build_page(entry, response.text)


async def fetch_pages(
    origin: str = DEFAULT_ORIGIN,
    *,
    concurrency: int = DEFAULT_CONCURRENCY,
    client: httpx.AsyncClient | None = None,
) -> tuple[list[Page], list[str]]:
    """Fetch every published docs page listed in the site manifest.

    Returns the pages in manifest order plus the paths that were listed but
    skipped (unpublished, not exported as Markdown, or empty).
    """
    owned = client is None
    if client is None:
        client = httpx.AsyncClient(follow_redirects=True, timeout=REQUEST_TIMEOUT)
    try:
        entries = await fetch_manifest(client, origin)
        semaphore = asyncio.Semaphore(concurrency)

        async def bounded(entry: Entry) -> Page | None:
            async with semaphore:
                return await fetch_page(client, origin, entry)

        results = await asyncio.gather(*(bounded(entry) for entry in entries))
    finally:
        if owned:
            await client.aclose()

    pages = [page for page in results if page is not None]
    skipped = [entry.path for entry, page in zip(entries, results) if page is None]
    return pages, skipped
