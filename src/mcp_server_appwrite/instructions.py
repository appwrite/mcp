"""Server metadata and instructions sent to MCP clients on initialize.

The prose lives in ``assets/instructions.json`` so it can be edited without
touching code. Its ``description`` is the server's ``serverInfo.description``;
its ``sections`` are an ordered list in which each section holds lines of
Markdown and optional conditions. A section with ``transports`` is included
only for those transports (the API-key stdio catalog lacks console-only APIs
such as dedicated databases and domain registration, see ``catalog_policy``),
and one with ``docs`` only when docs search availability matches. Lines within
a section are joined by newlines and sections by blank lines, in file order.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from importlib.resources import files
from typing import Any

from .constants import TRANSPORTS


@dataclass(frozen=True, slots=True)
class Section:
    lines: tuple[str, ...]
    transports: frozenset[str] | None = None
    docs: bool | None = None

    def applies(self, transport: str, docs_enabled: bool) -> bool:
        if self.transports is not None and transport not in self.transports:
            return False
        return self.docs is None or self.docs == docs_enabled

    def render(self) -> str:
        return "\n".join(self.lines)

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Section:
        unknown = raw.keys() - {"lines", "transports", "docs"}
        if unknown:
            raise ValueError(f"Unknown instruction section keys: {sorted(unknown)}")
        lines = raw["lines"]
        if not lines or not all(
            isinstance(line, str) and line.strip() for line in lines
        ):
            raise ValueError("Instruction sections need non-empty lines.")
        transports = raw.get("transports")
        if transports is not None:
            invalid = set(transports) - TRANSPORTS
            if not transports or invalid:
                raise ValueError(f"Invalid instruction transports: {transports}")
            transports = frozenset(transports)
        docs = raw.get("docs")
        if docs is not None and not isinstance(docs, bool):
            raise ValueError("Instruction section 'docs' must be a boolean.")
        return cls(tuple(lines), transports, docs)


@cache
def _load() -> dict[str, Any]:
    return json.loads(
        files("mcp_server_appwrite.assets").joinpath("instructions.json").read_text()
    )


@cache
def load_sections() -> tuple[Section, ...]:
    return tuple(Section.parse(section) for section in _load()["sections"])


@cache
def load_description() -> str:
    """The ``serverInfo.description`` clients see on initialize."""
    description = _load()["description"]
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Instructions 'description' must be a non-empty string.")
    return description


def build_instructions(transport: str = "http", *, docs_enabled: bool = True) -> str:
    return "\n\n".join(
        section.render()
        for section in load_sections()
        if section.applies(transport, docs_enabled)
    )
