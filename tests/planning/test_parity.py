"""REQUIRED GATE — planner-vs-``create_session`` parity for the 7 roles.

Because 5 of the 7 planner roles (vad/transport/agent/noise_reducer/
echo_canceller) hand-roll resolution OUTSIDE any catalog, the planner can
silently diverge from what ``create_session`` actually does. This test is the
gate the M6b ``/health/ready`` manifest/plan wiring is guarded behind: the
planner verdict (provider / config_type / extra / required_env / blocking-error)
MUST match the ``create_session`` outcome.

The roles split into two parity shapes:

* **Synchronous (stt/tts/vad/noise_reducer/echo_canceller)** — a missing
  env/extra makes ``create_session`` fail at construction, so the blocking
  direction is provable BOTH ways (plan blocks AND ``create_session`` raises).
* **Deferred (transport/agent)** — resolution imports user/SDK code at
  CONNECTION time, not at ``create_session`` construction, so the
  "construction raises" shape does not apply. The planner is side-effect-free
  by design (it never imports the transport SDK or the agent module — see
  ``tests/planning/test_boundary.py`` / ``tests/project/test_boundary.py``), so:
  - **transport** — the planner ``find_spec``-checks the transport extra and
    BLOCKS readiness when it is absent (the correct verdict: the server can't
    truly serve), even though ``create_session`` *construction* defers the
    ``require_module`` to the first connection.
  - **agent** — the planner validates the ``python:`` selection but CANNOT
    statically import/invoke the agent factory, so an unresolvable reference is
    NOT a static blocking error; ``manifest.resolve_agent`` raises
    ``EASYCAT_E605`` only at connection time. This is a documented carve-out
    (resolving the agent on every readiness poll would import + invoke the
    user's factory, which a health probe must not do).

Strategy (offline + side-effect-free):

* **success** — env + extra present: ``create_session`` SUCCEEDS and the plan
  reports no blocking error.
* **missing env** — unset the env var: ``create_session`` raises the credential
  error (``EASYCAT_E203``) AND the plan lists that role's ``required_env`` in
  ``missing_env`` / ``blocking_errors``.
* **missing extra** — hand the planner a ``ProbeEnvironment`` in which the
  extra's probe module is absent AND make the matching ``require_module`` raise
  for ``create_session`` (no uninstall): the plan lists the extra in
  ``missing_extras`` AND ``create_session`` raises the missing-extra error. The
  planner side is DATA, not a monkeypatched interpreter, so the verdict is the
  same on a bare dev-group machine and a fully installed one.

The vad-string coercion fix (``silero`` round-trips identically through both
paths) is covered here too.
"""

from __future__ import annotations

import pytest

from easycat.config import EasyConfig, create_session
from easycat.errors import EasyCatError
from easycat.planning import build_provider_plan
from easycat.planning._resolution import ProbeEnvironment
from easycat.planning.provider_plan import _plan_with_probe
from easycat.vad import VADConfig

_ENV = {"OPENAI_API_KEY": "sk-parity-test"}


class _Agent:
    """A minimal non-noop agent so ``create_session`` passes provider validation."""

    async def run(self, text: str) -> str:
        return "ok"


def _base_config(**overrides: object) -> EasyConfig:
    kwargs: dict[str, object] = {
        "stt": "openai",
        "tts": "openai",
        "vad": VADConfig(backend="silero"),
        "openai_api_key": "sk-parity-test",
        "agent": _Agent(),
        "debug": "off",
    }
    kwargs.update(overrides)
    return EasyConfig(**kwargs)


def _require_extras(*modules: str) -> None:
    """Skip unless every named provider probe module is importable.

    The parity gate exercises the REAL ``create_session`` (and the planner's
    ``find_spec`` extra checks) against installed providers, so a "no blocking
    error" / "create_session succeeds" assertion can only be verified when those
    extras are present. Project CI's ``validate quick`` lane syncs only
    ``--group dev`` (no extras), so these assertions must skip there rather than
    fail — mirroring the ``pytest.importorskip`` convention already used below.
    """
    for module in modules:
        pytest.importorskip(module)


# ── Success parity: all 7 roles resolve, create_session succeeds ─────


def test_parity_success_all_seven_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    _require_extras("onnxruntime", "sounddevice")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parity-test")
    config = _base_config()
    plan = build_provider_plan(config, environ=_ENV)

    # All seven roles are present in the plan.
    assert set(plan.selected) == {
        "stt",
        "tts",
        "vad",
        "transport",
        "agent",
        "noise_reducer",
        "echo_canceller",
    }
    # No blocking error -> create_session must succeed.
    assert not plan.has_blocking_errors, plan.blocking_errors()

    session = create_session(config)
    assert session is not None
    # The plan's role verdicts match the configured pipeline.
    assert plan.selected["stt"].config_type == "OpenAISTTConfig"
    assert plan.selected["stt"].required_env == "OPENAI_API_KEY"
    assert plan.selected["tts"].config_type == "OpenAITTSConfig"
    assert plan.selected["vad"].provider == "silero"
    assert plan.selected["vad"].config_type == "VADConfig"
    assert plan.selected["transport"].provider == "local"
    assert plan.selected["agent"].provider == "python"


# ── Missing-env parity (stt/tts) ─────────────────────────────────────


def test_parity_missing_env_blocks_both_planner_and_create_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    # The TTS side has a key so the ONLY blocking gap is the deepgram STT key.

    # Planner side: a manifest VoiceProfile is read DIRECTLY (no EasyConfig
    # construction, so no premature E203). The deepgram STT required env is
    # reported missing -> blocking.
    from easycat.project.schema import VoiceProfile

    profile = VoiceProfile(name="default", transport="local", stt="deepgram", tts="openai")
    plan = build_provider_plan(profile, environ={"OPENAI_API_KEY": "sk-parity-test"})
    assert "DEEPGRAM_API_KEY" in plan.missing_env
    assert plan.has_blocking_errors
    assert "missing_env:DEEPGRAM_API_KEY" in plan.blocking_errors()

    # create_session path: the same missing deepgram credential raises
    # EASYCAT_E203 at EasyConfig construction (the credential gate create_session
    # relies on). The planner verdict (DEEPGRAM_API_KEY blocking) matches.
    with pytest.raises(EasyCatError) as excinfo:
        EasyConfig(
            stt="deepgram",
            tts="openai",
            openai_api_key="sk-parity-test",
            agent=_Agent(),
            debug="off",
        )
    assert excinfo.value.code == "EASYCAT_E203"


def test_parity_openai_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # No openai key at all -> both stt and tts report OPENAI_API_KEY missing.
    plan = build_provider_plan(
        EasyConfig(stt="openai", tts="openai", openai_api_key="sk-x", agent=_Agent(), debug="off"),
        environ={},
    )
    assert "OPENAI_API_KEY" in plan.missing_env
    assert plan.has_blocking_errors

    # create_session path: EasyConfig with no key resolved raises E203.
    with pytest.raises(EasyCatError) as excinfo:
        EasyConfig(agent=_Agent(), debug="off")
    assert excinfo.value.code == "EASYCAT_E203"


# ── Missing-extra parity, per non-catalog role ───────────────────────


def _plan_without(config: object, *probe_modules: str) -> object:
    """Plan ``config`` against a snapshot where ``probe_modules`` are absent.

    Every other probe module reads as present, so the planner verdict is a
    function of the named absence alone. ``_plan_with_probe`` is the seam
    ``build_provider_plan`` itself calls with the process snapshot — nothing
    about resolution changes, only where the probe answers come from.
    """
    return _plan_with_probe(
        config,  # type: ignore[arg-type]
        probe=ProbeEnvironment.fake(env=_ENV, unavailable=probe_modules, default=True),
    )


def test_parity_missing_vad_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parity-test")
    config = _base_config()

    # Planner: simulate the silero-vad probe module (onnxruntime) absent.
    plan = _plan_without(config, "onnxruntime")
    assert "silero-vad" in plan.missing_extras
    assert plan.has_blocking_errors

    # create_session: make Silero's require_module raise the missing-extra error.
    from easycat.vad import silero

    def boom(module_name: str, **kwargs: object) -> object:
        raise ImportError(f"Silero VAD ONNX requires the {module_name} package.")

    monkeypatch.setattr(silero, "require_module", boom)
    with pytest.raises((ImportError, RuntimeError)):
        create_session(config)


def test_parity_missing_echo_canceller_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parity-test")
    # Enable AEC explicitly with fallback_policy="error" so create_session fails
    # hard (parity: planner reports the missing extra as blocking).
    from easycat.echo_cancellation import EchoCancellationConfig

    config = _base_config(
        echo_cancellation=EchoCancellationConfig(enabled=True, fallback_policy="error"),
    )

    plan = _plan_without(config, "livekit")
    assert "aec" in plan.missing_extras
    assert plan.has_blocking_errors

    import easycat.echo_cancellation as aec

    def boom(module_name: str, **kwargs: object) -> object:
        raise ImportError(f"Echo cancellation requires the {module_name} package.")

    monkeypatch.setattr(aec, "require_module", boom)
    with pytest.raises((ImportError, RuntimeError)):
        create_session(config)


def test_parity_passthrough_aec_extra_missing_is_warning_not_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The DEFAULT fallback_policy is "passthrough": with livekit absent
    # create_session degrades to PassthroughAEC instead of raising, so the
    # planner must NOT block /health/ready — it reports a non-blocking warning.
    _require_extras("onnxruntime", "sounddevice")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parity-test")
    from easycat.echo_cancellation import EchoCancellationConfig

    config = _base_config(echo_cancellation=EchoCancellationConfig(enabled=True))

    plan = _plan_without(config, "livekit")
    assert "aec" not in plan.missing_extras
    assert not plan.has_blocking_errors, plan.blocking_errors()
    assert any("degraded" in warning for warning in plan.warnings)

    # create_session degrades to passthrough (no raise) when the livekit import
    # fails under the passthrough policy — matching the non-blocking plan.
    import easycat.echo_cancellation as aec

    def boom(module_name: str, **kwargs: object) -> object:
        raise ImportError(f"Echo cancellation requires the {module_name} package.")

    monkeypatch.setattr(aec, "require_module", boom)
    session = create_session(config)
    assert session is not None


def test_parity_browser_profile_aec_extra_missing_is_warning_not_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A manifest browser profile auto-enables AEC with the passthrough fallback
    # (the manifest cannot pick "error"), so a missing aec extra must stay a
    # warning — otherwise /health/ready would reject a deployable browser server.
    _require_extras("onnxruntime", "aiortc")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parity-test")
    from easycat.project.schema import VoiceProfile

    profile = VoiceProfile(name="default", transport="webrtc", stt="openai", tts="openai")

    plan = _plan_without(profile, "livekit")
    assert plan.selected["echo_canceller"].provider == "livekit"
    assert "aec" not in plan.missing_extras
    assert not plan.has_blocking_errors, plan.blocking_errors()
    assert any("degraded" in warning for warning in plan.warnings)


def test_parity_missing_noise_reducer_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parity-test")
    from easycat.noise_reduction import NoiseReducerConfig

    config = _base_config(
        noise_reduction=NoiseReducerConfig(backend="rnnoise", fallback_policy="error"),
        enable_noise_reduction=True,
    )

    plan = _plan_without(config, "pyrnnoise")
    assert "rnnoise" in plan.missing_extras
    assert plan.has_blocking_errors

    import easycat.noise_reduction as nr

    def boom(module_name: str, **kwargs: object) -> object:
        raise ImportError(f"RNNoise requires the {module_name} package.")

    monkeypatch.setattr(nr, "require_module", boom)
    with pytest.raises((ImportError, RuntimeError)):
        create_session(config)


def test_parity_auto_passthrough_noise_reducer_missing_is_warning_not_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The DEFAULT noise reducer is backend="auto" + fallback_policy="passthrough":
    # with no backend installed, create_session degrades to a passthrough reducer
    # instead of raising, so the planner must NOT block /health/ready (mirroring
    # the AEC passthrough case). Only an explicit backend="rnnoise" or
    # fallback_policy="error" blocks (covered above).
    _require_extras("onnxruntime", "sounddevice")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parity-test")
    # Default NoiseReducerConfig is backend="auto" + fallback_policy="passthrough".
    config = _base_config(enable_noise_reduction=True)

    # Both auto-chain backends absent: rnnoise probe gone for the planner.
    plan = _plan_without(config, "pyrnnoise")
    assert "rnnoise" not in plan.missing_extras
    assert not plan.has_blocking_errors, plan.blocking_errors()
    assert any("degraded" in warning for warning in plan.warnings)

    # create_session degrades to PassthroughNoiseReducer (no raise) when neither
    # Krisp nor RNNoise is importable under the passthrough policy.
    import easycat.noise_reduction as nr

    def boom(module_name: str, **kwargs: object) -> object:
        raise ImportError(f"RNNoise requires the {module_name} package.")

    monkeypatch.setattr(nr, "require_module", boom)
    session = create_session(config)
    assert session is not None


def test_parity_auto_vad_satisfied_by_union_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``create_vad('auto')`` tries Silero -> FunASR -> TEN -> Krisp, so it is
    # satisfiable by ANY of {onnxruntime, ten_vad, krisp_audio}. The planner must
    # not block the auto VAD on a single missing extra when another union member
    # is importable — here onnxruntime is gone but ten_vad remains.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parity-test")
    pytest.importorskip("ten_vad")
    config = _base_config(vad="auto")

    plan = _plan_without(config, "onnxruntime")
    assert plan.selected["vad"].provider == "auto"
    assert "silero-vad" not in plan.missing_extras
    assert not plan.has_blocking_errors, plan.blocking_errors()


def test_parity_auto_vad_blocks_only_when_whole_union_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # When NONE of the auto-chain union is importable, ``create_vad('auto')``
    # raises, so the planner must block and recommend the silero-vad extra.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parity-test")
    config = _base_config(vad="auto")

    plan = _plan_without(config, "onnxruntime", "ten_vad", "krisp_audio")
    assert "silero-vad" in plan.missing_extras
    assert plan.has_blocking_errors


# ── Deferred-resolution carve-outs (transport / agent) ───────────────


def test_parity_transport_extra_blocks_readiness_even_though_construction_defers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Transport SDK import is DEFERRED to the first connection (webrtc calls
    # require_module('aiortc') at connect time, not at EasyConfig construction),
    # so the "construction raises" parity shape does not apply. The planner still
    # find_spec-checks the transport extra and BLOCKS readiness when it is
    # absent — the correct verdict, since the server can't actually serve. This
    # locks the blocking direction for the deferred transport role.
    # ``create_session`` below builds the browser default auto-VAD for real, so
    # gate on its backend being installed (the plan assertions hold regardless).
    _require_extras("onnxruntime")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parity-test")
    config = EasyConfig.browser(  # WebRTCTransportConfig -> webrtc extra (aiortc)
        openai_api_key="sk-parity-test", agent=_Agent(), debug="off"
    )

    plan = _plan_without(config, "aiortc")
    assert plan.selected["transport"].provider == "webrtc"
    assert "webrtc" in plan.missing_extras
    assert plan.has_blocking_errors

    # Construction itself does NOT raise (aiortc import is deferred) — documented:
    # the transport failure surfaces at connection time, not here.
    assert create_session(config) is not None


def test_parity_agent_is_a_deferred_carveout_not_a_static_blocker() -> None:
    _require_extras("onnxruntime", "sounddevice")
    # The planner is side-effect-free: it never imports the agent module, so an
    # UNRESOLVABLE ``python:`` reference is NOT a static blocking error. The
    # divergence (it raises EASYCAT_E605 at connection time via resolve_agent) is
    # an intentional, documented carve-out — locked here so the gate's role
    # coverage stays honest.
    from easycat.project.manifest import ProjectManifest
    from easycat.project.schema import ProjectSection, ServerSection, VoiceProfile

    profile = VoiceProfile(
        name="default",
        transport="local",
        stt="openai",
        tts="openai",
        agent="python:easycat_nonexistent_module_xyz:create_agent",
    )
    plan = build_provider_plan(profile, environ=_ENV)
    # The agent role resolves to the python backend with no env/extra gap, and
    # the unresolvable target does NOT block the static plan.
    assert plan.selected["agent"].provider == "python"
    assert not plan.has_blocking_errors, plan.blocking_errors()

    # The same unresolvable reference DOES raise at connection-time resolution.
    manifest = ProjectManifest(
        project=ProjectSection(),
        server=ServerSection(),
        profiles={"default": profile},
    )
    with pytest.raises(EasyCatError) as excinfo:
        manifest.resolve_agent("default")
    assert excinfo.value.code == "EASYCAT_E605"


# ── VAD string-coercion regression (round-trips identically) ─────────


def test_parity_vad_string_coercion_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    # The create_session leg below builds a real Silero VAD, so gate on its
    # backend (the to_easyconfig coercion + planner assertions hold regardless).
    _require_extras("onnxruntime")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parity-test")
    # The manifest converter coerces vad='silero' -> VADConfig(backend='silero')
    # so create_session no longer raises AttributeError, and the planner reports
    # the same vad selection.
    from easycat.project.manifest import ProjectManifest
    from easycat.project.schema import ProjectSection, ServerSection, VoiceProfile

    profile = VoiceProfile(name="default", transport="webrtc", vad="silero")
    manifest = ProjectManifest(
        project=ProjectSection(),
        server=ServerSection(),
        profiles={"default": profile},
    )
    config = manifest.to_easyconfig("default", resolve_agent=False)
    assert isinstance(config.vad, VADConfig)
    assert config.vad.backend == "silero"

    plan = build_provider_plan(profile, environ=_ENV)
    assert plan.selected["vad"].provider == "silero"
    assert plan.selected["vad"].config_type == "VADConfig"

    # The coerced config drives create_session without the str-backend crash.
    config_with_agent = EasyConfig.browser(
        vad=VADConfig(backend="silero"),
        openai_api_key="sk-parity-test",
        agent=_Agent(),
        debug="off",
    )
    session = create_session(config_with_agent)
    assert session is not None
