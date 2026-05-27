"""Plan 6: Full-stack voice round-trip with noise reduction AND AEC.

Plan 1 proved the baseline works with the voice bot's default stack.
Plan 6 goes further: it turns ON every processing stage that the
framework supports — real RNNoise noise reduction, real LiveKit AEC,
real Silero VAD, real OpenAI Whisper STT, real OpenAI Agents SDK, real
OpenAI TTS — and verifies an end-to-end voice turn still works when
the full pipeline is active.

This is the comprehensive "does the whole stack compose correctly"
test. It catches integration regressions where a stage transform
corrupts the audio in a way downstream stages can't recover from.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from easycat._audio_utils import resample
from easycat.runtime import JournalRecordKind
from tests.e2e._assertions import assert_turn_complete
from tests.e2e._audio import decode_and_asr, detect_clipping, measure_rms

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration_socket,
    pytest.mark.integration_live,
    pytest.mark.provider_openai,
    pytest.mark.surface_agent,
    pytest.mark.surface_stt,
    pytest.mark.surface_transport,
    pytest.mark.surface_tts,
]


def _build_full_stack_session(*, transport, instructions, debug="full"):
    """Build a live session with both noise reduction AND AEC enabled.

    Unlike ``build_live_session`` (which disables NR/AEC for
    environments where the backends aren't installed), this helper
    insists on the full stack. Requires:

    - OPENAI_API_KEY for STT/TTS/agent
    - torch (for Silero VAD)
    - pyrnnoise (for RNNoise)
    - livekit (for AEC)
    """
    from agents import Agent  # type: ignore[import-untyped]

    from easycat import EasyConfig, create_session

    api_key = os.environ["OPENAI_API_KEY"]
    agent = Agent(name="e2e_full_stack", instructions=instructions, model="gpt-4o-mini")
    return create_session(
        EasyConfig(
            openai_api_key=api_key,
            transport=transport,
            agent=agent,
            debug=debug,  # type: ignore[arg-type]
            enable_noise_reduction=True,
            enable_echo_cancellation=True,
        )
    )


async def test_full_stack_nr_and_aec_voice_roundtrip(
    voice_fixtures: dict[str, pathlib.Path],
    ws_server_factory,
) -> None:
    """Voice round-trip with noise reduction and AEC both ACTIVE."""
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY required")
    pytest.importorskip("agents")
    pytest.importorskip("pyrnnoise")
    pytest.importorskip("livekit")
    pytest.importorskip("torch")

    from easycat.transports.websocket import (
        WebSocketConnectionTransport,
        WebSocketTransportConfig,
    )
    from tests.e2e._clients import WSVoiceClient

    async def builder(ws):  # type: ignore[no-untyped-def]
        transport = WebSocketConnectionTransport(ws, WebSocketTransportConfig())
        return _build_full_stack_session(
            transport=transport,
            instructions=(
                "You are a geography bot. Answer each question in one short "
                "sentence, and always include the answer clearly."
            ),
        )

    handle = await ws_server_factory(builder)

    # Real speech, resampled to 48 kHz (browser capture rate).
    speech_16k = voice_fixtures["question"].read_bytes()
    speech_48k = resample(speech_16k, 16000, 48000)

    async with WSVoiceClient(handle.url) as client:
        await client.wait_for_ready(timeout=5.0)
        await client.negotiate_config(sample_rate=48000)

        await client.send_pcm_realtime(speech_48k, sample_rate=48000)
        await client.send_silence(seconds=1.0, sample_rate=48000)
        outbound = await client.drain_until_silent(idle_for_s=1.5, max_wait_s=60.0)

    # ── Outbound audio is real speech ───────────────────────────────
    assert len(outbound) > 0, "no outbound audio"
    rms = measure_rms(outbound)
    assert rms > 0.005, f"outbound RMS too low: {rms}"
    assert not detect_clipping(outbound), "outbound audio is clipping"

    # ── The TTS format announcement arrived ─────────────────────────
    fmt_msgs = [m for m in client.control_messages() if m.get("type") == "audio_format"]
    assert fmt_msgs, "no audio_format message received"
    tts_rate = fmt_msgs[0]["sample_rate"]
    assert tts_rate in (16000, 24000, 48000)

    # ── Reference STT recovers "paris" from the outbound audio ──────
    transcript = await decode_and_asr(outbound, sample_rate=tts_rate)
    assert "paris" in transcript, (
        f"outbound audio did not contain 'paris'; transcript: {transcript!r}"
    )

    # ── Journal captured the full turn ──────────────────────────────
    session = handle.session
    assert session is not None
    journal = session.journal
    assert journal is not None
    events = journal.slice(kind=JournalRecordKind.EVENT)
    names = [e.name for e in events]
    for required in ("turn_started", "stt_final", "agent_final", "turn_ended"):
        assert required in names, f"missing {required!r} in {names}"

    assert_turn_complete(journal)

    # ── STT heard France/capital/Paris ──────────────────────────────
    stt_final_text = next(
        (e.data.get("text", "").lower() for e in events if e.name == "stt_final"),
        "",
    )
    assert any(tok in stt_final_text for tok in ("france", "capital", "paris")), (
        f"stt_final text unexpected: {stt_final_text!r}"
    )

    # ── Bridge framework transitions recorded ──────────────────────
    fw = journal.slice(kind=JournalRecordKind.FRAMEWORK_TRANSITION)
    directions = {(r.data.get("direction") if r.data else None) for r in fw}
    assert "enter" in directions and "exit" in directions, (
        f"expected enter/exit framework records, saw {directions}"
    )


async def test_full_stack_provider_versions_reflect_real_backends(
    voice_fixtures: dict[str, pathlib.Path],
    ws_server_factory,
) -> None:
    """When the full stack is enabled, ``version_info()`` on each
    provider should reflect the REAL backend (not 'passthrough' or
    'noop'). This catches regressions where the factory silently
    falls back to a no-op stub.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY required")
    pytest.importorskip("agents")
    pytest.importorskip("pyrnnoise")
    pytest.importorskip("livekit")
    pytest.importorskip("torch")

    from easycat.transports.websocket import (
        WebSocketConnectionTransport,
        WebSocketTransportConfig,
    )
    from tests.e2e._audio import sine_pcm16
    from tests.e2e._clients import WSVoiceClient

    async def builder(ws):  # type: ignore[no-untyped-def]
        transport = WebSocketConnectionTransport(ws, WebSocketTransportConfig())
        return _build_full_stack_session(
            transport=transport,
            instructions="Say yes.",
        )

    handle = await ws_server_factory(builder)

    # Just poke the pipeline so providers get touched; we only care
    # about version_info, not the transcript content this time.
    tone = sine_pcm16(duration_s=0.3, sample_rate=16000)
    async with WSVoiceClient(handle.url) as client:
        await client.wait_for_ready(timeout=5.0)
        await client.negotiate_config(sample_rate=16000)
        await client.send_pcm_realtime(tone, sample_rate=16000)
        await client.send_silence(seconds=0.5, sample_rate=16000)

    session = handle.session
    assert session is not None

    # Noise reducer is the real RNNoise backend
    nr_info = session.noise_reducer.version_info()
    assert nr_info["provider"] == "rnnoise", f"expected RNNoise, got {nr_info}"

    # Echo canceller is the real LiveKit backend
    aec_info = session.echo_canceller.version_info()
    assert aec_info["provider"] == "livekit", f"expected LiveKit AEC, got {aec_info}"

    # VAD is Silero (torch-backed)
    vad_info = session.vad.version_info()
    assert vad_info["provider"] in ("silero", "ten", "krisp"), f"expected real VAD, got {vad_info}"

    # STT and TTS are OpenAI
    stt_info = session.stt.version_info()
    assert stt_info["provider"] == "openai", f"expected OpenAI STT, got {stt_info}"
    tts_info = session.tts.version_info()
    assert tts_info["provider"] == "openai", f"expected OpenAI TTS, got {tts_info}"
