from __future__ import annotations

import tomllib
from datetime import date
from pathlib import Path

from tests._marker_lint import validate_flaky_marker, validate_provider_surface_markers
from tests.conftest import GUARD_DIRS, GUARD_EXEMPT, GUARD_FILES

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_VALIDATION_MARKERS = {
    "agent_bridge",
    "contract",
    "flaky",
    "guard",
    "integration_external",
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
    "serial",
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


def test_provider_scoped_live_marker_requires_surface_scope() -> None:
    errors = validate_provider_surface_markers(
        nodeid="tests/example_test.py::test_live_openai",
        marker_names={"integration_live", "provider_openai"},
    )

    assert errors == [
        (
            "tests/example_test.py::test_live_openai is provider-scoped but missing "
            "surface metadata; add one of: surface_agent, surface_stt, surface_transport, "
            "surface_tts, surface_vad"
        )
    ]


def test_surface_scoped_contract_marker_requires_provider_scope() -> None:
    errors = validate_provider_surface_markers(
        nodeid="tests/example_test.py::test_contract",
        marker_names={"contract", "surface_stt"},
    )

    assert errors == [
        (
            "tests/example_test.py::test_contract is surface-scoped but missing "
            "provider metadata; add provider(NAME) or one of: provider_cartesia, "
            "provider_deepgram, provider_elevenlabs, provider_openai"
        )
    ]


def test_provider_and_surface_scoped_validation_marker_passes() -> None:
    errors = validate_provider_surface_markers(
        nodeid="tests/example_test.py::test_live_openai_stt",
        marker_names={"integration_live", "provider_openai", "surface_stt"},
    )

    assert errors == []


def test_unscoped_live_marker_requires_provider_and_surface_scope() -> None:
    errors = validate_provider_surface_markers(
        nodeid="tests/example_test.py::test_external_tool_live",
        marker_names={"integration_live"},
    )

    assert errors == [
        (
            "tests/example_test.py::test_external_tool_live uses bare integration_live; "
            "live tests must declare provider and surface metadata, or use "
            "integration_external/integration_local for non-provider dependencies"
        )
    ]


def test_unscoped_external_marker_is_allowed() -> None:
    errors = validate_provider_surface_markers(
        nodeid="tests/example_test.py::test_external_tool",
        marker_names={"integration_external"},
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
        (
            "tests/example_test.py::test_intermittent has @pytest.mark.flaky missing "
            "metadata: owner, review_by"
        )
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
        (
            "tests/example_test.py::test_release_gate is release-scoped but still "
            "quarantined with @pytest.mark.flaky"
        )
    ]


def test_valid_flaky_marker_passes_until_review_date() -> None:
    errors = validate_flaky_marker(
        nodeid="tests/example_test.py::test_intermittent",
        marker_names={"flaky"},
        marker_kwargs={"issue": "GH-123", "owner": "validation", "review_by": "2026-05-22"},
        today=date(2026, 5, 22),
    )

    assert errors == []


def test_guard_overlay_paths_exist() -> None:
    """A moved or deleted guard path would silently drop its guard coverage."""
    for rel in sorted(GUARD_FILES | GUARD_EXEMPT):
        assert (REPO_ROOT / rel).is_file(), f"guard overlay references missing file: {rel}"
    for rel in sorted(GUARD_DIRS):
        assert (REPO_ROOT / rel).is_dir(), f"guard overlay references missing directory: {rel}"


def test_required_validation_markers_are_registered() -> None:
    """The validation runner's marker expressions rely on these being declared."""
    assert REQUIRED_VALIDATION_MARKERS <= _registered_marker_names()


def test_local_and_external_markers_are_documented_for_contributors() -> None:
    descriptions = _registered_marker_descriptions()
    contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    reference = (REPO_ROOT / "docs" / "reference" / "validation-vocabulary.md").read_text(
        encoding="utf-8"
    )

    assert "integration_local" in descriptions
    assert "integration_external" in descriptions
    for doc in (contributing, reference):
        assert "local integration tests with no live services" in doc
        assert "external local binaries, SDKs, or services" in doc
