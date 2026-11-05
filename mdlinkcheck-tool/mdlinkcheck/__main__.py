"""Command-line entry point for mdlinkcheck."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from .core import PathInputError, check_paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mdlinkcheck",
        description="Check local file and heading-anchor links in Markdown files.",
    )
    parser.add_argument(
        "paths",
        nargs="+",
        metavar="PATH",
        help="Markdown file or directory to scan recursively",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        errors = check_paths(args.paths)
    except PathInputError as exc:
        print(f"mdlinkcheck: error: {exc}", file=sys.stderr)
        return 2

    for error in errors:
        print(error.format())
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
