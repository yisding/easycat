from __future__ import annotations

import re
import subprocess
import tomllib
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
BUNDLED_SMART_TURN_RE = re.compile(r"smart-turn-v(?P<version>[0-9.]+)-cpu\.onnx")
CURRENT_PLAN_SOURCE_TEST_PATH_RE = re.compile(
    r"`(?P<path>(?:src/easycat|tests)/[A-Za-z0-9_./-]+\.py)"
    r"(?::(?P<line>[0-9]+(?:-[0-9]+)?))?"
    r"(?:::[A-Za-z_][A-Za-z0-9_]*)?`"
)
SOURCE_LINE_REF_RE = re.compile(
    r"\b(?:src/easycat/|tests/|docs/)?[A-Za-z0-9_./-]+\.py:[0-9]+(?:-[0-9]+)?\b"
)


def _tracked_file_count(*patterns: str) -> int:
    result = subprocess.run(
        ["git", "ls-files", *patterns],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return len([line for line in result.stdout.splitlines() if line])


def _line_count(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def test_gitignore_covers_local_generated_state() -> None:
    """Keep contributor-local automation and cache state out of routine git status."""
    patterns = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    for pattern in (
        ".hypothesis/",
        ".mypy_cache/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".uv-cache/",
        ".agents/",
        ".codex",
        ".codex/",
        ".claude/",
    ):
        assert pattern in patterns


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


def test_current_tests_and_docs_avoid_brittle_source_line_refs() -> None:
    """Use names or symbols for current maintainer references, not stale line numbers."""
    roots = (
        SOURCE_ROOT,
        REPO_ROOT / "tests",
        REPO_ROOT / "docs",
    )
    root_files = (
        REPO_ROOT / "README.md",
        REPO_ROOT / "AGENTS.md",
        REPO_ROOT / "CLAUDE.md",
        REPO_ROOT / "CONTRIBUTING.md",
    )
    stale: list[str] = []

    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.suffix not in {".py", ".md"}:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if SOURCE_LINE_REF_RE.search(line):
                    stale.append(f"{rel}:{line_number}: {line.strip()}")
    for path in root_files:
        rel = path.relative_to(REPO_ROOT).as_posix()
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if SOURCE_LINE_REF_RE.search(line):
                stale.append(f"{rel}:{line_number}: {line.strip()}")

    assert not stale, "Current tests/docs should not use brittle source line refs:\n" + "\n".join(
        stale
    )


def test_current_peripheral_plans_reference_current_config_and_tts_alignment_names() -> None:
    """Keep current peripheral docs aligned with the landed config package layout."""
    files = (
        REPO_ROOT / "plan" / "peripherals" / "peripheral-cartesia-provider.md",
        REPO_ROOT / "plan" / "peripherals" / "peripheral-telephony-tts-output.md",
        REPO_ROOT / "plan" / "peripherals" / "peripheral-eval-and-debugger-ui.md",
    )
    texts = {path: path.read_text(encoding="utf-8") for path in files}
    combined = "\n".join(texts.values())
    missing_paths: list[str] = []
    brittle_line_refs: list[str] = []

    for current_phrase in (
        "src/easycat/config/easy.py",
        "src/easycat/config/_factory.py",
        "src/easycat/config/_tts_alignment.py",
        "preferred_tts_output_format",
        "EasyConfig(record_to=",
    ):
        assert current_phrase in combined

    for stale_phrase in (
        "src/easycat/config.py",
        "`config.py`",
        "`preferred_tts_format`",
        "preferred_tts_format:",
        "transport.preferred_tts_format",
        "TwilioTransport.preferred_tts_format",
        "TransportFormatApplied",
        "EasyCatConfig",
    ):
        assert stale_phrase not in combined

    for path, text in texts.items():
        for line_number, line in enumerate(text.splitlines(), 1):
            for match in CURRENT_PLAN_SOURCE_TEST_PATH_RE.finditer(line):
                path_text = match.group("path")
                if match.group("line") is not None:
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    brittle_line_refs.append(f"{rel}:{line_number}: `{match.group(0).strip('`')}`")
                if not (REPO_ROOT / path_text).exists():
                    rel = path.relative_to(REPO_ROOT).as_posix()
                    missing_paths.append(f"{rel}:{line_number}: `{path_text}`")

    assert not missing_paths, "Current peripheral docs reference missing files:\n" + "\n".join(
        missing_paths
    )
    assert not brittle_line_refs, (
        "Current peripheral docs should use stable symbol refs, not file-line refs:\n"
        + "\n".join(brittle_line_refs)
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


def test_cli_test_plan_describes_integration_local_marker_selection() -> None:
    """Keep integration_local docs aligned with pytest's actual marker behavior."""
    plan = (REPO_ROOT / "tests" / "cli" / "TEST_PLANS.md").read_text(encoding="utf-8")
    packaging_source = (REPO_ROOT / "tests" / "cli" / "test_packaging.py").read_text(
        encoding="utf-8"
    )
    combined = "\n".join((plan, packaging_source))
    normalized_plan = " ".join(plan.split())

    assert "bare `pytest` still collects them unless the caller supplies a marker expression" in (
        normalized_plan
    )
    assert "bare pytest still collects it unless the caller supplies a marker expression" in (
        packaging_source
    )
    for stale_phrase in (
        "run in CI but not on every `pytest` invocation",
        "Skipped by default to keep the fast test suite fast",
    ):
        assert stale_phrase not in combined


def test_roadmap_current_code_status_tracks_inventory_and_artifact_hygiene() -> None:
    """Keep the current-code snapshot aligned with tracked files and release hygiene."""
    from easycat._public_api import LAZY_EXPORTS

    status = (REPO_ROOT / "plan" / "roadmap" / "current-code-status.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(status.split())
    source_count = _tracked_file_count("src/easycat/**/*.py", "src/easycat/*.py")
    test_count = _tracked_file_count("tests/**/test_*.py", "tests/test_*.py")
    session_lines = _line_count(REPO_ROOT / "src" / "easycat" / "session" / "_session.py")
    init_lines = _line_count(REPO_ROOT / "src" / "easycat" / "__init__.py")

    assert f"`src/easycat/` contains {source_count} tracked Python files." in status
    assert f"`tests/` contains {test_count} tracked `test_*.py` files." in status
    assert (
        f"`Session` is reduced from the older cleanup note but still large at roughly "
        f"{session_lines:,} lines."
    ) in normalized
    assert (
        f"`src/easycat/__init__.py` is smaller than the older cleanup note at roughly "
        f"{init_lines:,} lines"
    ) in normalized
    assert (
        f"The public surface is still broad at {len(LAZY_EXPORTS)} lazy top-level exports"
        in normalized
    )
    assert "cache/workspace artifacts" not in status
    assert "local/generated/secret artifacts leaking into release artifacts" in status


def test_current_status_bridge_docs_track_roadmap_snapshot_counts() -> None:
    """Keep current-facing planning summaries aligned with the canonical snapshot."""
    session_lines = _line_count(REPO_ROOT / "src" / "easycat" / "session" / "_session.py")
    init_lines = _line_count(REPO_ROOT / "src" / "easycat" / "__init__.py")
    session_lines_text = f"{session_lines:,}"
    init_lines_text = f"{init_lines:,}"
    files = {
        "combined": REPO_ROOT / "plan" / "roadmap" / "combined-cleanup-tasks.md",
        "workstreams": REPO_ROOT / "plan" / "workstreams" / "README.md",
        "workstream-3": REPO_ROOT / "plan" / "workstreams" / "workstream-3-stage-refactor.md",
        "session-index": REPO_ROOT / "plan" / "session-decomposition" / "README.md",
        "session-overview": REPO_ROOT
        / "plan"
        / "session-decomposition"
        / "session-decomp-overview.md",
    }
    texts = {name: path.read_text(encoding="utf-8") for name, path in files.items()}
    normalized = {name: " ".join(text.split()) for name, text in texts.items()}

    assert f"roughly {session_lines_text} lines, not 2,961" in normalized["combined"]
    assert (
        f"`src/easycat/__init__.py` is now {init_lines_text} lines, not 578." in texts["combined"]
    )
    assert f"roughly {session_lines_text} lines" in normalized["workstreams"]
    assert f"{session_lines_text} lines in the 2026-06-07 snapshot" in normalized["workstream-3"]
    assert f"roughly {session_lines_text} lines" in normalized["session-index"]
    assert (
        f"roughly {session_lines_text} lines in the current snapshot"
        in normalized["session-overview"]
    )

    combined_text = "\n".join(texts.values())
    for stale_phrase in (
        "2026-06-05 snapshot",
        "Static inspection on 2026-05-21",
        "roughly 1,358 lines",
        "roughly 1,773 lines",
        "`src/easycat/__init__.py` is now 280 lines",
        "Session is **~1,770 lines** after Phase 5",
    ):
        assert stale_phrase not in combined_text


def test_current_plan_docs_track_bundled_smart_turn_version() -> None:
    """Keep current-facing planning docs aligned with the bundled ONNX model."""
    smart_turn_source = (SOURCE_ROOT / "smart_turn.py").read_text(encoding="utf-8")
    match = BUNDLED_SMART_TURN_RE.search(smart_turn_source)
    assert match is not None
    version = match.group("version")
    current_plan_text = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted((REPO_ROOT / "plan").glob("**/*.md"))
    )

    assert f"Smart Turn v{version}" in current_plan_text
    for stale_phrase in (
        "Smart Turn v3.1 promotion",
        "Smart Turn v3.1 wiring",
        "Smart Turn v3.1 runs on CPU",
        "Turn detection: Smart Turn v3.1",
        "Smart Turn v3.1 + Kyutai",
        "Why Smart Turn v3.1",
        "Pipecat Smart Turn v3.1",
        "Smart Turn v3.1 already combines",
        "Smart Turn v3.1 (12ms CPU)",
    ):
        assert stale_phrase not in current_plan_text


def test_peripheral_plans_use_current_config_surface_name() -> None:
    """Keep peripheral backlog docs aligned with the current EasyConfig API."""
    stale: list[str] = []

    for plan_path in sorted((REPO_ROOT / "plan" / "peripherals").glob("*.md")):
        for line_number, line in enumerate(plan_path.read_text(encoding="utf-8").splitlines(), 1):
            if "EasyCatConfig" in line:
                stale.append(f"{plan_path.relative_to(REPO_ROOT)}:{line_number}: {line.strip()}")

    assert not stale, "Peripheral plans should say EasyConfig, not EasyCatConfig:\n" + "\n".join(
        stale
    )

    langchain_plan = (
        REPO_ROOT / "plan" / "peripherals" / "peripheral-langchain-langgraph-bridge.md"
    ).read_text(encoding="utf-8")

    assert "from easycat import EasyConfig, LocalTransportConfig, create_session" in langchain_plan
    assert "EasyConfig(" in langchain_plan
    assert "EasyConfig(mcp_servers=[...])" in langchain_plan


def test_cli_test_plan_names_docs_route_map_coverage() -> None:
    """Keep the onboarding docs command visible in the CLI coverage map."""
    plan = (REPO_ROOT / "tests" / "cli" / "TEST_PLANS.md").read_text(encoding="utf-8")
    docs_plan = plan.split("## Plan 4 — `docs` route map", 1)[1].split("---", 1)[0]
    normalized_docs_plan = " ".join(docs_plan.split())

    assert (
        "| 4 | `docs` route map | "
        "`test_app.py` + `tests/test_docs_index.py` + `test_json_schema.py` |"
    ) in plan
    assert "easycat docs" in docs_plan
    assert "easycat docs --json" in docs_plan
    assert (
        "route fields, audience filter metadata, `command_note`, or online" in normalized_docs_plan
    )
    assert "exact labels with hyphen/underscore aliases" in normalized_docs_plan
    assert "broad `operators` / `maintainers` role filters" in normalized_docs_plan
    assert "reject partial fragments such as `maint` or `agent`" in normalized_docs_plan
    for field in (
        "source_url",
        "command_note",
        "audience_filter",
        "available_audiences",
        "available_audience_filters",
        "audience_alias_note",
    ):
        assert f"`{field}`" in docs_plan
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


def test_cli_test_plan_names_scaffold_artifact_hygiene() -> None:
    """Keep the scaffold plan aligned with generated-project hygiene checks."""
    plan = (REPO_ROOT / "tests" / "cli" / "TEST_PLANS.md").read_text(encoding="utf-8")
    scaffold_plan = plan.split("## Plan 5 — `init` template rendering", 1)[1].split("---", 1)[0]
    normalized = " ".join(scaffold_plan.split())

    assert "cache, build, coverage, docs, mutation, package metadata" in normalized
    assert "secret-key artifacts leaking into generated projects" in normalized
    assert ".gitignore` covers local env variants" in normalized
    assert "local agent/tool state" in normalized
    assert "local `.pem` / `.key` files" in normalized
    assert "coverage files, `.egg-info` package metadata, bytecode suffixes" in normalized
    assert "local secret suffixes" in normalized
    assert "`easycat init --json` reports only the clean generated-project manifest" in (
        normalized
    )
    assert "the real top-level `.gitignore` remains" in normalized


def test_cli_test_plan_names_packaging_artifact_hygiene() -> None:
    """Keep the packaging plan aligned with release artifact rejection checks."""
    plan = (REPO_ROOT / "tests" / "cli" / "TEST_PLANS.md").read_text(encoding="utf-8")
    packaging_plan = plan.split(
        "## Plan 16 — Packaging — wheel and sdist ship template dotfiles, metadata, "
        "and clean contents",
        1,
    )[1].split("---", 1)[0]
    normalized = " ".join(packaging_plan.split())

    assert "cache, generated report/build output, package metadata" in normalized
    assert "cache, coverage, docs, mutation, package metadata" in normalized
    assert "bytecode, or local secret-key artifacts leaking into release artifacts" in (normalized)
    assert "`uv build --wheel` and `uv build --sdist` succeed" in normalized
    assert "build, coverage, docs, mutation, VCS, virtualenv" in normalized
    assert "package metadata artifacts" in normalized
    for token in (
        "`.ruff_cache`",
        "`.uv-cache`",
        "`.agents`",
        "`.codex`",
        "`.coverage`",
        "`.egg-info`",
        "bytecode",
        "local `.pem` / `.key` files",
    ):
        assert token in packaging_plan


def test_pytest_timeout_is_configured_as_suite_safety_net() -> None:
    """Keep the roadmap aligned with the configured pytest-timeout guard."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_dependencies = pyproject["dependency-groups"]["dev"]
    pytest_options = pyproject["tool"]["pytest"]["ini_options"]
    reliability = (REPO_ROOT / "plan/roadmap/combined-cleanup-tasks.md").read_text(
        encoding="utf-8"
    )
    reliability_section = reliability.split("### 7.4 Test Reliability", 1)[1].split(
        "### 7.5 Provider And Performance Testing",
        1,
    )[0]

    assert any(dependency.startswith("pytest-timeout") for dependency in dev_dependencies)
    assert pytest_options["timeout"] == 60
    assert pytest_options["timeout_method"] == "thread"
    assert pytest_options["faulthandler_timeout"] == 55
    assert "Done: `pytest-timeout`" in reliability_section


def test_asyncio_task_leak_guard_is_configured() -> None:
    """Keep async test leak detection wired into the root pytest config."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    markers = "\n".join(pyproject["tool"]["pytest"]["ini_options"]["markers"])
    conftest = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    reliability = (REPO_ROOT / "plan/roadmap/combined-cleanup-tasks.md").read_text(
        encoding="utf-8"
    )
    reliability_section = reliability.split("### 7.4 Test Reliability", 1)[1].split(
        "### 7.5 Provider And Performance Testing",
        1,
    )[0]

    assert "allow_task_leak" in markers
    assert "@pytest_asyncio.fixture(autouse=True)" in conftest
    assert "async def fail_on_leaked_asyncio_tasks" in conftest
    assert "asyncio.all_tasks" in conftest
    assert 'pytest.fail(f"asyncio task leak detected:' in conftest
    assert "Done: async tests fail on newly leaked pending asyncio tasks" in (reliability_section)


def test_e2e_ws_server_fixture_lets_os_choose_bound_port() -> None:
    """The shared e2e WebSocket fixture should not bind-close-reuse a port."""
    source = (REPO_ROOT / "tests" / "e2e" / "conftest.py").read_text(encoding="utf-8")
    start_ws_server = source.split("async def _start_ws_server", 1)[1].split(
        "async def _stop_ws_server",
        1,
    )[0]

    assert "def find_free_port" not in source
    assert 'websockets.serve(on_connect, "127.0.0.1", 0' in start_ws_server
    assert "_bound_server_port(server)" in start_ws_server


def test_test_helper_modules_do_not_define_bind_close_port_helpers() -> None:
    """Shared helpers should not expose bind-close-reuse port helpers."""
    for path in (
        REPO_ROOT / "tests" / "integration" / "harness.py",
        REPO_ROOT / "tests" / "transports" / "conftest.py",
    ):
        source = path.read_text(encoding="utf-8")

        assert "def find_free_port" not in source


def test_focused_transport_tests_use_pytest_port_factory() -> None:
    """Keep focused transport tests off bind-close port helpers where feasible."""
    for path in (
        REPO_ROOT / "tests" / "transports" / "test_connection_transports.py",
        REPO_ROOT / "tests" / "transports" / "test_transports.py",
        REPO_ROOT / "tests" / "transports" / "test_webrtc.py",
        REPO_ROOT / "tests" / "transports" / "test_websocket_session_server.py",
        REPO_ROOT / "tests" / "transports" / "test_webtransport.py",
    ):
        source = path.read_text(encoding="utf-8")

        assert "find_free_port" not in source
        assert "unused_tcp_port_factory" in source


def test_integration_socket_tests_use_pytest_port_factory() -> None:
    """Keep migrated integration socket tests off bind-close port helpers."""
    for path in (
        REPO_ROOT / "tests" / "integration" / "test_session_lifecycle_e2e.py",
        REPO_ROOT / "tests" / "integration" / "test_twilio_session_integration.py",
        REPO_ROOT / "tests" / "integration" / "test_twilio_transport_e2e.py",
        REPO_ROOT / "tests" / "integration" / "test_websocket_session_integration.py",
        REPO_ROOT / "tests" / "integration" / "test_ws_transport_e2e.py",
    ):
        source = path.read_text(encoding="utf-8")

        assert "find_free_port" not in source
        assert "unused_tcp_port_factory" in source


def test_scaffold_smoke_ruff_uses_generated_project_config() -> None:
    """The scaffold smoke matrix should lint with the generated project's config."""
    source = (REPO_ROOT / "tests" / "cli" / "e2e" / "test_scaffold_smoke.py").read_text(
        encoding="utf-8"
    )
    test_body = source.split("def test_scaffold_python_files_pass_ruff", 1)[1]

    assert "cwd=project" in test_body
    assert "path.relative_to(project)" in test_body
    assert "str(path) for path in python_files" not in test_body


def test_peripheral_cli_package_layout_lists_top_level_cli_modules() -> None:
    """Keep the maintainer-facing CLI layout aligned with the package tree."""
    plan = (REPO_ROOT / "plan" / "peripherals" / "peripheral-cli.md").read_text(encoding="utf-8")
    layout = plan.split("## Package Layout", 1)[1].split("### Entry point", 1)[0]
    actual = sorted(path.name for path in (REPO_ROOT / "src" / "easycat" / "cli").glob("*.py"))

    missing = [filename for filename in actual if f"    {filename}" not in layout]

    assert not missing, "peripheral-cli.md package layout omits CLI modules: " + ", ".join(missing)
    assert "replay.py" not in layout
