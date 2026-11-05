from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path

from mdlinkcheck.__main__ import main


FIXTURES = Path(__file__).parent / "fixtures"


class CliTests(unittest.TestCase):
    def run_main(self, paths: list[str]) -> tuple[int, list[str], str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(paths)
        return result, stdout.getvalue().splitlines(), stderr.getvalue()

    def test_fixture_output_is_exact(self) -> None:
        exit_code, output, stderr = self.run_main([str(FIXTURES)])
        source = FIXTURES / "root.md"
        self.assertEqual(exit_code, 1)
        self.assertEqual(stderr, "")
        case_source = FIXTURES / "cases" / "source.md"
        self.assertEqual(
            output,
            [
                f"{case_source}:3:1: missing-file no.md",
                f"{case_source}:6:1: missing-anchor guide.md#missing",
                f"{case_source}:8:1: missing-anchor #missing",
                f"{source}:3:1: missing-file missing.md",
                f"{source}:5:1: missing-anchor guide.md#absent",
                f"{source}:8:1: missing-anchor #elsewhere",
                f"{source}:13:1: missing-file assets/nope.png",
            ],
        )

    def test_exit_code_zero(self) -> None:
        source = FIXTURES / "valid.md"
        exit_code, output, stderr = self.run_main([str(source)])
        self.assertEqual((exit_code, output, stderr), (0, [], ""))

    def test_exit_code_two_for_missing_path(self) -> None:
        missing = FIXTURES / "does-not-exist"
        exit_code, output, stderr = self.run_main([str(missing)])
        self.assertEqual(exit_code, 2)
        self.assertEqual(output, [])
        self.assertIn("path does not exist", stderr)


if __name__ == "__main__":
    unittest.main()
