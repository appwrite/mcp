"""Validation of ``assets/instructions.json`` sections.

Unit tests rather than e2e: the shipped file is always valid, so no e2e flow can
reach the malformed-section errors that guard future edits to it.
"""

import unittest

from mcp_server_appwrite.instructions import Section, load_sections


class SectionTest(unittest.TestCase):
    def test_shipped_file_loads_and_covers_every_transport(self):
        sections = load_sections()
        for transport in ("stdio", "http"):
            self.assertTrue(
                any(section.applies(transport, True) for section in sections)
            )

    def test_applies_filters_by_transport_and_docs(self):
        section = Section(("Hosted only.",), frozenset({"http"}), True)
        self.assertTrue(section.applies("http", True))
        self.assertFalse(section.applies("stdio", True))
        self.assertFalse(section.applies("http", False))

    def test_parse_rejects_malformed_sections(self):
        invalid = [
            {"lines": []},
            {"lines": [""]},
            {"lines": ["Text."], "transports": ["websocket"]},
            {"lines": ["Text."], "transports": []},
            {"lines": ["Text."], "docs": "yes"},
            {"lines": ["Text."], "transport": ["http"]},
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                Section.parse(raw)

    def test_parse_reads_conditions(self):
        section = Section.parse(
            {"lines": ["Text."], "transports": ["stdio"], "docs": False}
        )
        self.assertEqual(section.lines, ("Text.",))
        self.assertEqual(section.transports, frozenset({"stdio"}))
        self.assertIs(section.docs, False)


if __name__ == "__main__":
    unittest.main()
