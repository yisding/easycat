from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "easycat"
PLANNING_LABEL_RE = re.compile(r"\b(?:WS\d+[A-Z]?|AC\d+(?:\.\d+)?|T\d+(?:\.\d+)?)\b|workstream-")


def test_library_source_does_not_reference_internal_planning_labels() -> None:
    """Keep maintainer-facing source comments tied to behavior, not old plans."""
    stale: list[str] = []

    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PLANNING_LABEL_RE.search(line):
                stale.append(f"{rel}:{line_number}: {line.strip()}")

    assert not stale, "Library source contains stale planning labels:\n" + "\n".join(stale)
