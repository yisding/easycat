"""Guard the shrinking Ruff complexity grandfather list."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPLEXITY_CODES = frozenset({"C901", "PLR0912", "PLR0915"})


def _complexity_ignores() -> set[tuple[str, str]]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    per_file_ignores = pyproject["tool"]["ruff"]["lint"]["per-file-ignores"]

    return {
        (path, code)
        for path, codes in per_file_ignores.items()
        for code in codes
        if code in COMPLEXITY_CODES
    }


def test_complexity_grandfather_entries_match_current_violations() -> None:
    expected = _complexity_ignores()
    paths = sorted({path for path, _code in expected})
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--isolated",
            "--select",
            ",".join(sorted(COMPLEXITY_CODES)),
            "--output-format",
            "json",
            *paths,
        ],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode in {0, 1}, result.stderr
    violations = json.loads(result.stdout)
    actual = {
        (
            Path(violation["filename"]).resolve().relative_to(REPO_ROOT.resolve()).as_posix(),
            violation["code"],
        )
        for violation in violations
    }
    stale = sorted(expected - actual)

    assert not stale, "Remove stale Ruff complexity ignores from pyproject.toml: " + ", ".join(
        f"{path} ({code})" for path, code in stale
    )
