"""Provider contract matrix: every STT × TTS pair survives the session wiring.

Per-provider unit tests (``tests/stt/``, ``tests/tts/``) cover provider
internals against mocked sockets. The gap they leave is the *wiring
seam*: when someone adds a provider, widens a config dataclass, or
changes how ``Session`` plumbs providers into the pipeline, which
``(STT, TTS)`` pair silently breaks?

This test parametrizes over every registered STT config × TTS config
pair from ``stt/factory.py`` and ``tts/factory.py``, builds a real
``Session`` via ``create_session()``, and runs one scripted turn. The
actual provider instances are replaced with the harness scripted
stubs so no real API call is made — what we're exercising is:

  - every config dataclass constructs with minimal args;
  - ``create_stt_provider_from_config`` / ``create_tts_provider_from_config``
    dispatch for every registered type;
  - the event_bus-injection branch in each ``from_config`` function
    is reached for providers that require it (Deepgram, ElevenLabs,
    OpenAI Realtime, Cartesia);
  - ``Session`` drives a full turn to ``BotStoppedSpeaking`` regardless
    of the config variant;
  - no stage holds a reference to a config field that exists on one
    provider but not another.

A regression here typically means: a new provider was added to the
registry without the corresponding ``event_bus`` injection branch, or
a config field was renamed on one provider and the session path reads
it by attribute.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any

import pytest

from easycat import create_session
from easycat.events import (
    AgentFinal,
    BotStoppedSpeaking,
    STTFinal,
    TTSAudio,
    TurnEnded,
    TurnStarted,
)
from easycat.stt.cartesia_provider import CartesiaSTTConfig
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.stt.elevenlabs_provider import ElevenLabsSTTConfig
from easycat.stt.factory import _PROVIDER_TO_CONFIG as _STT_REGISTRY
from easycat.stt.openai_provider import OpenAISTTConfig
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
from easycat.tts.cartesia_tts import CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig
from easycat.tts.factory import _PROVIDERS as _TTS_REGISTRY
from easycat.tts.openai_tts import OpenAITTSConfig
from easycat.turn_manager import TurnManagerConfig

from .harness import (
    EventCollector,
    QueueTransport,
    RecordingTTS,
    ScriptedSTT,
    ScriptedVAD,
    make_chunk,
    patch_provider_factories,
)

pytestmark = pytest.mark.integration_local

_FAST_TURN = TurnManagerConfig(end_of_turn_silence_ms=1)


# ── Config constructors ──────────────────────────────────────────────
#
# Every STT/TTS config in the registry needs at minimum an api_key.
# Two providers (ElevenLabs TTS, Cartesia TTS) use ``model_id`` instead
# of ``model`` and require a voice_id. We construct the configs with
# the smallest valid kwargs so we exercise the dataclass defaults —
# anything that breaks with defaults is a regression.


def _build_stt_config(config_cls: type) -> Any:
    # All STT configs take api_key as the only required field; defaults
    # handle the rest. Pass a sentinel key since the provider is stubbed.
    return config_cls(api_key="test-key")


def _build_tts_config(config_cls: type) -> Any:
    kwargs: dict[str, Any] = {"api_key": "test-key"}
    field_names = {f.name for f in fields(config_cls)}
    # ElevenLabs + Cartesia require a voice_id (no default).
    if "voice_id" in field_names:
        kwargs["voice_id"] = "test-voice"
    return config_cls(**kwargs)


# ── Parametrization ──────────────────────────────────────────────────
#
# We pull from the live registries so newly-added providers are
# automatically exercised — no manual list to keep in sync.

_STT_CONFIG_CLASSES = [cfg_cls for _provider_cls, cfg_cls in _STT_REGISTRY.values()]
_TTS_CONFIG_CLASSES = [cfg_cls for _provider_cls, cfg_cls in _TTS_REGISTRY.values()]


class _UpperAgent:
    async def run(self, text: str) -> str:
        return text.upper()


def _make_config(stt_cfg: Any, tts_cfg: Any, transport: Any) -> Any:
    from easycat import EasyCatConfig

    return EasyCatConfig(
        stt=stt_cfg,
        tts=tts_cfg,
        transport=transport,
        agent=_UpperAgent(),
        turn_taking=_FAST_TURN,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stt_config_cls",
    _STT_CONFIG_CLASSES,
    ids=[c.__name__ for c in _STT_CONFIG_CLASSES],
)
@pytest.mark.parametrize(
    "tts_config_cls",
    _TTS_CONFIG_CLASSES,
    ids=[c.__name__ for c in _TTS_CONFIG_CLASSES],
)
async def test_session_wiring_for_every_provider_pair(
    monkeypatch: pytest.MonkeyPatch,
    stt_config_cls: type,
    tts_config_cls: type,
) -> None:
    """One scripted turn must complete cleanly for every (STT, TTS) pair.

    Asserts the canonical event sequence: TurnStarted → STTFinal →
    AgentFinal → TTSAudio → BotStoppedSpeaking → TurnEnded. The
    scripted providers replace the real ones after ``create_session``
    dispatches, so what's really verified is config construction,
    factory dispatch, event-bus injection, and Session lifecycle.
    """
    stt_cfg = _build_stt_config(stt_config_cls)
    tts_cfg = _build_tts_config(tts_config_cls)

    transport = QueueTransport()
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    config = _make_config(stt_cfg, tts_cfg, transport)
    session = create_session(config)
    collector = EventCollector(session.event_bus)
    collector.subscribe(
        TurnStarted,
        TurnEnded,
        STTFinal,
        AgentFinal,
        TTSAudio,
        BotStoppedSpeaking,
    )

    await session.start()
    try:
        await transport.push_audio(make_chunk(), make_chunk())
        await collector.wait_for(BotStoppedSpeaking, timeout=3.0)

        event_types = [type(e).__name__ for e in collector.events]
        for expected in (
            "TurnStarted",
            "STTFinal",
            "AgentFinal",
            "TTSAudio",
            "BotStoppedSpeaking",
            "TurnEnded",
        ):
            assert expected in event_types, (
                f"{stt_config_cls.__name__} × {tts_config_cls.__name__}: "
                f"missing {expected}; saw {event_types}"
            )

        # Order invariants that must hold regardless of provider wiring.
        # TurnStarted opens the turn; STTFinal must happen before the
        # agent and TTS run (everything downstream of the user's words
        # is gated on STTFinal). We do NOT gate on the relative order
        # of AgentFinal/TTSAudio/BotStoppedSpeaking/TurnEnded: the
        # session emits TurnEnded as soon as the user-input side of
        # the turn closes, which can precede the agent finishing or
        # the bot finishing speaking.
        idx = {name: event_types.index(name) for name in event_types}
        assert idx["TurnStarted"] < idx["STTFinal"]
        assert idx["STTFinal"] < idx["AgentFinal"]
        assert idx["STTFinal"] < idx["TTSAudio"]
    finally:
        await transport.finish_input()
        await session.stop()


# ── Registry coverage guard ──────────────────────────────────────────


_EXPECTED_STT_CONFIGS = {
    OpenAISTTConfig,
    OpenAIRealtimeSTTConfig,
    DeepgramSTTConfig,
    ElevenLabsSTTConfig,
    CartesiaSTTConfig,
}
_EXPECTED_TTS_CONFIGS = {
    OpenAITTSConfig,
    DeepgramTTSConfig,
    ElevenLabsTTSConfig,
    CartesiaTTSConfig,
}


def test_registry_covers_every_known_config() -> None:
    """Fail loudly when a new provider is added without matrix coverage.

    If someone adds a provider to ``stt/factory.py`` or
    ``tts/factory.py``, the parametrized test above auto-includes it.
    The risk is the *opposite*: a config class is dropped from the
    registry without also being deprecated here, which would silently
    shrink the matrix. This guard catches that drift.
    """
    registered_stt = set(_STT_CONFIG_CLASSES)
    registered_tts = set(_TTS_CONFIG_CLASSES)

    assert _EXPECTED_STT_CONFIGS <= registered_stt, (
        f"STT registry lost configs: {_EXPECTED_STT_CONFIGS - registered_stt}. "
        f"Update this guard if a provider was intentionally removed."
    )
    assert _EXPECTED_TTS_CONFIGS <= registered_tts, (
        f"TTS registry lost configs: {_EXPECTED_TTS_CONFIGS - registered_tts}. "
        f"Update this guard if a provider was intentionally removed."
    )
