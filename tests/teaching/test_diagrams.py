"""Verify that ASCII box-drawing diagrams in teaching READMEs are column-aligned.

A box top edge ``┌────┐`` on row R at cols (c1, c2) must have a matching
bottom edge on some row R' below at the *same* (c1, c2). Off-by-N
misalignments produce visibly broken diagrams on every monospace
renderer (GitHub desktop, mkdocs site, code editors).

T-junctions (├ ┤ ┬ ┴ ┼) are accepted in place of any corner so that
boxes with attached connectors (e.g. chapter 8's smart-turn state
diagram, chapter 15's lifecycle pipeline) still validate.

This test intentionally only checks *paired* corners — annotation
L-shapes like ``└── label`` have no matching ┌ above and are skipped.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TEACHING_DIR = REPO_ROOT / "docs" / "teaching"
FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)\n```", re.MULTILINE | re.DOTALL)

TL_CHARS = "┌├"
TR_CHARS = "┐┤"
BL_CHARS = "└├┴"
BR_CHARS = "┘┤┴"
H_LINE = "─"


def _find_box_bugs(block: str) -> list[str]:
    """Return human-readable descriptions of every box-corner misalignment."""
    lines = block.split("\n")
    bugs: list[str] = []

    for r, line in enumerate(lines):
        for c, ch in enumerate(line):
            if ch not in TL_CHARS:
                continue
            # Find candidate top-right on same row, separated only by ─.
            c2 = c + 1
            while c2 < len(line) and line[c2] == H_LINE:
                c2 += 1
            if c2 >= len(line) or line[c2] not in TR_CHARS:
                continue
            # Look downward for the matching bottom edge. The first row
            # below the top edge that contains *any* bottom-corner candidate
            # is the row we evaluate — either it matches cleanly or it's
            # misaligned.
            for r2 in range(r + 1, len(lines)):
                ln2 = lines[r2]
                bl_cols = [j for j, ch3 in enumerate(ln2) if ch3 in BL_CHARS]
                br_cols = [j for j, ch3 in enumerate(ln2) if ch3 in BR_CHARS]
                if not bl_cols and not br_cols:
                    continue  # No bottom corners on this row; keep looking.
                left_ok = c in bl_cols
                right_ok = c2 in br_cols
                if left_ok and right_ok:
                    break  # Matched cleanly.
                # Find the nearest off-column candidate on this row, on the
                # plausible side (BL columns near c, BR columns near c2).
                near_bl = [j for j in bl_cols if abs(j - c) <= 5 and j != c]
                near_br = [j for j in br_cols if abs(j - c2) <= 5 and j != c2]
                if not left_ok and near_bl:
                    j = min(near_bl, key=lambda x: abs(x - c))
                    bugs.append(
                        f"box top {TL_CHARS[0]}@col{c + 1}..{TR_CHARS[0]}@col{c2 + 1} "
                        f"on row {r + 1}: bottom-left corner found at "
                        f"col {j + 1} (off by {j - c}) on row {r2 + 1}"
                    )
                if not right_ok and near_br:
                    j = min(near_br, key=lambda x: abs(x - c2))
                    bugs.append(
                        f"box top {TL_CHARS[0]}@col{c + 1}..{TR_CHARS[0]}@col{c2 + 1} "
                        f"on row {r + 1}: bottom-right corner found at "
                        f"col {j + 1} (off by {j - c2}) on row {r2 + 1}"
                    )
                break  # We've evaluated the first plausible bottom row.
    return bugs


def _check_internal_consistency(block: str) -> list[str]:
    """Verify each box's mid rows have │ at the same columns as the corners.

    A box at cols (c1, c2) between rows r..r' must have ``│`` (or ``├``/``┤``)
    at cols c1 and c2 on every row strictly between r and r'. This catches
    the class of bug where a mid-row label is one column too long or short
    (the chapter 4 / chapter 8 patterns).
    """
    lines = block.split("\n")
    bugs: list[str] = []
    sides = set("│├┤┌┐└┘┴┬")

    for r, line in enumerate(lines):
        for c, ch in enumerate(line):
            if ch not in TL_CHARS:
                continue
            c2 = c + 1
            while c2 < len(line) and line[c2] == H_LINE:
                c2 += 1
            if c2 >= len(line) or line[c2] not in TR_CHARS:
                continue
            # Find matching bottom row (must be at exact cols c, c2).
            r2 = None
            for rr in range(r + 1, len(lines)):
                ln2 = lines[rr]
                left = ln2[c] if c < len(ln2) else " "
                right = ln2[c2] if c2 < len(ln2) else " "
                if left in BL_CHARS and right in BR_CHARS:
                    r2 = rr
                    break
            if r2 is None:
                continue
            # Check intermediate rows.
            for rm in range(r + 1, r2):
                ln = lines[rm]
                left = ln[c] if c < len(ln) else " "
                right = ln[c2] if c2 < len(ln) else " "
                if left not in sides:
                    bugs.append(
                        f"box at rows {r + 1}-{r2 + 1}, cols {c + 1}-{c2 + 1}: "
                        f"row {rm + 1} col {c + 1} is {left!r}, expected vertical bar"
                    )
                if right not in sides:
                    bugs.append(
                        f"box at rows {r + 1}-{r2 + 1}, cols {c + 1}-{c2 + 1}: "
                        f"row {rm + 1} col {c2 + 1} is {right!r}, expected vertical bar"
                    )
    return bugs


def _diagrams_in(path: Path):
    text = path.read_text()
    for m in FENCE_RE.finditer(text):
        block = m.group(1)
        if any(c in block for c in "┌┐└┘"):
            start_line = text[: m.start()].count("\n") + 2
            yield start_line, block


@pytest.mark.parametrize(
    "readme",
    sorted(TEACHING_DIR.glob("*/README.md")),
    ids=lambda p: p.parent.name,
)
def test_chapter_diagrams_are_column_aligned(readme: Path) -> None:
    failures: list[str] = []
    for start_line, block in _diagrams_in(readme):
        for bug in _find_box_bugs(block):
            failures.append(f"  L{start_line}+ {bug}")
        for bug in _check_internal_consistency(block):
            failures.append(f"  L{start_line}+ {bug}")
    if failures:
        rel = readme.relative_to(REPO_ROOT)
        pytest.fail(
            f"\n{rel} has misaligned diagram(s):\n" + "\n".join(failures),
            pytrace=False,
        )


def test_top_level_readme_diagrams_are_column_aligned() -> None:
    readme = TEACHING_DIR / "README.md"
    failures: list[str] = []
    for start_line, block in _diagrams_in(readme):
        for bug in _find_box_bugs(block):
            failures.append(f"  L{start_line}+ {bug}")
        for bug in _check_internal_consistency(block):
            failures.append(f"  L{start_line}+ {bug}")
    if failures:
        pytest.fail(
            "docs/teaching/README.md has misaligned diagram(s):\n" + "\n".join(failures),
            pytrace=False,
        )
