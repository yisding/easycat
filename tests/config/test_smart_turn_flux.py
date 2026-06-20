from __future__ import annotations

import logging
import math

import pytest

from easycat import (
    AudioProcessingConfig,
    EasyConfig,
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
        # The local transport now enables smart-turn by default, which keeps
        # VAD on; pin it off so this test exercises the flux-disables-VAD path.
        smart_turn=False,
        agent=_DummyAgent(),
    )

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


def test_create_session_derives_endpoint_threshold_from_grouped_audio_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_audio_backends(monkeypatch)
    config = EasyConfig(
        openai_api_key="test-key",
        audio_processing=AudioProcessingConfig(
            smart_turn=True,
            smart_turn_sensitivity=0.75,
        ),
        agent=_DummyAgent(),
    )

    assert isinstance(config.audio_processing.smart_turn, SmartTurnConfig)
    assert config.smart_turn.threshold == pytest.approx(0.25)

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
    # the now-default debug="full" spins up the journal/warmup/debugger path,
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
