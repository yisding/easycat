"""End-to-end tests for the Twilio transport layer.

Consolidates all Twilio-specific transport tests covering:
- Full turn E2E with mulaw conversion
- Media handling edge cases (outbound track filtering, invalid base64)
- Mark auto-naming and DTMF interleaving
- Barge-in with clear message
- Disconnect and lifecycle (stop, multiple starts, send before/after stop)
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest
import websockets

from easycat import create_session
from easycat.events import (
    DTMF,
    AgentFinal,
    BotStartedSpeaking,
    Interruption,
    STTFinal,
)
from easycat.transports.twilio_media import (
    TwilioConnectionTransport,
    pcm16_to_mulaw,
)

from .harness import (
    EventCollector,
    RecordingTTS,
    ScriptedSTT,
    ScriptedVAD,
    find_free_port,
    make_chunk,
    make_test_config,
    patch_provider_factories,
)

pytestmark = pytest.mark.integration_socket

FAST_TURN_MS = 1


# ── Helpers ──────────────────────────────────────────────────────────


class UpperAgent:
    async def run(self, text: str) -> str:
        return text.upper()


def twilio_start(stream_sid: str = "MZ123", call_sid: str = "CA456") -> str:
    return json.dumps(
        {
            "event": "start",
            "streamSid": stream_sid,
            "start": {"streamSid": stream_sid, "callSid": call_sid},
        }
    )


def twilio_media(payload_b64: str, stream_sid: str = "MZ123") -> str:
    return json.dumps(
        {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": payload_b64},
        }
    )


def twilio_stop(stream_sid: str = "MZ123") -> str:
    return json.dumps({"event": "stop", "streamSid": stream_sid})


def twilio_mark(name: str, stream_sid: str = "MZ123") -> str:
    return json.dumps(
        {
            "event": "mark",
            "streamSid": stream_sid,
            "mark": {"name": name},
        }
    )


def twilio_dtmf(digit: str, stream_sid: str = "MZ123") -> str:
    return json.dumps({"event": "dtmf", "streamSid": stream_sid, "dtmf": {"digit": digit}})


def make_silence_mulaw(n_samples: int = 160) -> str:
    """Create base64-encoded mulaw silence."""
    pcm = bytes(n_samples * 2)  # 16-bit silence
    mulaw = pcm16_to_mulaw(pcm, source_rate=8000)
    return base64.b64encode(mulaw).decode("ascii")


# ── Full turn E2E ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_twilio_full_turn_e2e(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full turn through Twilio transport with mulaw conversion."""
    stt = ScriptedSTT(["hello twilio"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    result_future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = TwilioConnectionTransport(ws)
        from easycat.turn_manager import TurnManagerConfig

        session = create_session(
            make_test_config(
                transport=transport,
                agent=UpperAgent(),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
            )
        )
        collector = EventCollector(session.event_bus)
        collector.subscribe(AgentFinal, STTFinal)

        await session.start()
        try:
            final = await collector.wait_for(AgentFinal, timeout=3.0)
            if not result_future.done():
                result_future.set_result({"text": final.text})
        except BaseException as exc:
            if not result_future.done():
                result_future.set_exception(exc)
        finally:
            await session.stop()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(twilio_start())
            payload = make_silence_mulaw()
            await ws.send(twilio_media(payload))
            await ws.send(twilio_media(payload))

            # Read outbound media
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                assert msg["event"] == "media"
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

            await ws.send(twilio_stop())

        result = await asyncio.wait_for(result_future, timeout=4.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result["text"] == "HELLO TWILIO"


# ── Media handling edge cases ──────────────────────────────────────


@pytest.mark.asyncio
async def test_twilio_outbound_track_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Media messages with outbound track should be skipped."""
    stt = ScriptedSTT(["inbound only"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    result_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = TwilioConnectionTransport(ws)
        from easycat.turn_manager import TurnManagerConfig

        session = create_session(
            make_test_config(
                transport=transport,
                agent=UpperAgent(),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
            )
        )
        collector = EventCollector(session.event_bus)
        collector.subscribe(AgentFinal)
        await session.start()
        try:
            final = await collector.wait_for(AgentFinal, timeout=3.0)
            if not result_future.done():
                result_future.set_result(final.text)
        except BaseException as exc:
            if not result_future.done():
                result_future.set_exception(exc)
        finally:
            await session.stop()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(twilio_start())
            payload = make_silence_mulaw()

            # Send outbound track media (should be ignored)
            outbound_msg = json.dumps(
                {
                    "event": "media",
                    "streamSid": "MZ123",
                    "media": {
                        "payload": payload,
                        "track": "outbound",
                    },
                }
            )
            await ws.send(outbound_msg)

            # Send inbound track media (should be processed)
            await ws.send(twilio_media(payload))
            await ws.send(twilio_media(payload))

            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=2.0)
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

            try:
                await ws.send(twilio_stop())
            except websockets.exceptions.ConnectionClosed:
                pass  # Session may have already closed the connection

        result = await asyncio.wait_for(result_future, timeout=4.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result == "INBOUND ONLY"


@pytest.mark.asyncio
async def test_twilio_invalid_base64_media_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid base64 payload should be logged and skipped, not crash."""
    stt = ScriptedSTT(["after invalid"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    result_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = TwilioConnectionTransport(ws)
        from easycat.turn_manager import TurnManagerConfig

        session = create_session(
            make_test_config(
                transport=transport,
                agent=UpperAgent(),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
            )
        )
        collector = EventCollector(session.event_bus)
        collector.subscribe(AgentFinal)
        await session.start()
        try:
            final = await collector.wait_for(AgentFinal, timeout=3.0)
            if not result_future.done():
                result_future.set_result(final.text)
        except BaseException as exc:
            if not result_future.done():
                result_future.set_exception(exc)
        finally:
            await session.stop()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(twilio_start())

            # Send invalid base64 payload
            bad_msg = json.dumps(
                {
                    "event": "media",
                    "streamSid": "MZ123",
                    "media": {"payload": "not!valid!base64!!!"},
                }
            )
            await ws.send(bad_msg)

            # Then send valid audio
            payload = make_silence_mulaw()
            await ws.send(twilio_media(payload))
            await ws.send(twilio_media(payload))

            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=2.0)
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

            try:
                await ws.send(twilio_stop())
            except websockets.exceptions.ConnectionClosed:
                pass  # Session may have already closed the connection

        result = await asyncio.wait_for(result_future, timeout=4.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result == "AFTER INVALID"


@pytest.mark.asyncio
async def test_twilio_send_audio_before_start_is_noop() -> None:
    """Sending audio before receiving start message should be silently ignored."""
    port = find_free_port()
    send_result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = TwilioConnectionTransport(ws)
        await transport.connect()
        # No start message received yet, so stream_sid is None
        chunk = make_chunk(640)
        await transport.send_audio(chunk)
        # Should not crash; just silently ignored
        if not send_result.done():
            send_result.set_result(True)
        await transport.disconnect()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}"):
            # Don't send start message
            await asyncio.sleep(0.2)

        result = await asyncio.wait_for(send_result, timeout=2.0)
        assert result is True
    finally:
        server.close()
        await server.wait_closed()


# ── Mark and DTMF ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_twilio_mark_auto_naming() -> None:
    """send_mark without name should auto-generate sequential names."""
    port = find_free_port()
    marks_future: asyncio.Future[list] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = TwilioConnectionTransport(ws)
        await transport.connect()
        await asyncio.sleep(0.1)  # Wait for start to be processed
        names = []
        names.append(await transport.send_mark())
        names.append(await transport.send_mark())
        names.append(await transport.send_mark("custom_mark"))
        names.append(await transport.send_mark())
        if not marks_future.done():
            marks_future.set_result(names)
        await ws.wait_closed()
        await transport.disconnect()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(twilio_start())
            marks = await asyncio.wait_for(marks_future, timeout=3.0)

            # Read mark messages
            received_marks = []
            try:
                for _ in range(4):
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=1.0))
                    received_marks.append(msg["mark"]["name"])
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

    finally:
        server.close()
        await server.wait_closed()

    assert marks[0] == "mark_1"
    assert marks[1] == "mark_2"
    assert marks[2] == "custom_mark"
    assert marks[3] == "mark_3"  # Counter continues past custom


@pytest.mark.asyncio
async def test_twilio_dtmf_during_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DTMF events should be emitted even while audio is being processed."""
    stt = ScriptedSTT(["hello twilio"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    dtmf_digits: list[str] = []
    result_future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = TwilioConnectionTransport(ws)
        from easycat.turn_manager import TurnManagerConfig

        session = create_session(
            make_test_config(
                transport=transport,
                agent=UpperAgent(),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
            )
        )

        def on_dtmf(event: DTMF) -> None:
            dtmf_digits.append(event.digit)

        session.event_bus.subscribe(DTMF, on_dtmf)

        collector = EventCollector(session.event_bus)
        collector.subscribe(AgentFinal)

        await session.start()
        try:
            final = await collector.wait_for(AgentFinal, timeout=3.0)
            # Small delay for DTMF events to be processed
            await asyncio.sleep(0.1)
            if not result_future.done():
                result_future.set_result({"text": final.text, "digits": list(dtmf_digits)})
        except BaseException as exc:
            if not result_future.done():
                result_future.set_exception(exc)
        finally:
            await session.stop()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(twilio_start())
            payload = make_silence_mulaw()

            # Interleave DTMF with audio
            await ws.send(twilio_media(payload))
            await ws.send(twilio_dtmf("1"))
            await ws.send(twilio_media(payload))
            await ws.send(twilio_dtmf("2"))

            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=2.0)
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

            try:
                await ws.send(twilio_stop())
            except websockets.exceptions.ConnectionClosed:
                pass

        result = await asyncio.wait_for(result_future, timeout=4.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result["text"] == "HELLO TWILIO"
    assert result["digits"] == ["1", "2"]


# ── Barge-in ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_twilio_barge_in_sends_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Barge-in through Twilio should send a clear message to discard server-side audio."""
    stt = ScriptedSTT(["initial"])
    tts = RecordingTTS(chunk_sizes=(640, 640, 640, 640), chunk_delay_s=0.1)
    vad = ScriptedVAD(["start", "stop", "start"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    bot_speaking = asyncio.Event()
    result_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = TwilioConnectionTransport(ws)
        from easycat.turn_manager import TurnManagerConfig

        session = create_session(
            make_test_config(
                transport=transport,
                agent=UpperAgent(),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
            )
        )
        session.event_bus.subscribe(BotStartedSpeaking, lambda _e: bot_speaking.set())

        collector = EventCollector(session.event_bus)
        collector.subscribe(Interruption)

        await session.start()
        try:
            interruption = await collector.wait_for(Interruption, timeout=5.0)
            if not result_future.done():
                result_future.set_result(interruption is not None)
        except BaseException as exc:
            if not result_future.done():
                result_future.set_exception(exc)
        finally:
            await session.stop()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        received_clear = False
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(twilio_start())
            payload = make_silence_mulaw()
            await ws.send(twilio_media(payload))
            await ws.send(twilio_media(payload))

            await asyncio.wait_for(bot_speaking.wait(), timeout=3.0)
            await asyncio.sleep(0.05)

            # Barge-in
            await ws.send(twilio_media(payload))

            # Collect messages — look for a clear event
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=3.0)
                    if isinstance(msg, str):
                        parsed = json.loads(msg)
                        if parsed.get("event") == "clear":
                            received_clear = True
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

        result = await asyncio.wait_for(result_future, timeout=5.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result is True
    assert received_clear, "Expected a 'clear' message sent to Twilio on barge-in"


# ── Disconnect and lifecycle ───────────────────────────────────────


@pytest.mark.asyncio
async def test_twilio_stop_before_media_ends_receive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Twilio stop event should end the receive_audio iterator."""
    port = find_free_port()
    receive_ended = asyncio.Event()

    async def handler(ws) -> None:
        transport = TwilioConnectionTransport(ws)
        await transport.connect()
        chunks = []
        async for chunk in transport.receive_audio():
            chunks.append(chunk)
        receive_ended.set()
        await transport.disconnect()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(twilio_start())
            await ws.send(twilio_stop())
            await asyncio.sleep(0.1)

        await asyncio.wait_for(receive_ended.wait(), timeout=2.0)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_twilio_multiple_start_messages() -> None:
    """Transport should handle multiple start messages without crashing.

    A second start message (e.g., call transfer) should update stream_sid.
    """
    port = find_free_port()
    sids_seen: list[str | None] = []
    receive_ended = asyncio.Event()
    ready_for_second_start = asyncio.Event()
    ready_for_stop = asyncio.Event()

    async def handler(ws) -> None:
        transport = TwilioConnectionTransport(ws)
        await transport.connect()

        # Wait for first start to be processed
        await asyncio.sleep(0.1)
        sids_seen.append(transport._stream_sid)
        ready_for_second_start.set()

        # Wait for second start
        await asyncio.sleep(0.2)
        sids_seen.append(transport._stream_sid)
        ready_for_stop.set()

        chunks = []
        async for chunk in transport.receive_audio():
            chunks.append(chunk)
        receive_ended.set()
        await transport.disconnect()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(twilio_start("MZ111", "CA111"))
            await asyncio.wait_for(ready_for_second_start.wait(), timeout=2.0)

            await ws.send(twilio_start("MZ999", "CA999"))
            await asyncio.wait_for(ready_for_stop.wait(), timeout=2.0)

            await ws.send(twilio_stop("MZ999"))
            await asyncio.sleep(0.1)

        await asyncio.wait_for(receive_ended.wait(), timeout=2.0)
    finally:
        server.close()
        await server.wait_closed()

    assert sids_seen[0] == "MZ111"
    assert sids_seen[1] == "MZ999"


@pytest.mark.asyncio
async def test_twilio_send_audio_after_stop_is_noop() -> None:
    """Sending audio after Twilio stop event should be silently ignored."""
    port = find_free_port()
    send_ok: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = TwilioConnectionTransport(ws)
        await transport.connect()
        await asyncio.sleep(0.2)  # Wait for start + stop to be processed

        # After stop, stream_sid is None, so send_audio should be a no-op
        chunk = make_chunk(640)
        await transport.send_audio(chunk)  # Should not crash
        if not send_ok.done():
            send_ok.set_result(True)
        await transport.disconnect()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(twilio_start())
            await ws.send(twilio_stop())
            await asyncio.sleep(0.3)

        result = await asyncio.wait_for(send_ok, timeout=2.0)
        assert result is True
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_twilio_connection_transport_disconnect_cleanup() -> None:
    """disconnect() should clean up stream_sid and enqueue sentinel."""
    port = find_free_port()
    cleanup_ok: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = TwilioConnectionTransport(ws)
        await transport.connect()
        await asyncio.sleep(0.1)  # Wait for start message

        assert transport._stream_sid == "MZ123"
        assert transport._call_sid == "CA456"

        await transport.disconnect()

        if not cleanup_ok.done():
            cleanup_ok.set_result(
                {
                    "stream_sid": transport._stream_sid,
                    "call_sid": transport._call_sid,
                    "connected": transport._connected,
                }
            )

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.send(twilio_start())
            result = await asyncio.wait_for(cleanup_ok, timeout=3.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result["stream_sid"] is None
    assert result["call_sid"] is None
    assert result["connected"] is False
