"""Provider contract matrix: every STT × TTS pair survives the session wiring.

Per-provider unit tests (``tests/stt/``, ``tests/tts/``) cover provider
internals against mocked sockets. The gap they leave is the *wiring
seam* (factory/session wiring seam): when someone adds a provider, widens a config dataclass, or
changes how ``Session`` plumbs providers into the pipeline, which
``(STT, TTS)`` pair silently breaks?

This test parametrizes over every registered STT config × TTS config
pair from ``stt/factory.py`` and ``tts/factory.py`` and exercises the
seam in two phases per pair:

1. **Real factory dispatch + injection.** Calls the real
   ``create_stt_provider_from_config`` / ``create_tts_provider_from_config``
   with the config and a fresh ``EventBus``, asserts the returned
   provider's class matches the registry's ``_CONFIG_TO_PROVIDER`` map,
   and asserts the ``EventBus`` was injected into the provider's stored
   config for every type listed in the ``needs_event_bus`` branch.
   Without this phase, a regression like "new provider added but its
   config was forgotten in the ``needs_event_bus`` tuple" would ship
   silently — the patched lifecycle phase below would still pass.

2. **Session lifecycle smoke.** Builds a real ``Session`` via
   ``create_session()`` and runs one scripted turn. The factory
   functions are monkeypatched to scripted stubs only for *this*
   phase, so no real network call is made. This catches per-config
   regressions that only manifest when the session drives the
   pipeline (e.g. a config field renamed on one provider that some
   downstream stage reads by attribute).

Together: phase 1 verifies registry/injection correctness; phase 2
verifies that the dispatched config flows through the pipeline.
"""

from __future__ import annotations

from dataclasses import fields
from typing import Any

import pytest

from easycat import create_session
from easycat.events import (
    AgentFinal,
    BotStoppedSpeaking,
    EventBus,
    STTFinal,
    TTSAudio,
    TurnEnded,
    TurnStarted,
)
from easycat.stt.cartesia_provider import CartesiaSTTConfig
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.stt.elevenlabs_provider import ElevenLabsSTTConfig
from easycat.stt.factory import _CONFIG_TO_PROVIDER as _STT_CONFIG_TO_PROVIDER
from easycat.stt.factory import _PROVIDER_TO_CONFIG as _STT_REGISTRY
from easycat.stt.factory import create_stt_provider_from_config
from easycat.stt.openai_provider import OpenAISTTConfig
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
from easycat.tts.cartesia_tts import CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig
from easycat.tts.factory import _CONFIG_TO_PROVIDER as _TTS_CONFIG_TO_PROVIDER
from easycat.tts.factory import _PROVIDERS as _TTS_REGISTRY
from easycat.tts.factory import create_tts_provider_from_config
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


def _requires_event_bus(config_cls: type) -> bool:
    """Whether a config's factory branch must inject the session EventBus.

    Computed structurally — a config "needs an event bus" iff it declares
    an ``event_bus`` field — exactly the way ``create_*_provider_from_config``
    now decides. The guard verifies the invariant independently rather than
    mirroring a hand-maintained list that could silently drift from the
    factories.
    """
    return any(f.name == "event_bus" for f in fields(config_cls))


class _UpperAgent:
    async def run(self, text: str) -> str:
        return text.upper()


def _make_config(stt_cfg: Any, tts_cfg: Any, transport: Any) -> Any:
    from easycat import EasyConfig

    return EasyConfig(
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
    """Two-phase wiring check for every (STT, TTS) config pair.

    Phase 1 calls the *real* factory functions (registry dispatch +
    EventBus injection). Phase 2 patches them to scripted stubs and
    drives a turn through the lifecycle to BotStoppedSpeaking. See
    the module docstring for why both phases are needed.
    """
    stt_cfg = _build_stt_config(stt_config_cls)
    tts_cfg = _build_tts_config(tts_config_cls)

    # ── Phase 1: real factory dispatch + EventBus injection ──────────
    # All provider __init__ methods are network-free; OpenAITTS opens
    # an httpx.AsyncClient pool but issues no requests.
    bus = EventBus()
    real_stt = create_stt_provider_from_config(stt_cfg, bus)
    real_tts = create_tts_provider_from_config(tts_cfg, bus)
    try:
        # Registry dispatch returned the right concrete class. Strict
        # `type(...) is X` rather than isinstance to catch subclass drift.
        assert type(real_stt) is _STT_CONFIG_TO_PROVIDER[stt_config_cls], (
            f"STT factory returned {type(real_stt).__name__}, "
            f"expected {_STT_CONFIG_TO_PROVIDER[stt_config_cls].__name__}"
        )
        assert type(real_tts) is _TTS_CONFIG_TO_PROVIDER[tts_config_cls], (
            f"TTS factory returned {type(real_tts).__name__}, "
            f"expected {_TTS_CONFIG_TO_PROVIDER[tts_config_cls].__name__}"
        )

        # EventBus was injected into the provider's stored config for
        # every type that requires it. The factory uses
        # ``dataclasses.replace`` to set ``event_bus`` on a *copy* of
        # the config, then passes that copy to the provider. Reading
        # ``provider._config.event_bus`` is the only way to verify the
        # copy actually carried the bus through to the provider.
        if _requires_event_bus(stt_config_cls):
            assert real_stt._config.event_bus is bus, (
                f"{stt_config_cls.__name__}: EventBus not injected by factory"
            )
        if _requires_event_bus(tts_config_cls):
            assert real_tts._config.event_bus is bus, (
                f"{tts_config_cls.__name__}: EventBus not injected by factory"
            )
    finally:
        # OpenAITTS allocates an httpx.AsyncClient in __init__; close
        # it to avoid a leaked-connection warning. Other providers'
        # __init__ is pure dataclass storage so no cleanup needed.
        close = getattr(real_tts, "close", None)
        if close is not None:
            await close()

    # ── Phase 2: session lifecycle smoke with scripted providers ─────
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
