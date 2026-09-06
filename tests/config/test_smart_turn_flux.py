from __future__ import annotations

import logging
import math

import pytest

from easycat import (
    EasyConfig,
    STTProviderConfig,
    create_session,
)
from easycat.config import TelephonyConfig
from easycat.smart_turn import SmartTurnConfig
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.tts.openai_tts import OpenAITTSConfig
from easycat.turn_manager import TurnManagerConfig, TurnMode
from tests.config._helpers import (
    _DummyAgent,
    _stub_audio_backends,
)


def test_create_session_disables_vad_for_deepgram_flux(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "easycat.config._factory.create_vad",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("create_vad should not be called")
        ),
    )

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    monkeypatch.setattr(
        "easycat.config._factory.create_noise_reducer", lambda *_args, **_kwargs: _NoiseReducer()
    )

    config = EasyConfig(
        stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
        tts=OpenAITTSConfig(api_key="test-key"),
        agent=_DummyAgent(),
    )

    # Flux keeps smart-turn off by default (even on the local-mic preset), so
    # it drives turns from STT finals and never wires a Silero VAD.
    assert config.smart_turn.enabled is False

    session = create_session(config)

    assert session._enable_vad is False
    assert session._auto_turn_from_stt_final is True


def test_create_session_disables_vad_for_deepgram_flux_string_shortcut(
    monkeypatch: pytest.MonkeyPatch,
):
    """The ``"deepgram/flux"`` string spec must match the typed config.

    Smart-turn is normalized after string shortcuts resolve, so the string
    form is recognized as Flux and keeps smart-turn (and the VAD) off — the
    same as passing a ``DeepgramSTTConfig`` directly.
    """
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
    monkeypatch.setattr(
        "easycat.config._factory.create_vad",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("create_vad should not be called")
        ),
    )

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    monkeypatch.setattr(
        "easycat.config._factory.create_noise_reducer", lambda *_args, **_kwargs: _NoiseReducer()
    )

    config = EasyConfig(
        stt="deepgram/flux-general-en",
        tts=OpenAITTSConfig(api_key="test-key"),
        agent=_DummyAgent(),
    )

    assert config.smart_turn.enabled is False

    session = create_session(config)

    assert session._enable_vad is False
    assert session._auto_turn_from_stt_final is True


def test_named_provider_config_preserves_flux_endpointing_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "easycat.config._factory.create_vad",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("create_vad should not be called")
        ),
    )

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    monkeypatch.setattr(
        "easycat.config._factory.create_noise_reducer", lambda *_args, **_kwargs: _NoiseReducer()
    )
    config = EasyConfig(
        stt=STTProviderConfig(
            provider="deepgram",
            api_key="test-key",
            params={"model": "flux-general-en"},
        ),
        tts=OpenAITTSConfig(api_key="test-key"),
        agent=_DummyAgent(),
    )

    assert isinstance(config.stt, DeepgramSTTConfig)
    assert config.smart_turn.enabled is False

    session = create_session(config)

    assert session._enable_vad is False
    assert session._auto_turn_from_stt_final is True


def test_create_session_keeps_flux_auto_turn_disabled_for_push_to_talk(
    monkeypatch: pytest.MonkeyPatch,
):
    create_vad_called = False

    class _VAD:
        async def process(self, chunk):
            if False:
                yield chunk

        def configure(self, **kwargs):
            pass

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    def _create_vad(*_args, **_kwargs):
        nonlocal create_vad_called
        create_vad_called = True
        return _VAD()

    monkeypatch.setattr("easycat.config._factory.create_vad", _create_vad)
    monkeypatch.setattr(
        "easycat.config._factory.create_noise_reducer", lambda *_args, **_kwargs: _NoiseReducer()
    )

    config = EasyConfig(
        stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
        tts=OpenAITTSConfig(api_key="test-key"),
        turn_taking=TurnManagerConfig(mode=TurnMode.PUSH_TO_TALK),
        agent=_DummyAgent(),
    )

    session = create_session(config)

    assert create_vad_called is True
    assert session._enable_vad is True
    assert session._auto_turn_from_stt_final is False


def test_create_session_keeps_vad_enabled_for_flux_when_smart_turn_enabled(
    monkeypatch: pytest.MonkeyPatch,
):
    create_vad_called = False

    class _VAD:
        async def process(self, chunk):
            if False:
                yield chunk

        def configure(self, **kwargs):
            pass

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    def _create_vad(*_args, **_kwargs):
        nonlocal create_vad_called
        create_vad_called = True
        return _VAD()

    monkeypatch.setattr("easycat.config._factory.create_vad", _create_vad)
    monkeypatch.setattr(
        "easycat.config._factory.create_noise_reducer", lambda *_args, **_kwargs: _NoiseReducer()
    )

    config = EasyConfig(
        stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
        tts=OpenAITTSConfig(api_key="test-key"),
        smart_turn=SmartTurnConfig(enabled=True),
        agent=_DummyAgent(),
    )

    session = create_session(config)

    assert create_vad_called is True
    assert session._enable_vad is True
    assert session._auto_turn_from_stt_final is False


def test_create_session_derives_endpoint_threshold_from_smart_turn(
    monkeypatch: pytest.MonkeyPatch,
):
    """When endpoint_threshold is left None, it is derived from smart_turn.threshold."""
    _stub_audio_backends(monkeypatch)
    config = EasyConfig(
        openai_api_key="test-key",
        smart_turn=SmartTurnConfig(enabled=True, threshold=0.7),
        agent=_DummyAgent(),
    )

    session = create_session(config)

    assert session._turn_manager._config.endpoint_threshold == 0.7
    # The source config must not be mutated.
    assert config.turn_taking.endpoint_threshold is None


def test_easyconfig_accepts_smart_turn_bool_shortcut() -> None:
    config = EasyConfig(openai_api_key="test-key", smart_turn=True)

    assert isinstance(config.smart_turn, SmartTurnConfig)
    assert config.smart_turn.enabled is True
    assert config.smart_turn.threshold == pytest.approx(0.5)


def test_easyconfig_smart_turn_sensitivity_enables_and_sets_threshold() -> None:
    config = EasyConfig(openai_api_key="test-key", smart_turn_sensitivity=0.8)

    assert isinstance(config.smart_turn, SmartTurnConfig)
    assert config.smart_turn.enabled is True
    assert config.smart_turn.threshold == pytest.approx(0.2)


def test_create_session_derives_endpoint_threshold_from_smart_turn_sensitivity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_audio_backends(monkeypatch)
    config = EasyConfig(
        openai_api_key="test-key",
        smart_turn=True,
        smart_turn_sensitivity=0.75,
        agent=_DummyAgent(),
    )

    session = create_session(config)

    assert session._turn_manager._config.endpoint_threshold == pytest.approx(0.25)


@pytest.mark.parametrize("value", [-0.1, 1.1, math.nan, True, "eager"])
def test_easyconfig_smart_turn_sensitivity_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError, match="smart_turn_sensitivity"):
        EasyConfig(openai_api_key="test-key", smart_turn_sensitivity=value)  # type: ignore[arg-type]


def test_easyconfig_smart_turn_false_rejects_sensitivity() -> None:
    with pytest.raises(ValueError, match="smart_turn_sensitivity requires smart_turn=True"):
        EasyConfig(openai_api_key="test-key", smart_turn=False, smart_turn_sensitivity=0.5)


def test_create_session_endpoint_threshold_overrides_smart_turn(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """An explicit manager endpoint_threshold wins and warns when it diverges."""
    _stub_audio_backends(monkeypatch)
    # debug="off" keeps this caplog assertion focused on the threshold warning:
    # Explicit debug="full" spins up the durable journal/warmup path,
    # whose logging setup detaches caplog's handler before the warning lands.
    config = EasyConfig(
        openai_api_key="test-key",
        turn_taking=TurnManagerConfig(endpoint_threshold=0.8),
        smart_turn=SmartTurnConfig(enabled=True, threshold=0.5),
        agent=_DummyAgent(),
        debug="off",
    )

    with caplog.at_level(logging.WARNING, logger="easycat.config"):
        session = create_session(config)

    assert session._turn_manager._config.endpoint_threshold == 0.8
    assert any("endpoint_threshold" in rec.message for rec in caplog.records)


def test_create_session_keeps_vad_enabled_for_flux_when_voicemail_detector_enabled(
    monkeypatch: pytest.MonkeyPatch,
):
    create_vad_called = False

    class _VAD:
        async def process(self, chunk):
            if False:
                yield chunk

        def configure(self, **kwargs):
            pass

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    def _create_vad(*_args, **_kwargs):
        nonlocal create_vad_called
        create_vad_called = True
        return _VAD()

    monkeypatch.setattr("easycat.config._factory.create_vad", _create_vad)
    monkeypatch.setattr(
        "easycat.config._factory.create_noise_reducer", lambda *_args, **_kwargs: _NoiseReducer()
    )

    config = EasyConfig(
        stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
        tts=OpenAITTSConfig(api_key="test-key"),
        telephony=TelephonyConfig(enable_voicemail_detector=True),
        agent=_DummyAgent(),
    )

    session = create_session(config)

    assert create_vad_called is True
    assert session._enable_vad is True
    assert session._auto_turn_from_stt_final is False


# ── Late STT mutation (gh 1027) ──────────────────────────────────


def _stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Record whether the VAD stage was built, without loading real backends."""
    created: list[bool] = []

    class _VAD:
        async def process(self, chunk):
            if False:
                yield chunk

        def configure(self, **kwargs):
            pass

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    def _create_vad(*_args, **_kwargs):
        created.append(True)
        return _VAD()

    monkeypatch.setattr("easycat.config._factory.create_vad", _create_vad)
    monkeypatch.setattr(
        "easycat.config._factory.create_noise_reducer", lambda *_args, **_kwargs: _NoiseReducer()
    )
    return created


def test_mutating_stt_to_flux_before_create_session_disables_smart_turn(
    monkeypatch: pytest.MonkeyPatch,
):
    """``cfg.stt = "deepgram/flux"`` after construction must re-derive the default.

    ``__post_init__`` materializes ``smart_turn=None`` into a concrete
    ``SmartTurnConfig``, so the "left unset" signal is gone by the time
    ``create_session`` calls ``_validate_for_session``. Without a recompute
    the mic preset's smart-turn stayed on, ``auto_turn_from_stt_final``
    stayed False, and EasyCat's VAD double-endpointed against Flux's own.
    """
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
    created = _stub_pipeline(monkeypatch)

    config = EasyConfig(
        stt="openai/gpt-4o-transcribe",
        tts=OpenAITTSConfig(api_key="test-key"),
        openai_api_key="test-key",
        agent=_DummyAgent(),
    )
    assert config.smart_turn.enabled is True  # local-mic preset default

    config.stt = "deepgram/flux-general-en"
    session = create_session(config)

    assert session._enable_vad is False
    assert session._auto_turn_from_stt_final is True
    assert created == []


def test_validate_for_session_recomputes_smart_turn_after_stt_mutation(
    monkeypatch: pytest.MonkeyPatch,
):
    """The recompute happens on the config itself, not only inside the build."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")

    config = EasyConfig(
        stt="openai/gpt-4o-transcribe",
        tts=OpenAITTSConfig(api_key="test-key"),
        openai_api_key="test-key",
        agent=_DummyAgent(),
    )
    assert config.smart_turn.enabled is True

    config.stt = "deepgram/flux-general-en"
    config._validate_for_session()

    assert config.smart_turn.enabled is False


def test_mutating_stt_away_from_flux_restores_the_mic_preset_default(
    monkeypatch: pytest.MonkeyPatch,
):
    """The recompute is symmetric: leaving Flux re-enables the default."""
    created = _stub_pipeline(monkeypatch)

    config = EasyConfig(
        stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
        tts=OpenAITTSConfig(api_key="test-key"),
        openai_api_key="test-key",
        agent=_DummyAgent(),
    )
    assert config.smart_turn.enabled is False

    config.stt = "openai/gpt-4o-transcribe"
    session = create_session(config)

    assert session._enable_vad is True
    assert session._auto_turn_from_stt_final is False
    assert created == [True]


@pytest.mark.parametrize("explicit", [True, SmartTurnConfig(enabled=True)])
def test_explicit_smart_turn_survives_a_late_stt_mutation(
    monkeypatch: pytest.MonkeyPatch,
    explicit,
):
    """Only an untouched default is re-derived; an explicit choice wins."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-key")
    _stub_pipeline(monkeypatch)

    config = EasyConfig(
        stt="openai/gpt-4o-transcribe",
        tts=OpenAITTSConfig(api_key="test-key"),
        openai_api_key="test-key",
        smart_turn=explicit,
        agent=_DummyAgent(),
    )

    config.stt = "deepgram/flux-general-en"
    session = create_session(config)

    assert config.smart_turn.enabled is True
    assert session._auto_turn_from_stt_final is False


def test_smart_turn_assigned_after_construction_is_normalized():
    """A late ``cfg.smart_turn = True`` resolves to a ``SmartTurnConfig``.

    ``_normalized_smart_turn`` asserts the field is already typed, so a bool
    assigned after construction previously reached the factory unnormalized.
    """
    config = EasyConfig(
        stt=DeepgramSTTConfig(api_key="test-key", model="flux-general-en"),
        tts=OpenAITTSConfig(api_key="test-key"),
        agent=_DummyAgent(),
    )
    assert config.smart_turn.enabled is False

    config.smart_turn = True
    config._validate_for_session()

    assert isinstance(config.smart_turn, SmartTurnConfig)
    assert config.smart_turn.enabled is True
