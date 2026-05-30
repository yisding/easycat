from __future__ import annotations

from datetime import UTC, datetime

import pytest

from easycat.validation.provider_reports import (
    LIVE_PROVIDER_SURFACES,
    Surface,
    _capability_status,
    build_provider_capability_report,
    known_live_surfaces,
    select_provider_surfaces,
)

pytestmark = [pytest.mark.contract]

_VALID_STATUSES = {
    "pass",
    "expected_skip",
    "auth_failure",
    "quota_failure",
    "provider_drift",
    "failure",
}


def _spec(surface: str):
    for spec in LIVE_PROVIDER_SURFACES:
        if spec.surface == surface:
            return spec
    raise AssertionError(f"no live spec for surface {surface}")


def test_declared_surfaces_match_live_specs() -> None:
    # Every surface in the Surface Literal must be selectable via a live spec,
    # and no live spec may reference an undeclared surface.
    declared = set(Surface.__args__)  # type: ignore[attr-defined]
    live = known_live_surfaces()
    assert live <= declared
    assert declared == live


@pytest.mark.parametrize(
    ("live_status", "expected"),
    [
        ("passed", "pass"),
        ("pass", "pass"),
        ("expected_skip", "expected_skip"),
        ("failed_missing_required_secret", "auth_failure"),
        ("failed", "failure"),
        ("not_requested", "failure"),
        ("some_typo_value", "failure"),
    ],
)
def test_capability_status_stays_in_contract(live_status: str, expected: str) -> None:
    result = _capability_status(live_status, failure_class=None)
    assert result == expected
    assert result in _VALID_STATUSES


def test_capability_status_failure_class_mapping() -> None:
    assert _capability_status("failed", "provider_quota") == "quota_failure"
    assert _capability_status("failed", "auth_or_quota") == "auth_failure"
    assert _capability_status("failed", "provider_drift") == "provider_drift"


def test_tts_report_populates_voices() -> None:
    spec = _spec("tts")
    assert spec.default_voices, "expected a default voice for the chosen tts spec"
    report = build_provider_capability_report(
        spec,
        live_checked_at=datetime.now(UTC),
        credential_present=True,
        live_status="passed",
    )
    assert report.voices, "tts capability report must catalog voices"
    payload = report.to_dict()
    assert payload["voices"], "serialized tts report must expose a non-empty voices list"


def test_non_tts_report_has_no_voices() -> None:
    spec = _spec("stt")
    report = build_provider_capability_report(
        spec,
        live_checked_at=datetime.now(UTC),
        credential_present=True,
        live_status="passed",
    )
    assert report.voices == ()


def test_select_provider_surfaces_rejects_removed_surfaces() -> None:
    assert select_provider_surfaces(surfaces=["vad"]) == ()
    assert select_provider_surfaces(surfaces=["transport"]) == ()
