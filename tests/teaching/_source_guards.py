"""Shared drift guards for pedagogically duplicated teaching scripts.

Several chapters intentionally copy the same coordinator/cleanup code so each
lesson stays self-contained. These helpers keep those copies in lockstep by
asserting that every copy still contains the load-bearing substrings (and, when
relevant, avoids the substrings that mark a stale copy) without re-running each
one. Behavioral proof of the copied logic lives in the per-chapter runtime
tests; this only guards against silent copy drift.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def assert_sources_match(
    paths: Iterable[Path],
    *,
    required: Iterable[str] = (),
    forbidden: Iterable[str] = (),
    label: str = "Teaching script copies",
) -> None:
    """Assert every path contains all ``required`` and none of the ``forbidden`` substrings."""
    required = tuple(required)
    forbidden = tuple(forbidden)
    stale: list[str] = []
    for path in paths:
        source = Path(path).read_text(encoding="utf-8")
        if any(needle not in source for needle in required) or any(
            needle in source for needle in forbidden
        ):
            stale.append(Path(path).relative_to(ROOT).as_posix())
    assert not stale, f"{label} drifted in: " + ", ".join(stale)
