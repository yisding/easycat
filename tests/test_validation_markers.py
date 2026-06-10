from __future__ import annotations

import tomllib
from datetime import date
from pathlib import Path

from tests._marker_lint import validate_flaky_marker, validate_provider_surface_markers

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_VALIDATION_MARKERS = {
    "agent_bridge",
    "contract",
    "flaky",
    "integration_live",
    "integration_local",
    "integration_socket",
    "latency",
    "provider",
    "provider_cartesia",
    "provider_deepgram",
    "provider_elevenlabs",
    "provider_openai",
    "release",
    "slow",
    "stress",
    "surface_agent",
    "surface_stt",
    "surface_transport",
    "surface_tts",
    "surface_vad",
}


def _registered_marker_names() -> set[str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    markers = pyproject["tool"]["pytest"]["ini_options"]["markers"]
    return {marker.split(":", 1)[0].split("(", 1)[0].strip() for marker in markers}


def _registered_marker_descriptions() -> dict[str, str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    markers = pyproject["tool"]["pytest"]["ini_options"]["markers"]
    descriptions: dict[str, str] = {}
    for marker in markers:
        name = marker.split(":", 1)[0].split("(", 1)[0].strip()
        _, _, description = marker.partition(":")
        descriptions[name] = description.strip()
    return descriptions


def _validation_task_section(heading: str, next_heading: str) -> str:
    plan = (REPO_ROOT / "plan/validation/tasks.md").read_text(encoding="utf-8")
    return plan.split(heading, 1)[1].split(next_heading, 1)[0]


def test_provider_scoped_live_marker_requires_surface_scope() -> None:
    errors = validate_provider_surface_markers(
        nodeid="tests/example_test.py::test_live_openai",
        marker_names={"integration_live", "provider_openai"},
    )

    assert errors == [
        "tests/example_test.py::test_live_openai is provider-scoped but missing "
        "surface metadata; add one of: surface_agent, surface_stt, surface_transport, "
        "surface_tts, surface_vad"
    ]


def test_surface_scoped_contract_marker_requires_provider_scope() -> None:
    errors = validate_provider_surface_markers(
        nodeid="tests/example_test.py::test_contract",
        marker_names={"contract", "surface_stt"},
    )

    assert errors == [
        "tests/example_test.py::test_contract is surface-scoped but missing "
        "provider metadata; add provider(NAME) or one of: provider_cartesia, "
        "provider_deepgram, provider_elevenlabs, provider_openai"
    ]


def test_provider_and_surface_scoped_validation_marker_passes() -> None:
    errors = validate_provider_surface_markers(
        nodeid="tests/example_test.py::test_live_openai_stt",
        marker_names={"integration_live", "provider_openai", "surface_stt"},
    )

    assert errors == []


def test_unscoped_live_marker_is_allowed_until_provider_scope_is_declared() -> None:
    errors = validate_provider_surface_markers(
        nodeid="tests/example_test.py::test_external_tool_live",
        marker_names={"integration_live"},
    )

    assert errors == []


def test_flaky_marker_requires_issue_owner_and_review_by() -> None:
    errors = validate_flaky_marker(
        nodeid="tests/example_test.py::test_intermittent",
        marker_names={"flaky"},
        marker_kwargs={"issue": "GH-123"},
        today=date(2026, 5, 22),
    )

    assert errors == [
        "tests/example_test.py::test_intermittent has @pytest.mark.flaky missing "
        "metadata: owner, review_by"
    ]


def test_flaky_marker_review_by_must_not_be_stale() -> None:
    errors = validate_flaky_marker(
        nodeid="tests/example_test.py::test_intermittent",
        marker_names={"flaky"},
        marker_kwargs={"issue": "GH-123", "owner": "validation", "review_by": "2026-05-21"},
        today=date(2026, 5, 22),
    )

    assert errors == [
        "tests/example_test.py::test_intermittent has stale flaky review_by date 2026-05-21"
    ]


def test_release_marker_cannot_remain_flaky() -> None:
    errors = validate_flaky_marker(
        nodeid="tests/example_test.py::test_release_gate",
        marker_names={"flaky", "release"},
        marker_kwargs={"issue": "GH-123", "owner": "validation", "review_by": "2026-06-01"},
        today=date(2026, 5, 22),
    )

    assert errors == [
        "tests/example_test.py::test_release_gate is release-scoped but still "
        "quarantined with @pytest.mark.flaky"
    ]


def test_valid_flaky_marker_passes_until_review_date() -> None:
    errors = validate_flaky_marker(
        nodeid="tests/example_test.py::test_intermittent",
        marker_names={"flaky"},
        marker_kwargs={"issue": "GH-123", "owner": "validation", "review_by": "2026-05-22"},
        today=date(2026, 5, 22),
    )

    assert errors == []


def test_validation_marker_plan_tracks_registered_marker_state() -> None:
    registered = _registered_marker_names()
    marker_section = _validation_task_section(
        "### V0.1 Register Validation Markers",
        "### V0.2 Define Validation Report Model",
    )
    flaky_section = _validation_task_section(
        "### V0.4 Add Flaky Quarantine Metadata Check",
        "## V1: First-Class CLI And CI Artifacts",
    )

    assert REQUIRED_VALIDATION_MARKERS <= registered
    assert "strict_markers = true" in marker_section
    for marker_name in REQUIRED_VALIDATION_MARKERS:
        assert marker_name in marker_section

    assert "registers only `integration_local`" not in marker_section
    assert "No validation-specific markers are present yet" not in marker_section
    assert "No `flaky` marker is registered or used" not in flaky_section
    assert "`flaky` marker is registered" in flaky_section
    assert "`tests/_marker_lint.py`" in flaky_section
    assert "`tests/conftest.py`" in flaky_section
    assert "release-scoped flaky tests" in flaky_section


def test_integration_local_marker_definition_covers_local_builds() -> None:
    descriptions = _registered_marker_descriptions()
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    reference = (REPO_ROOT / "plan" / "validation" / "reference.md").read_text(encoding="utf-8")

    assert descriptions["integration_local"] == (
        "local integration tests with no live services; may use subprocesses/filesystem"
    )
    for doc in (contributing, reference):
        assert "local integration tests with no live services" in doc
    assert "fake providers, subprocesses, or filesystem state" in contributing
    assert "in-process end-to-end tests with fake providers" not in "\n".join(
        (contributing, reference, descriptions["integration_local"])
    )
