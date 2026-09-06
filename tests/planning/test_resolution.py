"""Unit coverage for the one pure resolution path (``easycat.planning._resolution``).

``build_provider_plan`` is now a projection over
:class:`~easycat.planning._resolution.ResolvedConfiguration`. These tests cover
the parts of resolution the public plan does not expose: the injected probe
snapshot, the ``spec`` field's secrets constraint, and the pipeline booleans the
resolver computes for later consumers.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from easycat.config import EasyConfig
from easycat.planning import build_provider_plan
from easycat.planning._resolution import (
    ProbeEnvironment,
    RoleDecision,
    resolve_from_easyconfig,
)
from easycat.planning.provider_plan import _plan_with_probe
from easycat.project.schema import VoiceProfile
from easycat.smart_turn import SmartTurnConfig
from easycat.transports.websocket import WebSocketTransportConfig
from easycat.turn_manager import TurnManagerConfig, TurnMode
from easycat.vad import VADConfig


class _Agent:
    async def run(self, text: str) -> str:
        return "ok"


def _config(**overrides: object) -> EasyConfig:
    kwargs: dict[str, object] = {
        "stt": "openai",
        "tts": "openai",
        "vad": VADConfig(backend="silero"),
        "transport": WebSocketTransportConfig(),
        "openai_api_key": "sk-resolution-test",
        "agent": _Agent(),
        "debug": "off",
    }
    kwargs.update(overrides)
    return EasyConfig(**kwargs)  # type: ignore[arg-type]


# ── ProbeEnvironment ─────────────────────────────────────────────────


def test_probe_environment_fake_honours_available_and_unavailable() -> None:
    probe = ProbeEnvironment.fake(available=["onnxruntime"], unavailable=["ten_vad"])
    assert probe.module_available("onnxruntime") is True
    assert probe.module_available("ten_vad") is False
    # Anything unnamed falls to ``default``.
    assert probe.module_available("krisp_audio") is False
    assert ProbeEnvironment.fake(default=True).module_available("krisp_audio") is True


def test_probe_environment_fake_refuses_a_module_named_on_both_sides() -> None:
    with pytest.raises(ValueError, match="onnxruntime"):
        ProbeEnvironment.fake(available=["onnxruntime"], unavailable=["onnxruntime"])


def test_probe_environment_from_process_reads_the_given_environ(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EASYCAT_PROBE_MARKER", "present")
    assert ProbeEnvironment.from_process().env.get("EASYCAT_PROBE_MARKER") == "present"
    # An explicit mapping wins over the process env, and is snapshotted.
    probe = ProbeEnvironment.from_process({})
    assert probe.env == {}
    assert "EASYCAT_PROBE_MARKER" not in probe.env


def test_probe_environment_from_process_probes_through_the_patchable_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``from_process`` binds the probe by lazy lookup so a server-internal plan
    # (which cannot be handed a ProbeEnvironment) is still testable.
    probe = ProbeEnvironment.from_process({})
    assert probe.module_available("easycat_not_a_real_module") is False
    monkeypatch.setattr(
        "easycat.planning._resolution._default_module_available", lambda _name: True
    )
    assert probe.module_available("easycat_not_a_real_module") is True


# ── RoleDecision ─────────────────────────────────────────────────────


def test_role_decision_spec_is_excluded_from_repr_and_equality() -> None:
    secret = "sk-" + "x" * 40
    base = {
        "role": "stt",
        "provider": "openai",
        "model": None,
        "config_type": "OpenAISTTConfig",
        "extra": "openai",
        "required_env": "OPENAI_API_KEY",
        "capabilities": frozenset(),
    }
    with_secret = RoleDecision(**base, spec=secret)  # type: ignore[arg-type]
    without = RoleDecision(**base, spec=None)  # type: ignore[arg-type]
    assert with_secret == without
    assert secret not in repr(with_secret)


def test_resolution_holds_the_caller_object_by_identity() -> None:
    vad = VADConfig(backend="silero")
    config = _config(vad=vad)
    resolved = resolve_from_easyconfig(config, probe=ProbeEnvironment.fake(default=True))
    assert resolved.roles["vad"].spec is vad
    assert resolved.roles["transport"].spec is config.transport


# ── Seam / public-entry-point agreement ──────────────────────────────


def test_plan_with_probe_matches_build_provider_plan_for_the_process_snapshot() -> None:
    profile = VoiceProfile(name="default", transport="local", stt="openai", tts="openai")
    env = {"OPENAI_API_KEY": "sk-resolution-test"}
    assert _plan_with_probe(
        profile, probe=ProbeEnvironment.from_process(env)
    ) == build_provider_plan(profile, environ=env)


def test_projection_drops_the_spec_from_the_public_selection() -> None:
    secret = "sk-" + "y" * 40
    config = _config(openai_api_key=secret, stt="openai", tts="openai")
    plan = build_provider_plan(config, environ={"OPENAI_API_KEY": secret})
    assert secret not in repr(plan)
    assert not hasattr(plan.selected["stt"], "spec")


# ── Pipeline booleans ────────────────────────────────────────────────


_BOOLEAN_CASES: dict[str, tuple[dict[str, object], bool]] = {
    "plain_stt_runs_the_vad_stage": ({}, False),
    "native_endpointing_stt_drives_turns_from_finals": (
        {"stt": "deepgram/flux-general-en"},
        True,
    ),
    "smart_turn_overrides_native_endpointing": (
        {"stt": "deepgram/flux-general-en", "smart_turn": SmartTurnConfig(enabled=True)},
        False,
    ),
    "push_to_talk_overrides_native_endpointing": (
        {
            "stt": "deepgram/flux-general-en",
            "turn_taking": TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK),
        },
        False,
    ),
}


@pytest.mark.parametrize("case", sorted(_BOOLEAN_CASES))
def test_resolution_reports_the_pipeline_booleans(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    overrides, expected_auto_turn = _BOOLEAN_CASES[case]
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-resolution-test")
    resolved = resolve_from_easyconfig(
        _config(**overrides), probe=ProbeEnvironment.fake(default=True)
    )
    assert resolved.auto_turn_from_stt_final is expected_auto_turn
    assert resolved.enable_vad is not expected_auto_turn


def test_voicemail_detector_overrides_native_endpointing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from easycat.config.easy import TelephonyConfig

    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-resolution-test")
    resolved = resolve_from_easyconfig(
        _config(
            stt="deepgram/flux-general-en",
            telephony=TelephonyConfig(enable_voicemail_detector=True),
        ),
        probe=ProbeEnvironment.fake(default=True),
    )
    assert resolved.auto_turn_from_stt_final is False
    assert resolved.enable_vad is True


def test_resolution_reports_the_noise_and_aec_switches() -> None:
    off = resolve_from_easyconfig(_config(), probe=ProbeEnvironment.fake(default=True))
    assert off.enable_noise_reduction is False
    assert off.enable_echo_cancellation is False

    on = resolve_from_easyconfig(
        _config(enable_noise_reduction=True, enable_echo_cancellation=True),
        probe=ProbeEnvironment.fake(default=True),
    )
    assert on.enable_noise_reduction is True
    assert on.enable_echo_cancellation is True


def test_missing_backends_is_empty_until_a_later_workstream() -> None:
    resolved = resolve_from_easyconfig(_config(), probe=ProbeEnvironment.fake(default=True))
    assert resolved.missing_backends == ()


# ── Import weight ────────────────────────────────────────────────────


def test_resolution_never_imports_a_provider_sdk() -> None:
    # Subprocess peer of ``test_boundary.py``'s checks: resolving a config that
    # selects silero + webrtc must probe, never import, the provider SDKs.
    code = (
        "import sys; "
        "from easycat.planning._resolution import ProbeEnvironment, resolve_from_profile; "
        "from easycat.project.schema import VoiceProfile; "
        "p = VoiceProfile(name='default', transport='webrtc', vad='silero'); "
        "resolve_from_profile(p, probe=ProbeEnvironment.fake(default=True)); "
        "print(sorted(m for m in sys.modules "
        "if m.split('.')[0] in {'onnxruntime', 'aiortc', 'aiohttp', 'openai'}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "OPENAI_API_KEY": "x"},
    )
    assert result.stdout.strip() == "[]", result.stdout
