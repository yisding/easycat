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
from easycat.config.easy import TelephonyConfig
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


class _InjectedSTT:
    """A live STT object: no catalog entry, no config class, nothing to look up."""

    async def start_stream(self) -> None: ...

    async def send_audio(self, _chunk: object) -> None: ...

    async def commit_segment(self) -> None: ...

    async def end_stream(self) -> None: ...

    async def events(self):
        if False:  # pragma: no cover - shape-only async generator
            yield None


class _InjectedTTS:
    async def synthesize(self, _text: str):
        if False:  # pragma: no cover - shape-only async generator
            yield None

    async def stop(self) -> None: ...

    async def cancel(self) -> None: ...


class _InjectedVAD:
    """A live VAD, so a noise/AEC row is not blocked by the default VAD's extra."""

    def configure(self, **_kwargs: object) -> None: ...

    async def process(self, _chunk: object):
        if False:  # pragma: no cover - shape-only async generator
            yield None


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


# ── The VAD role the session skips ───────────────────────────────────


_VAD_OVERRIDE_CASES: dict[str, dict[str, object]] = {
    "push_to_talk": {"turn_taking": TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK)},
    "smart_turn": {"smart_turn": SmartTurnConfig(enabled=True)},
    "voicemail": {"telephony": TelephonyConfig(enable_voicemail_detector=True)},
}

_NATIVE_ENDPOINTING_ENV = {
    "DEEPGRAM_API_KEY": "dg-resolution-test",
    "OPENAI_API_KEY": "sk-resolution-test",
}


def test_vad_role_is_disabled_when_the_stt_owns_endpointing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The role is RESOLVED, then reported disabled — its extra stops blocking."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-resolution-test")
    resolved = resolve_from_easyconfig(
        _config(stt="deepgram/flux-general-en"),
        probe=ProbeEnvironment.fake(env=_NATIVE_ENDPOINTING_ENV, default=False),
    )
    assert resolved.roles["vad"].enabled is False
    assert resolved.enable_vad is False
    # The underlying resolution is intact — only ``enabled`` changed.
    assert resolved.roles["vad"].provider == "silero"
    assert resolved.roles["vad"].extra == "silero-vad"
    assert "silero-vad" not in resolved.missing_extras


@pytest.mark.parametrize("override", sorted(_VAD_OVERRIDE_CASES))
def test_vad_role_returns_when_an_override_beats_native_endpointing(
    override: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skipped VAD must never hide a genuinely missing extra.

    Smart turn, push-to-talk and the voicemail detector each take endpointing
    back from the STT, so ``create_session`` builds the VAD again — and the
    plan must block on its absent extra again.
    """
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-resolution-test")
    resolved = resolve_from_easyconfig(
        _config(stt="deepgram/flux-general-en", **_VAD_OVERRIDE_CASES[override]),
        probe=ProbeEnvironment.fake(env=_NATIVE_ENDPOINTING_ENV, default=False),
    )
    assert resolved.auto_turn_from_stt_final is False
    assert resolved.roles["vad"].enabled is True
    assert resolved.enable_vad is True
    assert resolved.roles["vad"].provider == "silero"
    assert "silero-vad" in resolved.missing_extras


def test_profile_vad_role_is_disabled_when_the_stt_owns_endpointing() -> None:
    """The manifest path has no override knob, so the STT capability decides."""
    from easycat.planning._resolution import resolve_from_profile

    resolved = resolve_from_profile(
        VoiceProfile(name="default", transport="websocket", stt="deepgram/flux-general-en"),
        probe=ProbeEnvironment.fake(env=_NATIVE_ENDPOINTING_ENV, default=False),
    )
    assert resolved.auto_turn_from_stt_final is True
    assert resolved.roles["vad"].enabled is False
    assert "silero-vad" not in resolved.missing_extras


def test_resolution_reports_the_noise_and_aec_switches() -> None:
    off = resolve_from_easyconfig(_config(), probe=ProbeEnvironment.fake(default=True))
    assert off.enable_noise_reduction is False
    assert off.echo_canceller_selected is False

    on = resolve_from_easyconfig(
        _config(enable_noise_reduction=True, enable_echo_cancellation=True),
        probe=ProbeEnvironment.fake(default=True),
    )
    assert on.enable_noise_reduction is True
    # The planner's AEC view, NOT ``SessionConfig.enable_echo_cancellation`` —
    # hence the distinct field name.
    assert on.echo_canceller_selected is True


class _InjectedAEC:
    def process(self, _chunk: object) -> object: ...

    def feed_reference(self, _chunk: object) -> None: ...


def test_echo_canceller_selected_is_not_the_session_config_flag() -> None:
    """The two AEC booleans disagree, on purpose, for an injected canceller.

    ``create_session`` builds and wires the injected canceller but
    ``SessionConfig.enable_echo_cancellation`` still reports ``False``, because
    its rule is ``isinstance(spec, EchoCancellationConfig) and spec.enabled``
    (pinned by ``tests/planning/test_resolution_parity.py``'s D4 row and by
    ``test_echo_cancellation_enabled_table``). The resolver answers the OTHER
    question — "did the planner pick something other than the passthrough?" — so
    this row pins the divergence rather than leaving it only described in a
    docstring.
    """
    from easycat._pipeline_decisions import echo_cancellation_enabled
    from easycat.echo_cancellation import EchoCancellationConfig

    injected = _InjectedAEC()
    resolved = resolve_from_easyconfig(
        _config(echo_cancellation=injected), probe=ProbeEnvironment.fake(default=True)
    )
    assert resolved.echo_canceller_selected is True
    assert resolved.roles["echo_canceller"].capabilities == frozenset({"injected"})
    assert echo_cancellation_enabled(injected, config_cls=EchoCancellationConfig) is False


def test_late_stt_mutation_resolves_the_renormalized_smart_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The late-mutation row the boolean table cannot express.

    ``EasyConfig.mic`` ships ``smart_turn.enabled=True``. Reassigning ``stt`` to
    a native-endpointing provider afterwards makes ``_validate_for_session``
    turn smart-turn back off (``_renormalize_smart_turn``, gh-1027), so
    ``create_session`` drives turns from STT finals and builds no VAD. The
    resolver used to read the un-renormalized attribute and say the opposite;
    this row carried an ``xfail(strict=True)`` until
    :func:`easycat.planning._resolution._resolved_smart_turn_enabled` learned
    the same "untouched default" rule.

    ``tests/planning/test_resolution_parity.py::
    test_late_mutation_back_to_openai_restores_the_preset_default`` pins the
    construction side of the same mutation.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-resolution-test")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-resolution-test")
    config = EasyConfig.mic(agent=_Agent(), debug="off")
    assert config.smart_turn.enabled is True

    config.stt = "deepgram/flux-general-en"
    resolved = resolve_from_easyconfig(config, probe=ProbeEnvironment.fake(default=True))

    assert resolved.auto_turn_from_stt_final is True
    assert resolved.enable_vad is False

    # ...and switching back restores the preset default, so the stage the
    # planner reports is never frozen by an earlier preview.
    config.stt = "openai"
    restored = resolve_from_easyconfig(config, probe=ProbeEnvironment.fake(default=True))
    assert restored.auto_turn_from_stt_final is False
    assert restored.enable_vad is True


@pytest.mark.parametrize("spelling", ["bool", "sensitivity"])
def test_late_smart_turn_override_keeps_the_vad_role_and_its_extra(
    spelling: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two smart-turn spellings a raw ``.enabled`` read cannot see.

    ``cfg.smart_turn = True`` is a supported assignment (the field is typed
    ``SmartTurnConfig | bool | None``) and ``getattr(True, "enabled", False)`` is
    ``False``; a late ``cfg.smart_turn_sensitivity`` forces ``enabled=True``
    without touching ``smart_turn`` at all. Both make ``create_session`` build a
    VAD, so both must keep the role — and its missing extra as a blocking gap —
    in the plan.
    """
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-resolution-test")
    config = _config(stt="deepgram/flux-general-en")
    if spelling == "bool":
        config.smart_turn = True
    else:
        config.smart_turn_sensitivity = 0.7

    resolved = resolve_from_easyconfig(
        config, probe=ProbeEnvironment.fake(env=_NATIVE_ENDPOINTING_ENV, default=False)
    )
    assert resolved.auto_turn_from_stt_final is False
    assert resolved.enable_vad is True
    assert resolved.roles["vad"].provider == "silero"
    assert "silero-vad" in resolved.missing_extras


# ── Selections the session cannot construct ──────────────────────────


def test_missing_backends_is_empty_for_a_fully_available_pipeline() -> None:
    resolved = resolve_from_easyconfig(_config(), probe=ProbeEnvironment.fake(default=True))
    assert resolved.missing_backends == ()


def test_missing_probe_module_without_an_extra_is_a_blocking_backend_gap() -> None:
    """DX1-D2: a commercial backend with no pip extra must still be able to block.

    ``VAD_BACKENDS["krisp"]`` declares ``extra=None`` because Krisp ships no PyPI
    package, so ``_role_gap`` (which resolves a probe module FROM the extra) can
    never fire for it — while ``create_vad(VADConfig(backend="krisp"))`` raises on
    every machine without the SDK. The backend's own ``probe_module`` closes that,
    and the gap is reported separately from ``missing_extras`` because there is no
    extra to install.
    """
    resolved = resolve_from_easyconfig(
        _config(vad=VADConfig(backend="krisp")),
        probe=ProbeEnvironment.fake(
            env={"OPENAI_API_KEY": "sk-resolution-test"},
            unavailable=["krisp_audio"],
            default=True,
        ),
    )
    assert resolved.missing_backends == ("vad:krisp",)
    # Reported as its OWN gap class: there is no extra to name, so
    # ``missing_extras`` must stay untouched.
    assert resolved.missing_extras == ()

    plan = _plan_with_probe(
        _config(vad=VADConfig(backend="krisp")),
        probe=ProbeEnvironment.fake(
            env={"OPENAI_API_KEY": "sk-resolution-test"},
            unavailable=["krisp_audio"],
            default=True,
        ),
    )
    assert plan.missing_backends == ("vad:krisp",)
    assert "missing_backend:vad:krisp" in plan.blocking_errors()
    assert plan.has_blocking_errors is True
    assert plan.missing_extras == ()


def test_present_probe_module_is_not_a_gap() -> None:
    """The SAME selection on a machine that HAS the SDK is buildable, so no gap."""
    resolved = resolve_from_easyconfig(
        _config(vad=VADConfig(backend="krisp")),
        probe=ProbeEnvironment.fake(
            env={"OPENAI_API_KEY": "sk-resolution-test"},
            available=["krisp_audio"],
            default=True,
        ),
    )
    assert resolved.missing_backends == ()
    assert resolved.roles["vad"].provider == "krisp"
    assert resolved.roles["vad"].probe_module == "krisp_audio"


def test_a_backend_declaring_an_extra_never_becomes_a_backend_gap() -> None:
    """An extra-bearing backend keeps the missing-EXTRA path, unchanged.

    ``silero`` is absent here in exactly the same way ``krisp`` is above, and it
    must still be reported as ``missing_extras=("silero-vad",)`` — the operator
    fix is ``uv sync --extra silero-vad``, which ``missing_backends`` cannot
    express.
    """
    resolved = resolve_from_easyconfig(
        _config(),
        probe=ProbeEnvironment.fake(
            env={"OPENAI_API_KEY": "sk-resolution-test"},
            unavailable=["onnxruntime"],
            default=True,
        ),
    )
    assert resolved.roles["vad"].provider == "silero"
    assert resolved.missing_extras == ("silero-vad",)
    assert resolved.missing_backends == ()


def test_degrading_backend_is_never_a_backend_gap() -> None:
    """An auto chain that falls back to passthrough warns; it never blocks.

    ``create_noise_reducer`` with ``backend="auto"`` and the default
    ``fallback_policy="passthrough"`` returns a no-op reducer instead of raising
    when nothing is installed, so ``/health/ready`` must stay ready however many
    probes are absent.
    """
    from easycat.noise_reduction import NoiseReducerConfig

    resolved = resolve_from_easyconfig(
        _config(
            enable_noise_reduction=True,
            noise_reduction=NoiseReducerConfig(backend="auto", fallback_policy="passthrough"),
            vad=_InjectedVAD(),
        ),
        # EVERY probe absent, so nothing is available to hide behind.
        probe=ProbeEnvironment.fake(env={"OPENAI_API_KEY": "sk-resolution-test"}, default=False),
    )
    assert "degrades_to_passthrough" in resolved.roles["noise_reducer"].capabilities
    assert resolved.missing_backends == ()
    assert resolved.missing_extras == ()
    assert any("degraded" in warning for warning in resolved.warnings)


def test_a_disabled_role_never_contributes_a_backend_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A skipped stage is not a requirement — the same rule env/extras follow.

    ``deepgram/flux-general-en`` owns endpointing, so ``create_session`` builds no
    VAD at all and the absent Krisp SDK is not a gap for this deployment.
    """
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-resolution-test")
    resolved = resolve_from_easyconfig(
        _config(stt="deepgram/flux-general-en", vad=VADConfig(backend="krisp")),
        probe=ProbeEnvironment.fake(env=_NATIVE_ENDPOINTING_ENV, unavailable=["krisp_audio"]),
    )
    assert resolved.roles["vad"].enabled is False
    assert resolved.missing_backends == ()


# ── Injected stt/tts providers ───────────────────────────────────────


def test_injected_stt_and_tts_report_unknown_capabilities() -> None:
    """DX1-D3: a live injected provider is opaque, not capability-free.

    ``capabilities=frozenset()`` reads as "a known provider that declares no
    capabilities" — the opposite of the truth for an object EasyCat cannot
    introspect. ``{"injected"}`` is the representation the vad / noise_reducer /
    echo_canceller roles have always used; stt and tts now match.
    """
    resolved = resolve_from_easyconfig(
        _config(stt=_InjectedSTT(), tts=_InjectedTTS()),
        probe=ProbeEnvironment.fake(default=True),
    )
    for role, expected in (("stt", "_InjectedSTT"), ("tts", "_InjectedTTS")):
        decision = resolved.roles[role]
        assert decision.capabilities == frozenset({"injected"}), role
        assert decision.provider == expected
        assert decision.config_type == expected
        assert decision.extra is None
        assert decision.required_env is None
    # No credential is invented for an object that needs none, so an injected
    # pipeline is not blocked by an env var it never reads.
    assert "OPENAI_API_KEY" not in resolved.missing_env


def test_injected_stt_does_not_claim_native_endpointing() -> None:
    """The ``{"injected"}`` tag must not accidentally satisfy the turn policy.

    ``_decide_auto_turn`` reads ``"native_endpointing" in stt.capabilities``, so an
    opaque STT keeps EasyCat's own VAD stage — the conservative answer, and the
    one ``create_session`` takes (``_stt_uses_native_endpointing`` asks the
    catalog, which knows nothing about a live object).
    """
    resolved = resolve_from_easyconfig(
        _config(stt=_InjectedSTT()), probe=ProbeEnvironment.fake(default=True)
    )
    assert resolved.auto_turn_from_stt_final is False
    assert resolved.enable_vad is True


def test_a_registered_config_instance_still_reports_its_catalog_capabilities() -> None:
    """The injected branch must not swallow a REGISTERED third-party config.

    ``catalog.is_config_instance`` is checked first for vad/noise/aec, and an stt
    config is not a live provider, so declared capabilities survive.
    """
    from easycat.vad.factory import _CATALOG as vad_catalog

    class _EnergyVADConfig:
        pass

    class _EnergyVAD:
        def configure(self, **_kwargs: object) -> None: ...

        async def process(self, _chunk: object):
            if False:  # pragma: no cover - shape-only async generator
                yield None

    tables = (
        "providers",
        "env_vars",
        "extras",
        "api_domains",
        "probe_modules",
        "capabilities",
        "capability_resolvers",
        "config_to_provider",
    )
    saved = {name: dict(getattr(vad_catalog, name)) for name in tables}
    discovered = vad_catalog._discovered
    try:
        from easycat.vad.factory import register_vad_provider

        register_vad_provider(
            "energy", _EnergyVAD, _EnergyVADConfig, capabilities=frozenset({"offline"})
        )
        resolved = resolve_from_easyconfig(
            _config(vad=_EnergyVADConfig()), probe=ProbeEnvironment.fake(default=True)
        )
    finally:
        for name, entries in saved.items():
            table = getattr(vad_catalog, name)
            table.clear()
            table.update(entries)
        object.__setattr__(vad_catalog, "_discovered", discovered)

    assert resolved.roles["vad"].provider == "energy"
    assert resolved.roles["vad"].capabilities == frozenset({"offline"})


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
