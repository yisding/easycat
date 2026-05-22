"""Plan 1: Baseline Voice Round-Trip over WebSocket.

One clean voice turn through the complete real stack:
- Real WebSocket transport on localhost
- Real OpenAI Whisper STT
- Real OpenAI Agents SDK agent
- Real OpenAI TTS
- Debug=full so the journal captures every stage

The client streams "What is the capital of France?" at 48 kHz (simulating
a browser that captures at 48 kHz and announces it via the `config`
control message), collects the outbound PCM audio from the server, and
re-transcribes it via Whisper. "Paris" must appear in the re-transcribed
outbound audio. The journal is checked for the full turn lifecycle and
for real `openai_agents` framework transition records.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

import pytest

from easycat.audio_utils import resample
from easycat.debug.bundle import RunBundle
from easycat.runtime import JournalRecordKind
from tests.e2e._assertions import assert_turn_complete
from tests.e2e._audio import decode_and_asr, detect_clipping, measure_rms

pytestmark = [pytest.mark.asyncio, pytest.mark.integration_socket]


@pytest.mark.integration_live
@pytest.mark.provider_openai
@pytest.mark.surface_agent
@pytest.mark.surface_stt
@pytest.mark.surface_transport
@pytest.mark.surface_tts
async def test_baseline_voice_roundtrip_websocket(
    voice_fixtures: dict[str, pathlib.Path],
    ws_server_factory,
) -> None:
    """Full-duplex voice turn over WebSocket with real STT/agent/TTS."""
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY required")
    pytest.importorskip("agents")

    from easycat.transports.websocket import (
        WebSocketConnectionTransport,
        WebSocketTransportConfig,
    )
    from tests.e2e._clients import WSVoiceClient
    from tests.e2e.conftest import build_live_session

    async def builder(ws):  # type: ignore[no-untyped-def]
        transport = WebSocketConnectionTransport(ws, WebSocketTransportConfig())
        return build_live_session(transport=transport)

    handle = await ws_server_factory(builder)

    # 1. Load the real speech fixture and resample to 48 kHz.
    speech_16k = voice_fixtures["question"].read_bytes()
    speech_48k = resample(speech_16k, 16000, 48000)
    assert len(speech_48k) > 0

    # 2. Connect, handshake, stream, collect.
    async with WSVoiceClient(handle.url) as client:
        await client.wait_for_ready(timeout=5.0)
        await client.negotiate_config(sample_rate=48000)

        await client.send_pcm_realtime(speech_48k, sample_rate=48000)
        await client.send_silence(seconds=1.0, sample_rate=48000)

        # Wait for the full TTS response by draining until bytes stop
        # arriving for 1.5 s (end of agent stream + TTS flush).
        outbound = await client.drain_until_silent(idle_for_s=1.5, max_wait_s=60.0)

    # 3. Outbound audio must be real signal, not silence, not clipped.
    assert len(outbound) > 0, "no outbound audio"
    rms = measure_rms(outbound)
    assert rms > 0.005, f"outbound RMS too low: {rms}"
    assert not detect_clipping(outbound), "outbound audio is clipping"

    # 4. audio_format control message announced the TTS sample rate.
    fmt_msgs = [m for m in client.control_messages() if m.get("type") == "audio_format"]
    assert fmt_msgs, "no audio_format message received"
    assert fmt_msgs[0]["sample_rate"] in (16000, 24000, 48000)
    tts_rate = fmt_msgs[0]["sample_rate"]

    # 5. Re-transcribe the outbound audio: must contain "paris".
    transcript = await decode_and_asr(outbound, sample_rate=tts_rate)
    assert "paris" in transcript, (
        f"outbound audio did not contain 'paris'; transcript: {transcript!r}"
    )

    # 6. Journal has the full turn lifecycle plus bridge framework records.
    session = handle.session
    assert session is not None
    journal = session.journal
    assert journal is not None

    pre_stop_records = journal.read()
    events = journal.slice(kind=JournalRecordKind.EVENT)
    names = [e.name for e in events]
    for required in ("turn_started", "stt_final", "agent_final", "turn_ended"):
        assert required in names, f"missing {required!r} in {names}"

    assert_turn_complete(journal)

    # STT transcribed France/capital — at least one should match.
    stt_final_text = next(
        (e.data.get("text", "").lower() for e in events if e.name == "stt_final"),
        "",
    )
    assert any(tok in stt_final_text for tok in ("france", "capital", "paris")), (
        f"stt_final text did not match expected: {stt_final_text!r}"
    )

    # Bridge framework transitions from the adapted OpenAI Agents bridge:
    # we expect both enter and exit records at unit_kind=AGENT.
    fw = journal.slice(kind=JournalRecordKind.FRAMEWORK_TRANSITION)
    directions = {(r.data.get("direction") if r.data else None) for r in fw}
    assert "enter" in directions and "exit" in directions, (
        f"expected enter/exit framework records, saw directions={directions}"
    )

    # 7. A clean stop must preserve journal visibility and bundle export.
    await session.stop()

    post_stop_journal = session.journal
    assert post_stop_journal is not None
    post_stop_records = post_stop_journal.read()
    assert len(post_stop_records) >= len(pre_stop_records)

    post_stop_events = post_stop_journal.slice(kind=JournalRecordKind.EVENT)
    post_stop_stt_final_text = next(
        (e.data.get("text", "").lower() for e in post_stop_events if e.name == "stt_final"),
        "",
    )
    assert post_stop_stt_final_text == stt_final_text

    # 8. Export bundle after stop and verify provider versions are real.
    with tempfile.TemporaryDirectory() as td:
        bundle_path = pathlib.Path(td) / "baseline.zip"
        session.export_debug_bundle(str(bundle_path))
        bundle = RunBundle.load(bundle_path)
        versions = bundle.manifest.provider_versions
        assert versions, "manifest has no provider versions"
        # Each role's version info dict must at least name the provider.
        for role in ("stt", "tts"):
            assert role in versions, f"no {role} version info in manifest"
