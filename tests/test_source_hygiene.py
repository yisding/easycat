from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "src" / "easycat"
PLANNING_LABEL_RE = re.compile(r"\b(?:WS\d+[A-Z]?|AC\d+(?:\.\d+)?|T\d+(?:\.\d+)?)\b|workstream-")
REMOVED_CONFIG_MODULE_RE = re.compile(r"\bconfig\.py\b")
STALE_ASYNC_CONTEXT_TEARDOWN_RE = re.compile(
    r"__aexit__`? runs shutdown\(\)|shutdown\(\) uses the real path"
)
TEST_PLAN_TEST_REF_RE = re.compile(
    r"`(?P<ref>(?:tests/)?[A-Za-z0-9_./-]*test_[A-Za-z0-9_./-]+\.py(?:::[A-Za-z0-9_]+)?)`"
)


def test_library_source_does_not_reference_internal_planning_labels() -> None:
    """Keep maintainer-facing source comments tied to behavior, not old plans."""
    stale: list[str] = []

    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PLANNING_LABEL_RE.search(line):
                stale.append(f"{rel}:{line_number}: {line.strip()}")

    assert not stale, "Library source contains stale planning labels:\n" + "\n".join(stale)


def test_library_source_references_config_package_not_removed_module() -> None:
    """The config surface is a package now; comments should not teach ``config.py``."""
    stale: list[str] = []

    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if REMOVED_CONFIG_MODULE_RE.search(line):
                stale.append(f"{rel}:{line_number}: {line.strip()}")

    assert not stale, "Library source should reference config/, not config.py:\n" + "\n".join(
        stale
    )


def test_session_context_tests_describe_force_stop_teardown() -> None:
    """Keep direct async-context tests aligned with Session.__aexit__."""
    path = REPO_ROOT / "tests" / "session" / "test_async_context_manager.py"
    stale: list[str] = []

    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if STALE_ASYNC_CONTEXT_TEARDOWN_RE.search(line):
            stale.append(f"{path.relative_to(REPO_ROOT)}:{line_number}: {line.strip()}")

    assert not stale, "Async context tests should describe stop(force=True):\n" + "\n".join(stale)


def test_cli_test_plan_references_existing_test_files() -> None:
    """Keep the CLI test plan anchored to files that actually exist."""
    plan = REPO_ROOT / "tests" / "cli" / "TEST_PLANS.md"
    missing: list[str] = []
    for match in TEST_PLAN_TEST_REF_RE.finditer(plan.read_text(encoding="utf-8")):
        ref = match.group("ref")
        path_text = ref.split("::", 1)[0]
        if path_text.startswith("tests/"):
            candidates = [REPO_ROOT / path_text]
        else:
            candidates = sorted((REPO_ROOT / "tests" / "cli").rglob(path_text))
        if not any(candidate.exists() for candidate in candidates):
            missing.append(ref)

    assert not missing, "CLI test plan references missing test files: " + ", ".join(missing)
