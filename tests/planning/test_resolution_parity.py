"""DX1-1 characterization — plan-vs-``create_session`` resolution parity.

``tests/planning/test_parity.py`` compares blocking VERDICTS and skips 8 of
its cases whenever a provider extra is absent (every dev-group-only
environment, including CI's ``validate quick`` lane). Nothing there compares
the VALUES ``create_session`` actually hands to the provider constructors
against the preview, and nothing covers late mutation, injected instances, or
credential leakage. This file is that credential-free peer: every leaf
provider constructor is RECORDED (never executed for real) via
``tests/planning/_recording.py``, so every row runs on the dev group with no
``pytest.importorskip``.

Three divergences are live at this revision and characterized here as
``xfail(strict=True)``, so the PR that fixes each one MUST delete its marker
or the suite fails:

* **D1** (``native_endpointing_stt``) — a native-endpointing STT disables the
  VAD stage at construction, but the planner still selects a VAD and blocks.
* **D2** (``commercial_backend_without_sdk_*``) — a selected backend whose
  commercial SDK is absent has no pip extra, so it is never a blocking gap.
* **D3** (``custom_stt_reports_unknown_capabilities``) — an injected STT is
  described as a provider with no capabilities, indistinguishable from a
  known-capability-free provider (an injected VAD/noise/AEC gets
  ``{"injected"}``).

D4 (a registered third-party AEC config or injected AEC instance reports
``enable_echo_cancellation=False`` although a canceller was built) is
PRESERVED, not fixed — ``third_party_aec_config_reports_disabled`` pins
today's behavior with no xfail; changing it is a separate PR.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any

import pytest

from easycat.config import EasyConfig
from easycat.echo_cancellation import register_echo_canceller_provider
from easycat.noise_reduction import NoiseReducerConfig
from easycat.planning import ProviderPlan, build_provider_plan
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.transports._webrtc_config import WebRTCTransportConfig
from easycat.transports.websocket import WebSocketTransportConfig
from easycat.tts.openai_tts import OpenAITTSConfig
from easycat.turn_manager import TurnManagerConfig, TurnMode
from easycat.vad import VADConfig
from easycat.vad.factory import register_vad_provider

from ._recording import (
    ConstructedInputs,
    assert_preview_matches_construction,
    capture_construction,
)


class _Agent:
    """A minimal non-noop agent so ``create_session`` passes provider validation."""

    async def run(self, text: str) -> str:
        return "ok"


class _DuckSTT:
    async def start_stream(self) -> None:
        pass

    async def send_audio(self, _chunk: object) -> None:
        pass

    async def commit_segment(self) -> None:
        pass

    async def end_stream(self) -> None:
        pass

    async def events(self):
        if False:
            yield None


class _DuckTTS:
    async def synthesize(self, _text: str):
        if False:
            yield None

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


class _DuckVAD:
    def configure(self, **_kwargs: object) -> None:
        pass

    async def process(self, _chunk: object):
        if False:
            yield None


class _DuckNoiseReducer:
    async def process(self, chunk: object) -> object:
        return chunk


class _DuckEchoCanceller:
    async def process(self, chunk: object) -> object:
        return chunk

    def feed_reference(self, _chunk: object) -> None:
        pass


class _DuckTransport:
    """Satisfies ``TransportLike`` but is registered with no built-in backend."""

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self):
        return
        yield

    async def send_audio(self, _chunk: object) -> bool:
        return True


# ── Third-party registrations shared across a couple of rows ──────────
# ``register_*_provider`` is idempotent for identical metadata, so calling it
# from every test that needs it (rather than once at import time) is safe.


class _EnergyVADConfig:
    pass


class _EnergyVADProvider:
    def configure(self, **_kwargs: object) -> None:
        pass

    async def process(self, _chunk: object):
        if False:
            yield None


def _register_energy_vad() -> None:
    register_vad_provider("energy", _EnergyVADProvider, _EnergyVADConfig)


@dataclass
class _AcmeAECConfig:
    enabled: bool = False
    fallback_policy: str = "passthrough"


class _AcmeAECProvider:
    async def process(self, chunk: object) -> object:
        return chunk

    def feed_reference(self, _chunk: object) -> None:
        pass


def _register_acme_aec() -> None:
    register_echo_canceller_provider("acme", _AcmeAECProvider, _AcmeAECConfig)


# ── Row builders ────────────────────────────────────────────────────
#
# Each builder takes ``monkeypatch`` (to set credentials before EasyConfig
# construction resolves shortcuts) and returns the ``EasyConfig`` under test.


def _explicit_typed_configs(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
    from easycat.transports.local import LocalTransportConfig

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(
        stt=OpenAIRealtimeSTTConfig(api_key="sk-test"),
        tts=OpenAITTSConfig(api_key="sk-test"),
        vad=VADConfig(backend="silero"),
        transport=LocalTransportConfig(),
        agent=_Agent(),
        debug="off",
    )


def _shortcut_with_model(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(
        stt="deepgram/nova-2",
        transport=WebSocketTransportConfig(),
        agent=_Agent(),
        debug="off",
    )


def _shortcut_without_model(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(
        stt="deepgram",
        transport=WebSocketTransportConfig(),
        agent=_Agent(),
        debug="off",
    )


def _zero_config_openai_defaults(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(agent=_Agent(), debug="off")


def _mic_preset(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig.mic(agent=_Agent(), debug="off")


def _mic_preset_checks(plan: ProviderPlan, built: ConstructedInputs, config: EasyConfig) -> None:
    del plan, config
    assert built.enable_vad is True
    assert built.enable_echo_cancellation is True


def _browser_preset(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig.browser(agent=_Agent(), debug="off")


def _browser_preset_checks(
    plan: ProviderPlan, built: ConstructedInputs, config: EasyConfig
) -> None:
    del plan, config
    assert built.enable_echo_cancellation is True


def _bare_webrtc_transport(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(transport=WebRTCTransportConfig(), agent=_Agent(), debug="off")


def _aec_off_checks(plan: ProviderPlan, built: ConstructedInputs, config: EasyConfig) -> None:
    del plan, config
    assert built.enable_echo_cancellation is False


def _websocket_transport(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(transport=WebSocketTransportConfig(), agent=_Agent(), debug="off")


def _phone_preset(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig.phone(agent=_Agent(), debug="off")


def _phone_preset_checks(plan: ProviderPlan, built: ConstructedInputs, config: EasyConfig) -> None:
    del config
    assert built.enable_echo_cancellation is False
    assert any("transport_twilio_audio_format_auto_aligned" in w for w in plan.warnings)


def _noise_reduction_off(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(transport=WebSocketTransportConfig(), agent=_Agent(), debug="off")


def _noise_reduction_off_checks(
    plan: ProviderPlan, built: ConstructedInputs, config: EasyConfig
) -> None:
    del config
    assert built.session_config.noise_reducer is None
    assert plan.selected["noise_reducer"].provider == "off"


def _noise_reduction_on(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(
        transport=WebSocketTransportConfig(),
        enable_noise_reduction=True,
        agent=_Agent(),
        debug="off",
    )


def _noise_reduction_on_checks(
    plan: ProviderPlan, built: ConstructedInputs, config: EasyConfig
) -> None:
    del config
    assert built.session_config.noise_reducer is not None
    assert plan.selected["noise_reducer"].provider == "auto"


def _native_endpointing_stt(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(
        stt="deepgram/flux-general-en",
        vad=VADConfig(backend="silero"),
        transport=WebSocketTransportConfig(),
        agent=_Agent(),
        debug="off",
    )


def _native_endpointing_checks_vad_skipped(
    plan: ProviderPlan, built: ConstructedInputs, config: EasyConfig
) -> None:
    del plan, config
    assert built.vad is None
    assert built.enable_vad is False
    assert built.auto_turn_from_stt_final is True


def _native_endpointing_overridden_by_smart_turn(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(
        stt="deepgram/flux-general-en",
        smart_turn=True,
        transport=WebSocketTransportConfig(),
        agent=_Agent(),
        debug="off",
    )


def _native_endpointing_overridden_by_push_to_talk(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(
        stt="deepgram/flux-general-en",
        turn_taking=TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK),
        transport=WebSocketTransportConfig(),
        agent=_Agent(),
        debug="off",
    )


def _native_endpointing_overridden_by_voicemail(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    from easycat.config import TelephonyConfig

    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(
        stt="deepgram/flux-general-en",
        telephony=TelephonyConfig(enable_voicemail_detector=True),
        transport=WebSocketTransportConfig(),
        agent=_Agent(),
        debug="off",
    )


def _native_endpointing_checks_vad_built(
    plan: ProviderPlan, built: ConstructedInputs, config: EasyConfig
) -> None:
    del plan, config
    assert built.vad is not None
    assert built.enable_vad is True
    assert built.auto_turn_from_stt_final is False


def _custom_instances(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(
        stt=_DuckSTT(),
        tts=_DuckTTS(),
        vad=_DuckVAD(),
        noise_reduction=_DuckNoiseReducer(),
        echo_cancellation=_DuckEchoCanceller(),
        transport=_DuckTransport(),
        enable_noise_reduction=True,
        agent=_Agent(),
        debug="off",
    )


def _custom_instances_checks(
    plan: ProviderPlan, built: ConstructedInputs, config: EasyConfig
) -> None:
    del plan
    assert built.stt_spec is config.stt
    assert built.tts_spec is config.tts
    assert built.vad_spec is config.vad
    assert built.noise_spec is config.noise_reduction
    assert built.echo_spec is config.echo_cancellation
    assert built.transport_spec is config.transport


def _custom_stt_reports_unknown_capabilities(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(
        stt=_DuckSTT(), transport=WebSocketTransportConfig(), agent=_Agent(), debug="off"
    )


def _custom_stt_capability_check(
    plan: ProviderPlan, built: ConstructedInputs, config: EasyConfig
) -> None:
    del built, config
    # D3: an injected STT should be tagged {"injected"} the same way an
    # injected VAD/noise-reducer/AEC instance is (see _injected_selection);
    # today it falls through to the empty capability set instead.
    assert plan.selected["stt"].capabilities == frozenset({"injected"})


def _custom_transport_instance(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(transport=_DuckTransport(), agent=_Agent(), debug="off")


def _custom_transport_instance_checks(
    plan: ProviderPlan, built: ConstructedInputs, config: EasyConfig
) -> None:
    # Documented non-goal (D3's shape, but not tracked as D3 itself): an
    # UNKNOWN transport is indistinguishable from a known transport that
    # simply declares no capabilities.
    assert plan.selected["transport"].capabilities == frozenset()
    assert plan.selected["transport"].extra is None
    assert built.transport_spec is config.transport


def _registered_third_party_vad(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _register_energy_vad()
    return EasyConfig(
        vad="energy", transport=WebSocketTransportConfig(), agent=_Agent(), debug="off"
    )


def _registered_third_party_vad_checks(
    plan: ProviderPlan, built: ConstructedInputs, config: EasyConfig
) -> None:
    del built, config
    assert plan.selected["vad"].provider == "energy"
    assert plan.selected["vad"].config_type == "_EnergyVADConfig"


def _commercial_backend_without_sdk_vad(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(
        vad=VADConfig(backend="krisp"),
        transport=WebSocketTransportConfig(),
        agent=_Agent(),
        debug="off",
    )


def _commercial_backend_without_sdk_noise(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return EasyConfig(
        noise_reduction=NoiseReducerConfig(backend="krisp"),
        enable_noise_reduction=True,
        transport=WebSocketTransportConfig(),
        agent=_Agent(),
        debug="off",
    )


def _third_party_aec_config_reports_disabled(monkeypatch: pytest.MonkeyPatch) -> EasyConfig:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _register_acme_aec()
    return EasyConfig(
        echo_cancellation=_AcmeAECConfig(enabled=True),
        transport=WebSocketTransportConfig(),
        agent=_Agent(),
        debug="off",
    )


def _third_party_aec_checks(
    plan: ProviderPlan, built: ConstructedInputs, config: EasyConfig
) -> None:
    del plan
    # D4, PRESERVED (not fixed by this PR): a registered third-party AEC
    # config reports enable_echo_cancellation=False even though a canceller
    # was actually built and wired — config/_factory.py's rule is
    # ``isinstance(spec, EchoCancellationConfig)``, which a third-party config
    # class never satisfies.
    assert built.enable_echo_cancellation is False
    assert "echo" in built.called
    assert built.echo_spec is config.echo_cancellation


def _module_installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


@dataclass(frozen=True)
class _Row:
    build: Any
    passthrough: frozenset[str] = frozenset()
    checks: Any = None
    skip_if_installed: str | None = None


_ROWS: dict[str, _Row] = {
    "explicit_typed_configs": _Row(_explicit_typed_configs),
    "shortcut_with_model": _Row(_shortcut_with_model),
    "shortcut_without_model": _Row(_shortcut_without_model),
    "zero_config_openai_defaults": _Row(_zero_config_openai_defaults),
    "mic_preset": _Row(_mic_preset, checks=_mic_preset_checks),
    "browser_preset_aec_on": _Row(_browser_preset, checks=_browser_preset_checks),
    "bare_webrtc_transport_aec_off": _Row(_bare_webrtc_transport, checks=_aec_off_checks),
    "websocket_transport": _Row(_websocket_transport, checks=_aec_off_checks),
    "phone_preset": _Row(_phone_preset, checks=_phone_preset_checks),
    "noise_reduction_off": _Row(_noise_reduction_off, checks=_noise_reduction_off_checks),
    "noise_reduction_on": _Row(_noise_reduction_on, checks=_noise_reduction_on_checks),
    "native_endpointing_stt": _Row(
        _native_endpointing_stt, checks=_native_endpointing_checks_vad_skipped
    ),
    "native_endpointing_overridden_by_smart_turn": _Row(
        _native_endpointing_overridden_by_smart_turn, checks=_native_endpointing_checks_vad_built
    ),
    "native_endpointing_overridden_by_push_to_talk": _Row(
        _native_endpointing_overridden_by_push_to_talk,
        checks=_native_endpointing_checks_vad_built,
    ),
    "native_endpointing_overridden_by_voicemail": _Row(
        _native_endpointing_overridden_by_voicemail, checks=_native_endpointing_checks_vad_built
    ),
    "custom_instances": _Row(_custom_instances, checks=_custom_instances_checks),
    "custom_stt_reports_unknown_capabilities": _Row(
        _custom_stt_reports_unknown_capabilities, checks=_custom_stt_capability_check
    ),
    "custom_transport_instance": _Row(
        _custom_transport_instance, checks=_custom_transport_instance_checks
    ),
    "registered_third_party_vad": _Row(
        _registered_third_party_vad, checks=_registered_third_party_vad_checks
    ),
    "commercial_backend_without_sdk_vad": _Row(
        _commercial_backend_without_sdk_vad,
        passthrough=frozenset({"vad"}),
        skip_if_installed="krisp_audio",
    ),
    "commercial_backend_without_sdk_noise": _Row(
        _commercial_backend_without_sdk_noise,
        passthrough=frozenset({"noise"}),
        skip_if_installed="krisp_audio",
    ),
    "third_party_aec_config_reports_disabled": _Row(
        _third_party_aec_config_reports_disabled, checks=_third_party_aec_checks
    ),
}

_XFAIL_ROWS: dict[str, str] = {
    "native_endpointing_stt": (
        "D1: create_session skips the VAD stage for a native-endpointing STT "
        "(built.vad is None) but the planner still selects a VAD and blocks "
        "readiness on its extra."
    ),
    "custom_stt_reports_unknown_capabilities": (
        "D3: an injected STT is described with capabilities=frozenset(), "
        "indistinguishable from a known-capability-free provider, while an "
        "injected VAD/noise/AEC instance is tagged {'injected'}."
    ),
    "commercial_backend_without_sdk_vad": (
        "D2: VADConfig(backend='krisp') has no pip extra, so the plan reports "
        "ready even though create_vad('krisp') raises without the krisp_audio SDK."
    ),
    "commercial_backend_without_sdk_noise": (
        "D2: NoiseReducerConfig(backend='krisp') has no pip extra, so the plan "
        "reports ready even though create_noise_reducer raises without the "
        "krisp_audio SDK."
    ),
}


def _make_param(case_id: str) -> Any:
    marks = []
    if case_id in _XFAIL_ROWS:
        marks.append(pytest.mark.xfail(strict=True, reason=_XFAIL_ROWS[case_id]))
    return pytest.param(case_id, id=case_id, marks=marks)


@pytest.mark.parametrize("case_id", [_make_param(cid) for cid in _ROWS])
def test_preview_matches_constructed_values(case_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    row = _ROWS[case_id]
    if row.skip_if_installed is not None and _module_installed(row.skip_if_installed):
        pytest.skip(f"{row.skip_if_installed} is installed; this row characterizes its absence")

    config = row.build(monkeypatch)
    plan = build_provider_plan(config)
    built = capture_construction(monkeypatch, config, passthrough=row.passthrough)
    try:
        assert_preview_matches_construction(plan, built, config)
        if row.checks is not None:
            row.checks(plan, built, config)
    finally:
        import asyncio

        asyncio.run(built.session.stop(force=True))


def test_custom_instances_preserve_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """``custom_instances`` also pins identity — nothing is deep-copied."""
    config = _custom_instances(monkeypatch)
    built = capture_construction(monkeypatch, config)
    try:
        assert built.stt_spec is config.stt
        assert built.tts_spec is config.tts
        assert built.vad_spec is config.vad
        assert built.noise_spec is config.noise_reduction
        assert built.echo_spec is config.echo_cancellation
        assert built.transport_spec is config.transport
    finally:
        import asyncio

        asyncio.run(built.session.stop(force=True))


# ── Single-purpose tests ──────────────────────────────────────────────


def test_missing_credential_raises_at_easyconfig_construction_not_create_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the exception timing every later PR must preserve."""
    from easycat.errors import EasyCatError
    from easycat.project.schema import VoiceProfile

    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    with pytest.raises(EasyCatError) as excinfo:
        EasyConfig(stt="deepgram", agent=_Agent(), debug="off")
    assert excinfo.value.code == "EASYCAT_E203"

    profile = VoiceProfile(name="default", transport="local", stt="deepgram")
    plan = build_provider_plan(profile, environ={"OPENAI_API_KEY": "sk-test"})
    assert plan.missing_env == ("DEEPGRAM_API_KEY",)
    # The planner reports the gap without raising.


def test_late_mutation_is_reflected_in_both_preview_and_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    config = EasyConfig(transport=WebSocketTransportConfig(), agent=_Agent(), debug="off")

    preview_1 = build_provider_plan(config)
    assert preview_1.selected["stt"].provider == "openai-realtime"

    config.stt = "deepgram/nova-2"

    preview_2 = build_provider_plan(config)
    assert preview_2.selected["stt"].provider == "deepgram"

    built = capture_construction(monkeypatch, config)
    try:
        assert built.stt_spec.__class__ is DeepgramSTTConfig
    finally:
        import asyncio

        asyncio.run(built.session.stop(force=True))


def test_late_mutation_back_to_openai_restores_the_preset_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    config = EasyConfig.mic(agent=_Agent(), debug="off")
    assert config.smart_turn.enabled is True

    config.stt = "deepgram/flux-general-en"
    built_flux = capture_construction(monkeypatch, config)
    try:
        assert built_flux.enable_vad is False
        assert built_flux.auto_turn_from_stt_final is True
    finally:
        import asyncio

        asyncio.run(built_flux.session.stop(force=True))

    config.stt = "openai-realtime"
    built_openai = capture_construction(monkeypatch, config)
    try:
        assert config.smart_turn.enabled is True
        assert built_openai.enable_vad is True
        assert built_openai.auto_turn_from_stt_final is False
    finally:
        import asyncio

        asyncio.run(built_openai.session.stop(force=True))


@pytest.mark.parametrize("shortcut", ["webrtc", "websocket", "twilio", "telnyx", "local"])
def test_manifest_profile_and_easyconfig_paths_agree(
    shortcut: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from easycat.project.schema import VoiceProfile, parse_auth_reference

    monkeypatch.setenv("OPENAI_API_KEY", "sk-registry-test")
    monkeypatch.setenv("TWILIO_STREAM_TOKEN_SECRET", "twilio-registry-test")
    monkeypatch.setenv("TELNYX_STREAM_TOKEN_SECRET", "telnyx-registry-test")

    token = (
        parse_auth_reference(
            f"bearer-env:{shortcut.upper()}_STREAM_TOKEN_SECRET",
            field_name="voice.default.token",
        )
        if shortcut in {"twilio", "telnyx"}
        else None
    )
    profile = VoiceProfile(name="default", transport=shortcut, token=token)
    plan_from_profile = build_provider_plan(
        profile, environ={"OPENAI_API_KEY": "sk-registry-test"}
    )

    from easycat.project.manifest import ProjectManifest
    from easycat.project.schema import ProjectSection, ServerSection

    manifest = ProjectManifest(
        project=ProjectSection(), server=ServerSection(), profiles={"default": profile}
    )
    config = manifest.to_easyconfig("default", resolve_agent=False)
    plan_from_easyconfig = build_provider_plan(
        config, environ={"OPENAI_API_KEY": "sk-registry-test"}
    )

    for role in ("stt", "tts", "vad", "transport", "noise_reducer", "echo_canceller"):
        left = plan_from_profile.selected[role]
        right = plan_from_easyconfig.selected[role]
        assert (left.provider, left.config_type, left.extra, left.required_env) == (
            right.provider,
            right.config_type,
            right.extra,
            right.required_env,
        ), role
