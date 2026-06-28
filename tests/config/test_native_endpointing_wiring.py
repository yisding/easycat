"""Native provider-side endpointing must disable EasyCat's own VAD + commits.

When the chosen STT does its own turn detection (Cartesia ink-2, ElevenLabs
realtime VAD — alongside the existing Deepgram Flux), ``create_session`` must
derive turns from STT finals (``auto_turn_from_stt_final``) and NOT also build
the Silero VAD stage, or the two endpointers would double-fire and emit
duplicate FINAL transcripts.
"""

from __future__ import annotations

import pytest

from easycat import EasyConfig, create_session
from easycat.stt.cartesia_provider import CartesiaSTTConfig
from easycat.stt.elevenlabs_provider import ElevenLabsSTTConfig
from easycat.tts.openai_tts import OpenAITTSConfig
from tests.config._helpers import _DummyAgent, _stub_audio_backends


def _vad_must_not_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "easycat.config._factory.create_vad",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("create_vad should not be called")),
    )

    class _NoiseReducer:
        async def process(self, chunk):
            return chunk

    monkeypatch.setattr(
        "easycat.config._factory.create_noise_reducer", lambda *_a, **_k: _NoiseReducer()
    )


def _build(stt) -> EasyConfig:
    return EasyConfig(stt=stt, tts=OpenAITTSConfig(api_key="k"), agent=_DummyAgent())


# ── Cartesia ink-2 (native semantic turn detection) ──────────────────


def test_cartesia_ink2_disables_vad_and_auto_turns(monkeypatch):
    _vad_must_not_build(monkeypatch)
    config = _build(CartesiaSTTConfig(api_key="k"))  # default resolves to ink-2 (en)
    assert config.smart_turn.enabled is False

    session = create_session(config)
    assert session._enable_vad is False
    assert session._auto_turn_from_stt_final is True


def test_cartesia_ink2_string_shortcut_disables_vad(monkeypatch):
    monkeypatch.setenv("CARTESIA_API_KEY", "k")
    _vad_must_not_build(monkeypatch)
    config = _build("cartesia/ink-2")

    session = create_session(config)
    assert session._enable_vad is False
    assert session._auto_turn_from_stt_final is True


def test_cartesia_ink_whisper_keeps_local_vad(monkeypatch):
    _stub_audio_backends(monkeypatch)
    config = _build(CartesiaSTTConfig(api_key="k", model="ink-whisper"))

    session = create_session(config)
    assert session._enable_vad is True
    assert session._auto_turn_from_stt_final is False


def test_cartesia_non_english_default_keeps_local_vad(monkeypatch):
    # Non-English resolves to ink-whisper (ink-2 is English-only), which has no
    # native turn detection, so EasyCat's VAD must stay on.
    _stub_audio_backends(monkeypatch)
    config = _build(CartesiaSTTConfig(api_key="k", language="fr"))

    session = create_session(config)
    assert session._enable_vad is True
    assert session._auto_turn_from_stt_final is False


# ── ElevenLabs realtime VAD commit strategy ──────────────────────────


def test_elevenlabs_realtime_vad_disables_vad_and_auto_turns(monkeypatch):
    _vad_must_not_build(monkeypatch)
    config = _build(ElevenLabsSTTConfig(api_key="k"))  # default: realtime + vad
    assert config.smart_turn.enabled is False

    session = create_session(config)
    assert session._enable_vad is False
    assert session._auto_turn_from_stt_final is True


def test_elevenlabs_manual_commit_keeps_local_vad(monkeypatch):
    _stub_audio_backends(monkeypatch)
    config = _build(ElevenLabsSTTConfig(api_key="k", realtime_commit_strategy="manual"))

    session = create_session(config)
    assert session._enable_vad is True
    assert session._auto_turn_from_stt_final is False


def test_elevenlabs_batch_keeps_local_vad(monkeypatch):
    _stub_audio_backends(monkeypatch)
    config = _build(ElevenLabsSTTConfig(api_key="k", mode="batch"))

    session = create_session(config)
    assert session._enable_vad is True
    assert session._auto_turn_from_stt_final is False
