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
TEST_PLAN_TABLE_ROW_RE = re.compile(
    r"^\| (?P<number>\d+) \| (?P<title>.+?) \| (?P<backing>.+?) \|$",
    re.MULTILINE,
)
TEST_PLAN_HEADING_RE = re.compile(r"^## Plan (?P<number>\d+) — (?P<title>.+)$", re.MULTILINE)
STALE_TEST_PLAN_COUNT_RE = re.compile(r"\([0-9]+(?: [A-Za-z-]+)? tests?\)")
STALE_TEST_PLAN_PHRASES = ("M1 checks",)


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


def test_cli_test_plan_table_matches_plan_sections() -> None:
    """Keep the summary table and detailed CLI plan sections in lockstep."""
    plan = (REPO_ROOT / "tests" / "cli" / "TEST_PLANS.md").read_text(encoding="utf-8")
    table = {
        int(match.group("number")): match.group("title")
        for match in TEST_PLAN_TABLE_ROW_RE.finditer(plan)
    }
    headings = {
        int(match.group("number")): match.group("title")
        for match in TEST_PLAN_HEADING_RE.finditer(plan)
    }
    expected_numbers = list(range(1, len(table) + 1))

    assert list(table) == expected_numbers
    assert table == headings

    missing_backing: list[str] = []
    heading_matches = list(TEST_PLAN_HEADING_RE.finditer(plan))
    for index, match in enumerate(heading_matches):
        if index + 1 < len(heading_matches):
            next_start = heading_matches[index + 1].start()
        else:
            next_start = len(plan)
        section = plan[match.start() : next_start]
        if "**Backed by.**" not in section:
            missing_backing.append(f"Plan {match.group('number')} — {match.group('title')}")

    assert not missing_backing, "CLI test plan sections missing backing tests: " + ", ".join(
        missing_backing
    )


def test_cli_test_plan_avoids_brittle_test_count_claims() -> None:
    """Coverage plans should name files and behaviors, not stale numeric counts."""
    plan = REPO_ROOT / "tests" / "cli" / "TEST_PLANS.md"
    stale: list[str] = []

    for line_number, line in enumerate(plan.read_text(encoding="utf-8").splitlines(), 1):
        if STALE_TEST_PLAN_COUNT_RE.search(line) or any(
            phrase in line for phrase in STALE_TEST_PLAN_PHRASES
        ):
            stale.append(f"{plan.relative_to(REPO_ROOT)}:{line_number}: {line.strip()}")

    assert not stale, "CLI test plan contains brittle stale-count language:\n" + "\n".join(stale)


def test_cli_test_plan_names_docs_route_map_coverage() -> None:
    """Keep the onboarding docs command visible in the CLI coverage map."""
    plan = (REPO_ROOT / "tests" / "cli" / "TEST_PLANS.md").read_text(encoding="utf-8")
    docs_plan = plan.split("## Plan 4 — `docs` route map", 1)[1].split("---", 1)[0]
    normalized_docs_plan = " ".join(docs_plan.split())

    assert "| 4 | `docs` route map | `test_app.py` + `tests/test_docs_index.py` |" in plan
    assert "easycat docs" in docs_plan
    assert "easycat docs --json" in docs_plan
    assert "`audience`, `commands`, `command_note`, or online" in docs_plan
    assert "parseable doctor/schema/validation-report commands" in normalized_docs_plan
    assert "Provider contract routes" in docs_plan
    assert "tests/contracts/README.md" in docs_plan
    assert "test_app.py" in docs_plan
    assert "tests/test_docs_index.py" in docs_plan


def test_cli_test_plan_names_validation_json_lanes() -> None:
    """Keep the CLI JSON plan aligned with the public validation JSON lanes."""
    plan = (REPO_ROOT / "tests" / "cli" / "TEST_PLANS.md").read_text(encoding="utf-8")
    json_plan = plan.split("## Plan 12 — JSON envelope stability", 1)[1].split("---", 1)[0]
    validate_plan = plan.split("## Plan 13 — `validate` command and report rendering", 1)[1].split(
        "---", 1
    )[0]

    for command in (
        "validate quick",
        "validate contracts",
        "validate release",
        "validate report",
    ):
        assert command in json_plan

    for command in (
        "easycat validate quick --json",
        "easycat validate contracts --json",
        "easycat validate release --json",
        "easycat validate report .easycat/validation/latest.json --json",
    ):
        assert command in validate_plan

    assert "test_validate.py" in validate_plan
    assert "command-specific CLI suites" in json_plan


def test_peripheral_cli_package_layout_lists_top_level_cli_modules() -> None:
    """Keep the maintainer-facing CLI layout aligned with the package tree."""
    plan = (REPO_ROOT / "plan" / "peripherals" / "peripheral-cli.md").read_text(encoding="utf-8")
    layout = plan.split("## Package Layout", 1)[1].split("### Entry point", 1)[0]
    actual = sorted(path.name for path in (REPO_ROOT / "src" / "easycat" / "cli").glob("*.py"))

    missing = [filename for filename in actual if f"    {filename}" not in layout]

    assert not missing, "peripheral-cli.md package layout omits CLI modules: " + ", ".join(missing)
    assert "replay.py" not in layout
