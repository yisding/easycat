#!/usr/bin/env python3
"""Emit the nightly extras install matrix from ``[project.optional-dependencies]``.

The nightly ``extras-plan`` job runs this script and feeds its single output
line into a ``fromJSON`` GitHub Actions matrix, so every extra declared in
``pyproject.toml`` is install-tested automatically — adding a new extra adds a
matrix cell with no workflow edit.

Deliberate exclusions live in :data:`EXCLUDED_EXTRAS` with the reason recorded
next to each name; an exclusion that no longer matches a declared extra is
caught by ``tests/test_extras_matrix.py``.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Extras deliberately left out of the nightly install matrix.
EXCLUDED_EXTRAS: dict[str, str] = {
    # Decision (2026-06-09): TEN VAD ships under a non-permissive license and
    # the project deliberately does not vendor its binaries. Installing it in
    # project CI would make the project itself — not an opting-in end user —
    # accept those license terms nightly, so install coverage stays an
    # end-user opt-in. Revisit if the upstream license changes.
    "ten-vad": "non-permissive license; install acceptance stays an end-user opt-in",
}


def optional_extras(pyproject_path: Path | None = None) -> list[str]:
    """Every extra declared in ``[project.optional-dependencies]``."""
    path = pyproject_path or REPO_ROOT / "pyproject.toml"
    pyproject = tomllib.loads(path.read_text(encoding="utf-8"))
    return list(pyproject["project"]["optional-dependencies"])


def planned_extras(pyproject_path: Path | None = None) -> list[str]:
    """Extras the nightly matrix install-tests (declared minus documented exclusions)."""
    return [extra for extra in optional_extras(pyproject_path) if extra not in EXCLUDED_EXTRAS]


def main() -> None:
    # ``key=value`` shape so the workflow can append straight to $GITHUB_OUTPUT.
    print(f"extras={json.dumps(planned_extras())}")


if __name__ == "__main__":
    main()
