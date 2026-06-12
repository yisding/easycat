from __future__ import annotations

from tests.docs._docs_index_helpers import (
    _DOCS_COMMAND_NOTE,
    ANGLE_PLACEHOLDER_RE,
    _cli_docs_command_hint_problems,
    _docs_entries,
    shlex,
)


def test_cli_docs_command_hint_validator_checks_nested_easycat_commands() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken nested hints",
                "path": "README.md#cli",
                "audience": "contributors",
                "description": "Regression fixture for nested command validation.",
                "commands": (
                    "uv run easycat validate not-a-lane",
                    "easycat bundles not-a-bundle-command",
                ),
            }
        ]
    )

    assert "Broken nested hints: unknown easycat validate command not-a-lane" in problems
    assert "Broken nested hints: unknown easycat bundles command not-a-bundle-command" in problems


def test_cli_docs_command_hint_validator_checks_pytest_node_ids() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken pytest hint",
                "path": "docs/validation.md",
                "audience": "contributors",
                "description": "Regression fixture for pytest node validation.",
                "commands": (
                    "uv run pytest tests/docs/test_route_registry.py::missing_test "
                    "tests/docs/test_route_registry_missing.py",
                ),
            }
        ]
    )

    assert (
        "Broken pytest hint: missing pytest node tests/docs/test_route_registry.py::missing_test"
        in (problems)
    )
    assert (
        "Broken pytest hint: missing pytest target tests/docs/test_route_registry_missing.py"
        in (problems)
    )


def test_cli_docs_command_hint_validator_checks_tool_path_targets() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken tool path hints",
                "path": "CONTRIBUTING.md#the-development-loop",
                "audience": "contributors",
                "description": "Regression fixture for maintenance command target validation.",
                "commands": (
                    "uv run ruff check missing.py",
                    "uv run mypy missing/package",
                    "uvx ty check missing/package",
                ),
            }
        ]
    )

    assert "Broken tool path hints: missing ruff check target missing.py" in problems
    assert "Broken tool path hints: missing mypy target missing/package" in problems
    assert "Broken tool path hints: missing ty check target missing/package" in problems


def test_cli_docs_command_hint_validator_checks_docs_audience_filters() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken docs audience hints",
                "path": "docs/README.md",
                "audience": "all readers",
                "description": "Regression fixture for docs audience validation.",
                "commands": (
                    "uv run easycat docs --audience time-travelers",
                    "easycat docs --audience",
                    "easycat docs --audience --json",
                ),
            }
        ]
    )

    assert "Broken docs audience hints: unknown docs audience hint time-travelers" in problems
    assert problems.count("Broken docs audience hints: docs audience hint missing value") == 2


def test_cli_docs_command_hint_validator_checks_doctor_env_file_values() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken doctor env-file hints",
                "path": "README.md#cli",
                "audience": "app builders",
                "description": "Regression fixture for doctor env-file validation.",
                "commands": (
                    "easycat doctor --env-file --json",
                    "uv run easycat doctor --env-file --json",
                    "easycat doctor --env-file=missing-dir/.env",
                ),
            }
        ]
    )

    assert (
        problems.count("Broken doctor env-file hints: easycat doctor env-file hint missing value")
        == 2
    )
    assert (
        "Broken doctor env-file hints: missing easycat doctor env-file directory missing-dir/.env"
    ) in problems


def test_cli_docs_command_hint_validator_checks_uv_run_env_file_values() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken uv run env-file hints",
                "path": "README.md#install",
                "audience": "new users",
                "description": "Regression fixture for uv run env-file validation.",
                "commands": (
                    "uv run --env-file .env python missing_script.py",
                    "uv run --env-file --isolated python examples/openai_agents_voice.py",
                    "uv run --env-file=missing-dir/.env python examples/openai_agents_voice.py",
                    "uv run --env-file .env",
                ),
            }
        ]
    )

    assert "Broken uv run env-file hints: missing python script missing_script.py" in problems
    assert "Broken uv run env-file hints: uv run env-file hint missing value" in problems
    assert "Broken uv run env-file hints: unsupported uv run option --isolated" in problems
    assert (
        "Broken uv run env-file hints: missing uv run env-file directory missing-dir/.env"
        in problems
    )
    assert "Broken uv run env-file hints: missing uv run command" in problems


def test_cli_docs_command_hint_validator_checks_uvicorn_targets() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken uvicorn hints",
                "path": "examples/README.md",
                "audience": "app builders",
                "description": "Regression fixture for ASGI example command validation.",
                "commands": (
                    "uv run uvicorn examples.missing:create_app --factory",
                    "uv run uvicorn :create_app --factory",
                ),
            }
        ]
    )

    assert (
        "Broken uvicorn hints: missing uvicorn module target examples.missing:create_app"
        in problems
    )
    assert "Broken uvicorn hints: missing uvicorn module target" in problems


def test_cli_docs_command_hint_validator_checks_uv_sync_extras() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken uv sync hints",
                "path": "README.md#install",
                "audience": "new users",
                "description": "Regression fixture for optional dependency validation.",
                "commands": (
                    "uv sync --extra not-a-real-extra --group dev",
                    "uv sync --extra=another-fake-extra --group dev",
                    "uv sync --extra",
                    "uv sync --extra --group dev",
                ),
            }
        ]
    )

    assert "Broken uv sync hints: unknown uv sync extra not-a-real-extra" in problems
    assert "Broken uv sync hints: unknown uv sync extra another-fake-extra" in problems
    assert problems.count("Broken uv sync hints: uv sync extra hint missing value") == 2


def test_cli_docs_command_hint_validator_checks_uv_sync_groups() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken uv sync groups",
                "path": "README.md#install",
                "audience": "new users",
                "description": "Regression fixture for dependency group validation.",
                "commands": (
                    "uv sync --extra quickstart --group not-a-real-group",
                    "uv sync --extra quickstart --group=another-fake-group",
                    "uv sync --group",
                    "uv sync --group --extra quickstart",
                ),
            }
        ]
    )

    assert "Broken uv sync groups: unknown uv sync group not-a-real-group" in problems
    assert "Broken uv sync groups: unknown uv sync group another-fake-group" in problems
    assert problems.count("Broken uv sync groups: uv sync group hint missing value") == 2


def test_cli_docs_command_hint_validator_accepts_guide_placeholders() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Agent guide placeholders",
                "path": "AGENTS.md",
                "audience": "coding agents",
                "description": "Regression fixture for generic guide commands.",
                "commands": (
                    "just",
                    "uv sync --extra <name> --group dev",
                ),
            }
        ]
    )

    assert not problems


def test_cli_docs_command_hint_validator_checks_http_server_directories() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken http.server hints",
                "path": "docs/deployment/docker.md",
                "audience": "operators",
                "description": "Regression fixture for static file server hints.",
                "commands": (
                    "python -m http.server 8080 --directory missing-examples",
                    "python -m http.server 8080 --directory",
                    "python -m http.server 8080 --directory --bind localhost",
                ),
            }
        ]
    )

    assert "Broken http.server hints: missing http.server directory missing-examples" in problems
    assert problems.count("Broken http.server hints: http.server hint missing directory") == 2


def test_cli_docs_command_hint_validator_checks_docker_compose_files() -> None:
    problems = _cli_docs_command_hint_problems(
        [
            {
                "label": "Broken docker compose hints",
                "path": "docs/deployment/docker.md",
                "audience": "operators",
                "description": "Regression fixture for compose file hints.",
                "commands": (
                    "docker compose up --build",
                    "docker compose -f docker/missing.yaml up --build",
                    "docker compose -f",
                    "docker compose -f --env-file docker/.env up --build",
                    "docker compose --env-file -f docker/compose.yaml up --build",
                    "docker compose --env-file missing-dir/.env -f docker/compose.yaml up",
                ),
            }
        ]
    )

    assert "Broken docker compose hints: docker compose hint missing -f" in problems
    assert "Broken docker compose hints: missing compose file docker/missing.yaml" in problems
    assert (
        problems.count("Broken docker compose hints: docker compose hint missing compose file")
        == 2
    )
    assert "Broken docker compose hints: docker compose env-file hint missing value" in problems
    assert (
        "Broken docker compose hints: missing docker compose env-file directory missing-dir/.env"
        in problems
    )


def test_cli_docs_command_placeholders_are_explained() -> None:
    placeholders: set[str] = set()
    for entry in _docs_entries():
        for command in entry.get("commands", ()):
            for token in shlex.split(command):
                if token.isupper():
                    placeholders.add(token)
                placeholders.update(ANGLE_PLACEHOLDER_RE.findall(token))

    missing = [
        placeholder for placeholder in placeholders if placeholder not in _DOCS_COMMAND_NOTE
    ]

    assert not missing, "command_note missing placeholders: " + ", ".join(missing)
    assert "placeholder" in _DOCS_COMMAND_NOTE.lower()
    assert "uppercase or angle-bracket placeholders" in _DOCS_COMMAND_NOTE
    assert "Bare easycat commands use installed CLI form" in _DOCS_COMMAND_NOTE
    assert "prefix them with uv run" in _DOCS_COMMAND_NOTE
    assert "Commands already starting with uv run are repo-local" in _DOCS_COMMAND_NOTE
    assert "just commands are repo-local shortcuts" in _DOCS_COMMAND_NOTE
    assert "raw command table" in _DOCS_COMMAND_NOTE
    assert "repository root" in _DOCS_COMMAND_NOTE
