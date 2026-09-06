"""Markdown helpers shared by docs and CLI route tests."""

from __future__ import annotations

import re
import string
from pathlib import Path


def github_markdown_heading_anchors(path: Path) -> set[str]:
    """Return GitHub-style heading anchors for a Markdown file."""
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^#{1,6}\s+(?P<title>.+?)\s*$", line)
        if match is None:
            continue
        title = re.sub(r"\s+#+$", "", match.group("title").strip())
        slug = title.lower()
        # GitHub and Python-Markdown's ``toc`` both keep ``-`` and ``_`` and
        # drop the rest of the punctuation, so a heading like ``EASYCAT_E304``
        # anchors as ``#easycat_e304``. Stripping the underscore here would
        # reject links that resolve correctly in both renderers.
        droppable = string.punctuation.replace("-", "").replace("_", "")
        slug = slug.translate(str.maketrans("", "", droppable))
        slug = re.sub(r"\s+", "-", slug).strip("-")
        if not slug:
            continue

        duplicate_index = counts.get(slug, 0)
        counts[slug] = duplicate_index + 1
        anchors.add(slug if duplicate_index == 0 else f"{slug}-{duplicate_index}")
    return anchors
