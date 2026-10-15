"""Server instructions sent to MCP clients on initialize.

The prose lives in ``assets/instructions.json`` so it can be edited without
touching code. The file is an ordered list of sections; each section holds
sentences and optional conditions. A section with ``transports`` is included
only for those transports (the API-key stdio catalog lacks console-only APIs
such as dedicated databases and domain registration, see ``catalog_policy``),
and one with ``docs`` only when docs search availability matches. Sections that
apply are joined into a single paragraph in file order.
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
    sentences: tuple[str, ...]
    transports: frozenset[str] | None = None
    docs: bool | None = None

    def applies(self, transport: str, docs_enabled: bool) -> bool:
        if self.transports is not None and transport not in self.transports:
            return False
        return self.docs is None or self.docs == docs_enabled

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> Section:
        unknown = raw.keys() - {"sentences", "transports", "docs"}
        if unknown:
            raise ValueError(f"Unknown instruction section keys: {sorted(unknown)}")
        sentences = raw["sentences"]
        if not sentences or not all(
            isinstance(sentence, str) and sentence.strip() for sentence in sentences
        ):
            raise ValueError("Instruction sections need non-empty sentences.")
        transports = raw.get("transports")
        if transports is not None:
            invalid = set(transports) - TRANSPORTS
            if not transports or invalid:
                raise ValueError(f"Invalid instruction transports: {transports}")
            transports = frozenset(transports)
        docs = raw.get("docs")
        if docs is not None and not isinstance(docs, bool):
            raise ValueError("Instruction section 'docs' must be a boolean.")
        return cls(tuple(sentences), transports, docs)


@cache
def load_sections() -> tuple[Section, ...]:
    raw = json.loads(
        files("mcp_server_appwrite.assets").joinpath("instructions.json").read_text()
    )
    return tuple(Section.parse(section) for section in raw["sections"])


def build_instructions(transport: str = "http", *, docs_enabled: bool = True) -> str:
    return " ".join(
        sentence
        for section in load_sections()
        if section.applies(transport, docs_enabled)
        for sentence in section.sentences
    )
