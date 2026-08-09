"""Keep every maintained Markdown page reachable from MkDocs navigation."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = REPO_ROOT / "docs"
MKDOCS_CONFIG = REPO_ROOT / "mkdocs.yml"
MARKDOWN_PATH = re.compile(r"(?P<path>[A-Za-z0-9_./-]+\.md)\s*$", re.MULTILINE)


def test_mkdocs_navigation_covers_all_markdown_pages() -> None:
    config = MKDOCS_CONFIG.read_text(encoding="utf-8")
    _prefix, separator, nav_text = config.partition("\nnav:\n")
    assert separator, "mkdocs.yml must define a top-level nav section"

    configured_paths = [match.group("path") for match in MARKDOWN_PATH.finditer(nav_text)]
    configured = set(configured_paths)
    maintained = {path.relative_to(DOCS_ROOT).as_posix() for path in DOCS_ROOT.rglob("*.md")}
    missing = sorted(maintained - configured)
    stale = sorted(configured - maintained)
    duplicates = sorted(path for path in configured if configured_paths.count(path) > 1)

    assert not missing, "Add maintained Markdown pages to mkdocs.yml nav: " + ", ".join(missing)
    assert not stale, "Remove missing Markdown pages from mkdocs.yml nav: " + ", ".join(stale)
    assert not duplicates, "Remove duplicate Markdown pages from mkdocs.yml nav: " + ", ".join(
        duplicates
    )
