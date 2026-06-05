from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest

from easycat.tts.factory import _PROVIDER_TO_CONFIG as _TTS_REGISTRY
from easycat.tts.input import resolve_tts_input_policy
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
_TTS_NATIVE_MARKER_PROVIDERS = {"cartesia", "elevenlabs"}
_TTS_DEFAULT_OUTPUT_AUDIO_FORMATS = ["pcm16/24000/mono"]


def _spec(surface: str):
    for spec in LIVE_PROVIDER_SURFACES:
        if spec.surface == surface:
            return spec
    raise AssertionError(f"no live spec for surface {surface}")


def _specs(surface: str):
    return tuple(spec for spec in LIVE_PROVIDER_SURFACES if spec.surface == surface)


def _report_payload(spec):
    report = build_provider_capability_report(
        spec,
        live_checked_at=datetime.now(UTC),
        credential_present=True,
        live_status="passed",
    )
    return report.to_dict()


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


def test_tts_report_populates_input_policy() -> None:
    spec = _spec("tts")
    payload = _report_payload(spec)

    assert payload["capabilities"]["tts_input_policy"]["accepted_formats"]
    assert (
        payload["capabilities"]["tts_input_policy"]["supports_ssml"]
        == payload["capabilities"]["ssml"]
    )


@pytest.mark.parametrize("spec", _specs("tts"), ids=lambda spec: spec.provider)
@pytest.mark.asyncio
async def test_tts_report_ssml_matches_builtin_provider_input_policy(spec) -> None:
    provider_cls, config_cls = _TTS_REGISTRY[spec.provider]
    provider = provider_cls(config_cls(api_key="test-key"))

    try:
        expected = resolve_tts_input_policy(provider).supports_ssml
    finally:
        close = getattr(provider, "close", None)
        if close is not None:
            maybe_awaitable = close()
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable

    payload = _report_payload(spec)

    assert payload["capabilities"]["ssml"] is expected
    assert payload["capabilities"]["tts_input_policy"]["supports_ssml"] is expected


@pytest.mark.parametrize("spec", _specs("tts"), ids=lambda spec: spec.provider)
def test_tts_report_marker_capabilities_match_native_marker_emitters(spec) -> None:
    expected = spec.provider in _TTS_NATIVE_MARKER_PROVIDERS
    payload = _report_payload(spec)

    assert payload["capabilities"]["markers"] is expected
    assert payload["capabilities"]["alignment"] is expected


@pytest.mark.parametrize("spec", _specs("tts"), ids=lambda spec: spec.provider)
def test_tts_report_defaults_to_normalized_pcm16_output(spec) -> None:
    payload = _report_payload(spec)

    assert payload["capabilities"]["output_audio_formats"] == _TTS_DEFAULT_OUTPUT_AUDIO_FORMATS


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
