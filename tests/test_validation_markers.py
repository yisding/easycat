from __future__ import annotations

from datetime import date

from tests._marker_lint import validate_flaky_marker, validate_provider_surface_markers


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
