"""Core Markdown link checking logic."""

from __future__ import annotations

import re
import string
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote


LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(([^)\n]*)\)")
FENCE_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})")
ATX_HEADING_RE = re.compile(r"^[ ]{0,3}#{1,6}(?:[ \t]+|$)(.*)$")
SETEXT_HEADING_RE = re.compile(r"^[ ]{0,3}(=+|-+)[ \t]*$")
EXTERNAL_PREFIXES = ("http://", "https://", "mailto:")


class PathInputError(ValueError):
    """Raised when a requested input path cannot be scanned."""


@dataclass(frozen=True)
class LinkError:
    """A broken local Markdown link."""

    source: Path
    line: int
    column: int
    kind: str
    target: str

    def format(self) -> str:
        return f"{self.source}:{self.line}:{self.column}: {self.kind} {self.target}"


@dataclass(frozen=True)
class _Link:
    line: int
    column: int
    target: str


def check_paths(paths: list[str] | tuple[str, ...]) -> list[LinkError]:
    """Return broken-link errors found under files and directories in *paths*."""
    markdown_files = _collect_markdown_files(paths)
    anchor_cache: dict[Path, set[str]] = {}
    errors: list[LinkError] = []

    for source in markdown_files:
        text = _read_markdown(source)
        for link in _extract_links(text):
            if link.target.lower().startswith(EXTERNAL_PREFIXES):
                continue
            error = _check_link(source, link, anchor_cache)
            if error is not None:
                errors.append(error)
    return errors


def heading_slug(text: str) -> str:
    """Convert heading text to the GitHub-style slug used by this tool."""
    text = text.strip().lower()
    characters: list[str] = []
    for character in text:
        if character in "-_":
            characters.append(character)
        elif character in string.punctuation:
            continue
        elif unicodedata.category(character).startswith("P"):
            continue
        else:
            characters.append(character)
    return re.sub(r"\s+", "-", "".join(characters))


def extract_anchors(text: str) -> set[str]:
    """Extract unique generated heading anchors from Markdown text."""
    lines = _mask_fenced_lines(text.splitlines())
    headings: list[str] = []

    for index, line in enumerate(lines):
        atx_match = ATX_HEADING_RE.match(line)
        if atx_match:
            heading = re.sub(r"[ \t]+#+[ \t]*$", "", atx_match.group(1))
            headings.append(heading)
            continue
        if index > 0 and line and SETEXT_HEADING_RE.match(line):
            previous = lines[index - 1]
            if previous.strip():
                headings.append(previous.strip())

    counts: dict[str, int] = {}
    anchors: set[str] = set()
    for heading in headings:
        base = heading_slug(heading)
        duplicate_number = counts.get(base, 0)
        anchor = base if duplicate_number == 0 else f"{base}-{duplicate_number}"
        counts[base] = duplicate_number + 1
        anchors.add(anchor)
    return anchors


def _collect_markdown_files(paths: list[str] | tuple[str, ...]) -> list[Path]:
    files: dict[Path, Path] = {}
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            raise PathInputError(f"path does not exist: {path}")
        if path.is_file():
            if path.suffix.lower() != ".md":
                raise PathInputError(f"not a Markdown file: {path}")
            files[path.resolve()] = path
        elif path.is_dir():
            for candidate in path.rglob("*"):
                if candidate.is_file() and candidate.suffix.lower() == ".md":
                    files[candidate.resolve()] = candidate
        else:
            raise PathInputError(f"unsupported path type: {path}")
    return sorted(files.values(), key=lambda item: str(item).casefold())


def _read_markdown(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PathInputError(f"cannot read {path}: {exc}") from exc


def _extract_links(text: str) -> list[_Link]:
    links: list[_Link] = []
    for line_number, line in enumerate(_mask_fenced_lines(text.splitlines()), start=1):
        masked = _mask_inline_code(line)
        for match in LINK_RE.finditer(masked):
            links.append(
                _Link(
                    line=line_number,
                    column=match.start() + 1,
                    target=match.group(1).strip(),
                )
            )
    return links


def _mask_fenced_lines(lines: list[str]) -> list[str]:
    masked: list[str] = []
    fence_character: str | None = None
    fence_length = 0

    for line in lines:
        match = FENCE_RE.match(line)
        if fence_character is None:
            if match:
                marker = match.group(1)
                fence_character = marker[0]
                fence_length = len(marker)
                masked.append("")
            else:
                masked.append(line)
            continue

        masked.append("")
        if match:
            marker = match.group(1)
            if marker[0] == fence_character and len(marker) >= fence_length:
                fence_character = None
                fence_length = 0
    return masked


def _mask_inline_code(line: str) -> str:
    characters = list(line)
    index = 0
    while index < len(line):
        if line[index] != "`":
            index += 1
            continue
        run_end = index
        while run_end < len(line) and line[run_end] == "`":
            run_end += 1
        delimiter = line[index:run_end]
        closing = line.find(delimiter, run_end)
        if closing == -1:
            index = run_end
            continue
        for masked_index in range(index, closing + len(delimiter)):
            characters[masked_index] = " "
        index = closing + len(delimiter)
    return "".join(characters)


def _check_link(
    source: Path,
    link: _Link,
    anchor_cache: dict[Path, set[str]],
) -> LinkError | None:
    path_part, separator, anchor_part = link.target.partition("#")
    decoded_path = unquote(path_part)
    decoded_anchor = unquote(anchor_part) if separator else ""
    destination = source if decoded_path == "" else source.parent / decoded_path

    if not destination.is_file():
        return LinkError(source, link.line, link.column, "missing-file", link.target)

    if separator:
        resolved_destination = destination.resolve()
        if resolved_destination not in anchor_cache:
            anchor_cache[resolved_destination] = extract_anchors(
                _read_markdown(destination)
            )
        if decoded_anchor not in anchor_cache[resolved_destination]:
            return LinkError(
                source,
                link.line,
                link.column,
                "missing-anchor",
                link.target,
            )
    return None
