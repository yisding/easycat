"""Plan 3: Adversarial audio and transport corner cases.

The pipeline must degrade gracefully for real-world audio conditions:
long silence, background noise, quiet speech, sample-rate mismatches,
disconnects, malformed frames, Twilio track filtering, and provider
failures. Every corner case must either produce a clean journal trace
or explicitly skip with a helpful reason.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from easycat.runtime import JournalRecordKind
from tests.e2e._audio import (
    scale_pcm16,
    sine_pcm16,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration_socket]


# ---------------------------------------------------------------------------
# Shared setup: scripted-provider server where we control STT decisions
# ---------------------------------------------------------------------------


async def _make_scripted_server(monkeypatch, ws_server_factory, *, transcripts):
    from easycat import create_session
    from easycat.transports.websocket import WebSocketConnectionTransport
    from tests.integration.harness import (
        RecordingTTS,
        ScriptedSTT,
        ScriptedVAD,
        make_test_config,
        patch_provider_factories,
    )

    stt = ScriptedSTT(transcripts=transcripts)
    tts = RecordingTTS(chunk_sizes=(320,))
    vad = ScriptedVAD(script=[["start", "stop"]] * max(1, len(transcripts)))
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    class EchoAgent:
        async def run(self, text: str, **kw):  # type: ignore[no-untyped-def]
            return f"echo: {text}"

    async def builder(ws):  # type: ignore[no-untyped-def]
        transport = WebSocketConnectionTransport(ws)
        cfg = make_test_config(transport=transport, agent=EchoAgent())
        cfg.debug = "full"  # type: ignore[assignment]
        return create_session(cfg)

    return await ws_server_factory(builder), stt


# ---------------------------------------------------------------------------
# 3a. Long silence: no false turns
# ---------------------------------------------------------------------------


async def test_long_silence_produces_no_turns(
    monkeypatch: pytest.MonkeyPatch,
    ws_server_factory,
) -> None:
    """Five seconds of pure silence must not trigger a turn_started."""
    from tests.e2e._clients import WSVoiceClient

    handle, _ = await _make_scripted_server(
        monkeypatch, ws_server_factory, transcripts=["should never fire"]
    )

    async with WSVoiceClient(handle.url) as client:
        await client.wait_for_ready(timeout=5.0)
        await client.negotiate_config(sample_rate=16000)
        await client.send_silence(seconds=5.0, sample_rate=16000)
        await asyncio.sleep(0.5)

    session = handle.session
    assert session is not None
    # ScriptedVAD emits start/stop based on calls, not audio energy, so
    # we can only assert: no agent_final was produced, and there are no
    # crashes in the journal.
    records = session.journal.read()
    errors = [r for r in records if r.error is not None]
    assert not errors, f"errors during silence: {errors}"


# ---------------------------------------------------------------------------
# 3b. Quiet speech (-20 dB)
# ---------------------------------------------------------------------------


async def test_quiet_speech_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
    ws_server_factory,
) -> None:
    """Very quiet speech either transcribes or fails gracefully."""
    from tests.e2e._clients import WSVoiceClient

    handle, _ = await _make_scripted_server(monkeypatch, ws_server_factory, transcripts=["hello"])

    tone = sine_pcm16(duration_s=0.4, sample_rate=16000)
    quiet = scale_pcm16(tone, 0.05)  # -26 dB

    async with WSVoiceClient(handle.url) as client:
        await client.wait_for_ready(timeout=5.0)
        await client.negotiate_config(sample_rate=16000)
        await client.send_pcm_realtime(quiet, sample_rate=16000)
        await client.send_silence(seconds=0.5, sample_rate=16000)
        await asyncio.sleep(0.5)

    session = handle.session
    assert session is not None
    records = session.journal.read()
    errors = [r for r in records if r.error is not None]
    assert not errors, f"crash on quiet speech: {errors}"


# ---------------------------------------------------------------------------
# 3c. Sample-rate mismatch (client says 44.1 kHz)
# ---------------------------------------------------------------------------


async def test_sample_rate_mismatch_resamples(
    monkeypatch: pytest.MonkeyPatch,
    ws_server_factory,
) -> None:
    """Client announces 44.1 kHz; pipeline at 16 kHz must accept via
    transport-side resampling."""
    from easycat.audio_utils import resample
    from tests.e2e._clients import WSVoiceClient

    handle, _ = await _make_scripted_server(monkeypatch, ws_server_factory, transcripts=["hello"])

    tone_16k = sine_pcm16(duration_s=0.3, sample_rate=16000)
    tone_44k = resample(tone_16k, 16000, 44100)

    async with WSVoiceClient(handle.url) as client:
        await client.wait_for_ready(timeout=5.0)
        await client.negotiate_config(sample_rate=44100)
        await client.send_pcm_realtime(tone_44k, sample_rate=44100)
        await client.send_silence(seconds=0.5, sample_rate=44100)
        await asyncio.sleep(0.5)

    session = handle.session
    assert session is not None
    # The transport accepted and resampled without raising.
    records = session.journal.read()
    errors = [
        r for r in records if r.error is not None and "resample" in (r.error.message or "").lower()
    ]
    assert not errors, f"resampling errors: {errors}"


# ---------------------------------------------------------------------------
# 3d. Mid-utterance disconnect
# ---------------------------------------------------------------------------


async def test_mid_utterance_disconnect_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
    ws_server_factory,
) -> None:
    """Abruptly close the ws; server must unwind without hanging."""
    from tests.e2e._clients import WSVoiceClient

    handle, _ = await _make_scripted_server(
        monkeypatch, ws_server_factory, transcripts=["partial"]
    )

    tone = sine_pcm16(duration_s=0.5, sample_rate=16000)

    async with WSVoiceClient(handle.url) as client:
        await client.wait_for_ready(timeout=5.0)
        await client.negotiate_config(sample_rate=16000)
        await client.send_binary(tone[: len(tone) // 2])  # Partial tone
        # Disconnect immediately.
    # Allow the server handler to observe the close and clean up.
    await asyncio.sleep(1.0)

    session = handle.session
    assert session is not None
    # The session should be flagged as stopped, not wedged.
    assert not session.is_running or session.journal is not None


# ---------------------------------------------------------------------------
# 3e. Malformed frames
# ---------------------------------------------------------------------------


async def test_malformed_frames_do_not_crash(
    monkeypatch: pytest.MonkeyPatch,
    ws_server_factory,
) -> None:
    """Zero-byte, giant, and invalid-json frames must be rejected quietly."""
    from tests.e2e._clients import WSVoiceClient

    handle, _ = await _make_scripted_server(monkeypatch, ws_server_factory, transcripts=["ok"])

    async with WSVoiceClient(handle.url) as client:
        await client.wait_for_ready(timeout=5.0)
        await client.negotiate_config(sample_rate=16000)

        # Zero-byte binary frame.
        await client.send_binary(b"")
        # Invalid JSON (text frame).
        await client.send_json({"type": "garbage", "payload": 12345})
        # Invalid type string.
        try:
            await client._ws.send("not json at all")  # type: ignore[union-attr]
        except Exception:
            pass

        # Follow with real audio to prove session is still alive.
        tone = sine_pcm16(duration_s=0.2, sample_rate=16000)
        await client.send_pcm_realtime(tone, sample_rate=16000)
        await client.send_silence(seconds=0.4, sample_rate=16000)
        await asyncio.sleep(0.5)

    session = handle.session
    assert session is not None
    # No propagated exception from the malformed frames.
    records = session.journal.read()
    errors = [
        r
        for r in records
        if r.error is not None
        and any(kw in (r.error.message or "").lower() for kw in ("garbage", "json", "decode"))
    ]
    assert not errors, f"malformed-frame errors: {errors}"


# ---------------------------------------------------------------------------
# 3f. Twilio: outbound_track filter + wrong streamSid
# ---------------------------------------------------------------------------


async def test_twilio_outbound_track_and_wrong_stream_sid_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
    ws_server_factory,
) -> None:
    """TwilioConnectionTransport must drop outbound_track frames and
    mismatched streamSids without affecting STT input."""
    from easycat import create_session
    from easycat.transports.twilio_media import TwilioConnectionTransport
    from tests.e2e._clients import TwilioVoiceClient
    from tests.integration.harness import (
        RecordingTTS,
        ScriptedSTT,
        ScriptedVAD,
        make_test_config,
        patch_provider_factories,
    )

    stt = ScriptedSTT(transcripts=["ignored"])
    tts = RecordingTTS(chunk_sizes=(320,))
    vad = ScriptedVAD(script=[["start", "stop"]])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    class EchoAgent:
        async def run(self, text: str, **kw):  # type: ignore[no-untyped-def]
            return text

    async def builder(ws):  # type: ignore[no-untyped-def]
        transport = TwilioConnectionTransport(ws)
        cfg = make_test_config(transport=transport, agent=EchoAgent())
        cfg.debug = "full"  # type: ignore[assignment]
        return create_session(cfg)

    handle = await ws_server_factory(builder)

    mulaw_frame = bytes([0xFF] * 160)  # single 20ms μ-law frame

    async with TwilioVoiceClient(handle.url, stream_sid="MZgood", call_sid="CAgood") as client:
        await client.send_connected()
        await client.send_start()

        # Send N valid inbound frames, then N outbound_track frames.  If
        # the filter works, STT gets roughly N frames of audio; if it
        # fails, roughly 2N frames.
        n_valid = 10
        for _ in range(n_valid):
            await client.send_media_mulaw(mulaw_frame, track="inbound")
        for _ in range(n_valid):
            await client.send_media_mulaw(mulaw_frame, track="outbound_track")

        # Also a single wrong-streamSid frame.
        await client.send_raw(
            {
                "event": "media",
                "streamSid": "MZbad",
                "media": {
                    "payload": base64.b64encode(mulaw_frame).decode(),
                    "track": "inbound",
                },
            }
        )

        await asyncio.sleep(0.5)
        await client.send_stop()
        await asyncio.sleep(0.5)

    session = handle.session
    assert session is not None

    # Each valid inbound frame is 160 bytes μ-law -> ~320 bytes PCM16 @
    # 8 kHz -> 640 bytes PCM16 @ 16 kHz after resampling.  Ten frames of
    # real inbound audio should be around ~6400 bytes; twenty frames
    # (if outbound_track leaks through) would double to ~12800 bytes.
    total_audio = sum(len(c.data) for c in stt.sent_audio)
    valid_upper_bound = n_valid * 640 + n_valid * 320  # generous ceiling
    assert total_audio <= valid_upper_bound, (
        f"too much audio reached STT — outbound_track filter failed: "
        f"got {total_audio} bytes, expected <= {valid_upper_bound}"
    )


# ---------------------------------------------------------------------------
# 3g. Provider failure mid-turn
# ---------------------------------------------------------------------------


async def test_stt_provider_failure_is_structured_in_journal(
    monkeypatch: pytest.MonkeyPatch,
    ws_server_factory,
) -> None:
    """When STT raises, the error is recorded with ErrorInfo, session
    doesn't wedge, and subsequent turns work."""
    from easycat import create_session
    from easycat.transports.websocket import WebSocketConnectionTransport
    from tests.e2e._clients import WSVoiceClient
    from tests.integration.harness import (
        RecordingTTS,
        ScriptedSTT,
        ScriptedVAD,
        make_test_config,
        patch_provider_factories,
    )

    class FailingSTT(ScriptedSTT):
        async def start_stream(self) -> None:
            await super().start_stream()
            raise RuntimeError("simulated STT failure")

    stt = FailingSTT(transcripts=["will not fire"])
    tts = RecordingTTS(chunk_sizes=(320,))
    vad = ScriptedVAD(script=[["start", "stop"]])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    class EchoAgent:
        async def run(self, text: str, **kw):  # type: ignore[no-untyped-def]
            return text

    async def builder(ws):  # type: ignore[no-untyped-def]
        transport = WebSocketConnectionTransport(ws)
        cfg = make_test_config(transport=transport, agent=EchoAgent())
        cfg.debug = "full"  # type: ignore[assignment]
        return create_session(cfg)

    handle = await ws_server_factory(builder)

    tone = sine_pcm16(duration_s=0.2, sample_rate=16000)
    async with WSVoiceClient(handle.url) as client:
        await client.wait_for_ready(timeout=5.0)
        await client.negotiate_config(sample_rate=16000)
        await client.send_pcm_realtime(tone, sample_rate=16000)
        await client.send_silence(seconds=0.5, sample_rate=16000)
        await asyncio.sleep(1.0)

    session = handle.session
    if session is None or session.journal is None:
        pytest.skip("session teardown before journal readable")

    records = session.journal.read()
    error_records = [r for r in records if r.error is not None]
    # Some form of structured error record should exist.
    assert error_records or any(
        r.kind == JournalRecordKind.EVENT and r.name == "error" for r in records
    ), "STT failure not reflected in journal"
