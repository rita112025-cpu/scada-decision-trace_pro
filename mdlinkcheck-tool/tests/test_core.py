from __future__ import annotations

import unittest
from pathlib import Path

from mdlinkcheck.core import check_paths, extract_anchors, heading_slug

FIXTURES = Path(__file__).parent / "fixtures"


class CoreTests(unittest.TestCase):
    def test_heading_slugs_and_duplicate_suffixes(self) -> None:
        text = "# Hello, World!\n# Repeat\n# Repeat\n# Under_score - kept\n"
        self.assertEqual(heading_slug("Hello, World!"), "hello-world")
        self.assertEqual(
            extract_anchors(text),
            {"hello-world", "repeat", "repeat-1", "under_score---kept"},
        )

    def test_files_anchors_external_links_and_ignored_code(self) -> None:
        source = FIXTURES / "cases" / "source.md"
        errors = check_paths([str(source)])

        self.assertEqual(
            [(error.kind, error.target) for error in errors],
            [
                ("missing-file", "no.md"),
                ("missing-anchor", "guide.md#missing"),
                ("missing-anchor", "#missing"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
