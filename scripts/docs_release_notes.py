"""Turn a docs index build report into release notes for the refresh workflow.

Reads the JSON written by ``scripts/build_docs_index.py`` (``DOCS_REPORT_FILE``)
and writes Markdown listing the added, updated, and removed pages with links to
appwrite.io. The same text becomes the GitHub release body and the job summary.

    uv run python scripts/docs_release_notes.py report.json notes.md

Exit status is 0 in every case; the workflow decides whether to release from
the committed diff, not from this script.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from mcp_server_appwrite.docs_source import DEFAULT_ORIGIN

SECTIONS = (("added", "Added"), ("changed", "Updated"), ("removed", "Removed"))
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<>#|])")


def render(report: dict[str, Any], origin: str = DEFAULT_ORIGIN) -> str:
    changes = report.get("changes", {})
    counts = {key: len(changes.get(key, [])) for key, _ in SECTIONS}
    lines = [
        "Refresh the embedded Appwrite documentation index.",
        "",
        f"{report['pages']} pages, {report['chunks']} chunks "
        f"({report['chunks_embedded']} embedded, {report['chunks_reused']} reused).",
        "",
    ]
    if not any(counts.values()):
        lines.append("No documentation pages were added, updated, or removed.")
        return "\n".join(lines) + "\n"
    for key, heading in SECTIONS:
        pages = changes.get(key, [])
        if not pages:
            continue
        lines.append(f"### {heading} ({len(pages)})")
        lines.append("")
        for page in pages:
            title = page.get("title") or page["path"]
            lines.append(
                f"- [{escape(title)}]({origin}/{quote(page['path'], safe='/')})"
            )
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def escape(text: str) -> str:
    """Neutralize Markdown syntax in page titles so links render as written."""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text.replace("\n", " "))


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: docs_release_notes.py <report.json> <notes.md>", file=sys.stderr)
        return 2
    report = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    Path(argv[2]).write_text(render(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
