"""Guard the shrinking Ruff complexity grandfather list."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPLEXITY_CODES = frozenset({"C901", "PLR0912", "PLR0915"})
BASELINE_PATH = REPO_ROOT / "tests" / "ratchets" / "complexity-baseline.json"


def _complexity_ignores() -> set[tuple[str, str]]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    per_file_ignores = pyproject["tool"]["ruff"]["lint"]["per-file-ignores"]

    return {
        (path, code)
        for path, codes in per_file_ignores.items()
        for code in codes
        if code in COMPLEXITY_CODES
    }


@pytest.fixture(scope="module")
def complexity_violations() -> list[dict[str, object]]:
    expected = _complexity_ignores()
    paths = sorted({path for path, _code in expected})
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--isolated",
            # One-shot probe: avoid writing .ruff_cache into the repo root,
            # which would race with checkout-hygiene assertions elsewhere.
            "--no-cache",
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
    return json.loads(result.stdout)


def test_complexity_grandfather_entries_match_current_violations(
    complexity_violations: list[dict[str, object]],
) -> None:
    expected = _complexity_ignores()
    actual = {
        (
            Path(violation["filename"]).resolve().relative_to(REPO_ROOT.resolve()).as_posix(),
            violation["code"],
        )
        for violation in complexity_violations
    }
    stale = sorted(expected - actual)

    assert not stale, "Remove stale Ruff complexity ignores from pyproject.toml: " + ", ".join(
        f"{path} ({code})" for path, code in stale
    )


def test_complexity_violations_match_reviewed_function_fingerprints(
    complexity_violations: list[dict[str, object]],
    pytestconfig: pytest.Config,
) -> None:
    actual = _complexity_fingerprints(complexity_violations)
    if pytestconfig.getoption("--update-baseline"):
        rationale = _required_rationale(pytestconfig)
        data = {
            "version": 1,
            "rationale": rationale,
            "counts": dict(sorted(Counter(code for _path, code, _name in actual).items())),
            "entries": ["\t".join(item) for item in sorted(actual)],
        }
        BASELINE_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return

    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    expected = {tuple(str(item).split("\t")) for item in baseline["entries"]}
    assert baseline["counts"] == dict(
        sorted(Counter(code for _path, code, _name in expected).items())
    )
    added = sorted(actual - expected)
    removed = sorted(expected - actual)
    assert not added and not removed, (
        _format_complexity_delta(added, removed)
        + "\nUse --update-baseline --baseline-rationale 'reviewed reason' only for an "
        "intentional inventory change."
    )


def test_complexity_fingerprint_distinguishes_functions_in_one_ignored_file() -> None:
    tree = ast.parse(
        "class Owner:\n"
        "    def grandfathered(self):\n"
        "        pass\n"
        "    def newly_complex(self):\n"
        "        pass\n"
    )

    assert _qualname_at_line(tree, 2) == "Owner.grandfathered"
    assert _qualname_at_line(tree, 4) == "Owner.newly_complex"


def _complexity_fingerprints(
    violations: list[dict[str, object]],
) -> set[tuple[str, str, str]]:
    trees: dict[str, ast.Module] = {}
    fingerprints: set[tuple[str, str, str]] = set()
    for violation in violations:
        path = (
            Path(str(violation["filename"])).resolve().relative_to(REPO_ROOT.resolve()).as_posix()
        )
        tree = trees.get(path)
        if tree is None:
            tree = ast.parse((REPO_ROOT / path).read_text(encoding="utf-8"), filename=path)
            trees[path] = tree
        location = violation["location"]
        assert isinstance(location, dict)
        line = int(location["row"])
        fingerprints.add((path, str(violation["code"]), _qualname_at_line(tree, line)))
    return fingerprints


def _qualname_at_line(tree: ast.Module, line: int) -> str:
    visitor = _FunctionAtLine(line)
    visitor.visit(tree)
    assert visitor.qualname is not None, f"Ruff complexity violation at unowned line {line}"
    return visitor.qualname


class _FunctionAtLine(ast.NodeVisitor):
    def __init__(self, line: int) -> None:
        self.line = line
        self.names: list[str] = []
        self.qualname: str | None = None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.names.append(node.name)
        self.generic_visit(node)
        self.names.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.names.append(node.name)
        if node.lineno == self.line:
            self.qualname = ".".join(self.names)
        self.generic_visit(node)
        self.names.pop()


def _required_rationale(pytestconfig: pytest.Config) -> str:
    rationale = pytestconfig.getoption("--baseline-rationale")
    if not isinstance(rationale, str) or not rationale.strip():
        pytest.fail("--update-baseline requires a non-empty --baseline-rationale")
    return rationale.strip()


def _format_complexity_delta(
    added: list[tuple[str, str, str]],
    removed: list[tuple[str, str, str]],
) -> str:
    sections: list[str] = []
    if added:
        sections.append(
            "new complexity violations:\n  "
            + "\n  ".join(f"{path} [{code}] {name}" for path, code, name in added)
        )
    if removed:
        sections.append(
            "removed complexity baseline entries:\n  "
            + "\n  ".join(f"{path} [{code}] {name}" for path, code, name in removed)
        )
    return "\n".join(sections)
