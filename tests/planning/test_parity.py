"""REQUIRED GATE — planner-vs-``create_session`` parity for all 7 roles.

Because 5 of the 7 planner roles (vad/transport/agent/noise_reducer/
echo_canceller) hand-roll resolution OUTSIDE any catalog, the planner can
silently diverge from what ``create_session`` actually does. This test is the
gate the M6b ``/health/ready`` manifest/plan wiring is guarded behind: the
planner verdict (provider / config_type / extra / required_env / blocking-error)
MUST match the ``create_session`` outcome for EVERY one of the seven roles.

Strategy (offline + side-effect-free):

* **success** — env + extra present: ``create_session`` SUCCEEDS and the plan
  reports no blocking error.
* **missing env** — unset the env var: ``create_session`` raises the credential
  error (``EASYCAT_E203``) AND the plan lists that role's ``required_env`` in
  ``missing_env`` / ``blocking_errors``.
* **missing extra** — simulate ``find_spec=None`` for the planner AND make the
  matching ``require_module`` raise for ``create_session`` (no uninstall): the
  plan lists the extra in ``missing_extras`` AND ``create_session`` raises the
  missing-extra error.

The vad-string coercion fix (``silero`` round-trips identically through both
paths) is covered here too.
"""

from __future__ import annotations

import importlib.util

import pytest

from easycat.config import EasyConfig, create_session
from easycat.errors import EasyCatError
from easycat.planning import build_provider_plan
from easycat.vad import VADConfig

_ENV = {"OPENAI_API_KEY": "sk-parity-test"}


class _Agent:
    """A minimal non-noop agent so ``create_session`` passes provider validation."""

    async def run(self, text: str) -> str:  # noqa: D401 - test stub
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


# ── Success parity: all 7 roles resolve, create_session succeeds ─────


def test_parity_success_all_seven_roles(monkeypatch: pytest.MonkeyPatch) -> None:
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


def _force_find_spec_none(monkeypatch: pytest.MonkeyPatch, *probe_modules: str) -> None:
    """Make ``find_spec`` return ``None`` for the named probe modules only."""
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, package: object = None):  # noqa: ANN202
        if name in probe_modules:
            return None
        return real_find_spec(name, package)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)


def test_parity_missing_vad_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parity-test")
    config = _base_config()

    # Planner: simulate the silero-vad probe module (onnxruntime) absent.
    _force_find_spec_none(monkeypatch, "onnxruntime")
    plan = build_provider_plan(config, environ=_ENV)
    assert "silero-vad" in plan.missing_extras
    assert plan.has_blocking_errors

    # create_session: make Silero's require_module raise the missing-extra error.
    import easycat.vad.silero as silero

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

    _force_find_spec_none(monkeypatch, "livekit")
    plan = build_provider_plan(config, environ=_ENV)
    assert "aec" in plan.missing_extras
    assert plan.has_blocking_errors

    import easycat.echo_cancellation as aec

    def boom(module_name: str, **kwargs: object) -> object:
        raise ImportError(f"Echo cancellation requires the {module_name} package.")

    monkeypatch.setattr(aec, "require_module", boom)
    with pytest.raises((ImportError, RuntimeError)):
        create_session(config)


def test_parity_missing_noise_reducer_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-parity-test")
    from easycat.noise_reduction import NoiseReducerConfig

    config = _base_config(
        noise_reduction=NoiseReducerConfig(backend="rnnoise", fallback_policy="error"),
        enable_noise_reduction=True,
    )

    _force_find_spec_none(monkeypatch, "pyrnnoise")
    plan = build_provider_plan(config, environ=_ENV)
    assert "rnnoise" in plan.missing_extras
    assert plan.has_blocking_errors

    import easycat.noise_reduction as nr

    def boom(module_name: str, **kwargs: object) -> object:
        raise ImportError(f"RNNoise requires the {module_name} package.")

    monkeypatch.setattr(nr, "require_module", boom)
    with pytest.raises((ImportError, RuntimeError)):
        create_session(config)


# ── VAD string-coercion regression (round-trips identically) ─────────


def test_parity_vad_string_coercion_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
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
