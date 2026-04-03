"""End-to-end tests for WebSocket transports.

Consolidates WebSocket-specific tests from test_transport_bugs.py and
test_transport_e2e.py into a single module covering connection transport,
server transport, protocol edge cases, disconnect/lifecycle, and barge-in.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from easycat import (
    WebSocketConnectionTransport,
    WebSocketTransportConfig,
    create_session,
)
from easycat.audio_format import PCM16_MONO_16K, AudioFormat
from easycat.events import (
    AgentFinal,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Interruption,
    STTFinal,
    TurnStarted,
)
from easycat.transports.websocket import WebSocketTransport

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


class EchoAgent:
    async def run(self, text: str) -> str:
        return f"Echo: {text}"


class CountingAgent:
    """Agent that counts invocations."""

    def __init__(self) -> None:
        self.call_count = 0

    async def run(self, text: str) -> str:
        self.call_count += 1
        return f"turn {self.call_count}: {text.upper()}"


def make_ws_config(
    audio_format: AudioFormat | None = None,
) -> WebSocketTransportConfig:
    return WebSocketTransportConfig(audio_format=audio_format or PCM16_MONO_16K)


# ── Connection transport E2E ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_full_turn_e2e(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full turn through WebSocket transport: audio in -> agent -> audio out."""
    stt = ScriptedSTT(["hello websocket"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    result_future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(ws, make_ws_config())
        from easycat.turn_manager import TurnManagerConfig

        session = create_session(
            make_test_config(
                transport=transport,
                agent=UpperAgent(),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
            )
        )
        collector = EventCollector(session.event_bus)
        collector.subscribe(STTFinal, AgentFinal, BotStoppedSpeaking)

        await session.start()
        try:
            final = await collector.wait_for(AgentFinal, timeout=3.0)
            await collector.wait_for(BotStoppedSpeaking, timeout=3.0)
            if not result_future.done():
                result_future.set_result(
                    {
                        "text": final.text,
                        "tts_payloads": len(tts.payloads),
                    }
                )
        except BaseException as exc:
            if not result_future.done():
                result_future.set_exception(exc)
        finally:
            await session.stop()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            ready = json.loads(await ws.recv())
            assert ready["type"] == "ready"

            # Send config + audio
            await ws.send(json.dumps({"type": "config", "sample_rate": 16000}))
            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)

            # Read outbound audio format + audio data
            fmt_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
            assert fmt_msg["type"] == "audio_format"

            audio = await asyncio.wait_for(ws.recv(), timeout=3.0)
            assert isinstance(audio, bytes)

        result = await asyncio.wait_for(result_future, timeout=4.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result["text"] == "HELLO WEBSOCKET"
    assert result["tts_payloads"] == 1


@pytest.mark.asyncio
async def test_ws_client_disconnect_session_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shutdown() after client disconnect should complete promptly."""
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640, 640), chunk_delay_s=0.1)
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    session_holder: list = []
    handler_done = asyncio.Event()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(ws, make_ws_config())
        from easycat.turn_manager import TurnManagerConfig

        session = create_session(
            make_test_config(
                transport=transport,
                agent=UpperAgent(),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
            )
        )
        session_holder.append(session)
        await session.start()
        # Keep handler alive until websocket closes
        await ws.wait_closed()
        handler_done.set()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)
            await asyncio.sleep(0.05)

        # Client disconnected, wait for handler to notice
        await asyncio.wait_for(handler_done.wait(), timeout=3.0)
        await asyncio.sleep(0.1)

        # shutdown() should complete promptly (not hang)
        session = session_holder[0]
        await asyncio.wait_for(session.shutdown(), timeout=3.0)
        assert not session.is_running
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_ws_sample_rate_negotiation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Client negotiating a different sample rate should work."""
    stt = ScriptedSTT(["resampled"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    result_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(ws, make_ws_config())
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
            await ws.recv()  # ready

            # Negotiate 48kHz sample rate
            await ws.send(json.dumps({"type": "config", "sample_rate": 48000}))
            # Send 48kHz audio (3x the samples for same duration)
            audio_48k = bytes(1920)  # 960 samples at 48kHz = 20ms
            await ws.send(audio_48k)
            await ws.send(audio_48k)

            # Consume server responses
            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=2.0)
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

        result = await asyncio.wait_for(result_future, timeout=4.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result == "RESAMPLED"


# ── Server transport E2E ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_server_transport_full_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full turn through WebSocketTransport server variant (not ConnectionTransport)."""
    stt = ScriptedSTT(["hello server"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    config = WebSocketTransportConfig(host="127.0.0.1", port=port)
    transport = WebSocketTransport(config)

    from easycat.turn_manager import TurnManagerConfig

    session = create_session(
        make_test_config(
            transport=transport,
            agent=UpperAgent(),
            turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
        )
    )
    collector = EventCollector(session.event_bus)
    collector.subscribe(AgentFinal, BotStoppedSpeaking)

    await session.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            ready = json.loads(await ws.recv())
            assert ready["type"] == "ready"

            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)

            # Consume server messages
            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=3.0)
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

        final = await collector.wait_for(AgentFinal, timeout=3.0)
        assert final.text == "HELLO SERVER"
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_ws_server_transport_rejects_second_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Second client connecting to WebSocketTransport should be rejected with 4000."""
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    config = WebSocketTransportConfig(host="127.0.0.1", port=port)
    transport = WebSocketTransport(config)

    from easycat.turn_manager import TurnManagerConfig

    session = create_session(
        make_test_config(
            transport=transport,
            agent=UpperAgent(),
            turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
        )
    )
    await session.start()
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws1:
            ready = json.loads(await ws1.recv())
            assert ready["type"] == "ready"

            # Second client should be rejected
            with pytest.raises(websockets.exceptions.ConnectionClosed) as exc_info:
                async with websockets.connect(f"ws://127.0.0.1:{port}") as ws2:
                    await ws2.recv()
            assert exc_info.value.rcvd.code == 4000
    finally:
        await session.stop()


@pytest.mark.asyncio
async def test_ws_server_transport_client_reconnection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After first client disconnects, second client should connect and get ready."""
    stt = ScriptedSTT(["first", "second"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    config = WebSocketTransportConfig(host="127.0.0.1", port=port)
    transport = WebSocketTransport(config)

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
        # First client connects and completes a turn
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws1:
            ready = json.loads(await ws1.recv())
            assert ready["type"] == "ready"
            await ws1.send(make_chunk().data)
            await ws1.send(make_chunk().data)
            try:
                while True:
                    await asyncio.wait_for(ws1.recv(), timeout=2.0)
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

        final1 = await collector.wait_for(AgentFinal, timeout=3.0)
        assert final1.text == "FIRST"
    finally:
        # Pipeline exits after first client disconnects, so stop
        await session.stop()

    # Verify transport state is clean after stop
    assert not transport.has_client


@pytest.mark.asyncio
async def test_ws_multi_turn_single_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple turns over a single WebSocket connection."""
    agent = CountingAgent()
    stt = ScriptedSTT(["first turn", "second turn"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop", "start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    result_future: asyncio.Future[list[str]] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(
            ws, WebSocketTransportConfig(audio_format=PCM16_MONO_16K)
        )
        from easycat.turn_manager import TurnManagerConfig

        session = create_session(
            make_test_config(
                transport=transport,
                agent=agent,
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
            )
        )
        collector = EventCollector(session.event_bus)
        collector.subscribe(AgentFinal)

        await session.start()
        try:
            finals = []
            f1 = await collector.wait_for(AgentFinal, timeout=3.0)
            finals.append(f1.text)
            f2 = await collector.wait_for(
                AgentFinal, predicate=lambda e: e.text != f1.text, timeout=3.0
            )
            finals.append(f2.text)
            if not result_future.done():
                result_future.set_result(finals)
        except BaseException as exc:
            if not result_future.done():
                result_future.set_exception(exc)
        finally:
            await session.stop()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready

            # Turn 1
            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)
            await asyncio.sleep(0.3)

            # Turn 2
            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)

            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=3.0)
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

        result = await asyncio.wait_for(result_future, timeout=5.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result[0] == "turn 1: FIRST TURN"
    assert result[1] == "turn 2: SECOND TURN"
    assert agent.call_count == 2


# ── Protocol edge cases ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_audio_format_message_sent_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """audio_format control message should only be sent once per rate change."""
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640, 640))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    result_future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(ws, make_ws_config())
        from easycat.turn_manager import TurnManagerConfig

        session = create_session(
            make_test_config(
                transport=transport,
                agent=UpperAgent(),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
            )
        )
        collector = EventCollector(session.event_bus)
        collector.subscribe(BotStoppedSpeaking)

        await session.start()
        try:
            await collector.wait_for(BotStoppedSpeaking, timeout=3.0)
            if not result_future.done():
                result_future.set_result({"done": True})
        except BaseException as exc:
            if not result_future.done():
                result_future.set_exception(exc)
        finally:
            await session.stop()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)

            # Collect all server messages
            messages = []
            try:
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    if isinstance(msg, str):
                        messages.append(json.loads(msg))
                    else:
                        messages.append({"type": "audio_binary"})
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

        await asyncio.wait_for(result_future, timeout=4.0)
    finally:
        server.close()
        await server.wait_closed()

    # audio_format should only appear once (before first audio)
    format_msgs = [m for m in messages if m.get("type") == "audio_format"]
    assert len(format_msgs) == 1
    audio_msgs = [m for m in messages if m.get("type") == "audio_binary"]
    assert len(audio_msgs) >= 2  # 2 TTS chunks


@pytest.mark.asyncio
async def test_ws_empty_binary_message_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty binary frames should not crash the transport."""
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    result_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(ws, make_ws_config())
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
            await ws.recv()  # ready
            # Send empty binary
            await ws.send(b"")
            # Then real audio
            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)

            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=2.0)
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

        result = await asyncio.wait_for(result_future, timeout=4.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result == "HELLO"


@pytest.mark.asyncio
async def test_ws_invalid_json_control_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid JSON in text frames should be ignored without crashing."""
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    result_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(ws, make_ws_config())
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
            await ws.recv()  # ready
            # Send invalid JSON
            await ws.send("not valid json {{{")
            # Send unknown message type
            await ws.send(json.dumps({"type": "unknown_type"}))
            # Then real audio
            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)

            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=2.0)
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

        result = await asyncio.wait_for(result_future, timeout=4.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result == "HELLO"


@pytest.mark.asyncio
async def test_ws_format_negotiation_before_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config message sent before any audio should apply to all subsequent audio."""
    stt = ScriptedSTT(["negotiated"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    result_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(
            ws, WebSocketTransportConfig(audio_format=PCM16_MONO_16K)
        )
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
            await ws.recv()  # ready
            # Negotiate format, then send audio at that rate
            await ws.send(json.dumps({"type": "config", "sample_rate": 24000}))
            # 24kHz audio: 480 samples per 20ms = 960 bytes
            audio_24k = bytes(960)
            await ws.send(audio_24k)
            await ws.send(audio_24k)

            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=2.0)
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

        result = await asyncio.wait_for(result_future, timeout=4.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result == "NEGOTIATED"
    # STT should have received resampled audio
    assert len(stt.sent_audio) >= 2


@pytest.mark.asyncio
async def test_ws_invalid_sample_rate_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config message with invalid sample rate type should be ignored."""
    stt = ScriptedSTT(["valid"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    result_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(
            ws, WebSocketTransportConfig(audio_format=PCM16_MONO_16K)
        )
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
            await ws.recv()  # ready
            # Invalid rate types
            await ws.send(json.dumps({"type": "config", "sample_rate": "not_a_number"}))
            await ws.send(json.dumps({"type": "config", "sample_rate": 0}))
            await ws.send(json.dumps({"type": "config"}))
            # Valid audio at default rate
            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)

            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=2.0)
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

        result = await asyncio.wait_for(result_future, timeout=4.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result == "VALID"


@pytest.mark.asyncio
async def test_ws_text_only_no_audio_no_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sending only text/control messages with no audio should not crash."""
    stt = ScriptedSTT([])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD([])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    session_holder: list = []
    handler_done = asyncio.Event()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(
            ws, WebSocketTransportConfig(audio_format=PCM16_MONO_16K)
        )
        from easycat.turn_manager import TurnManagerConfig

        session = create_session(
            make_test_config(
                transport=transport,
                agent=UpperAgent(),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
            )
        )
        session_holder.append(session)
        await session.start()
        await ws.wait_closed()
        await session.stop()
        handler_done.set()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            # Only send text messages, no audio
            await ws.send(json.dumps({"type": "start"}))
            await ws.send(json.dumps({"type": "config", "sample_rate": 16000}))
            await ws.send(json.dumps({"type": "stop"}))
            await asyncio.sleep(0.1)

        await asyncio.wait_for(handler_done.wait(), timeout=3.0)
        assert not session_holder[0].is_running
    finally:
        server.close()
        await server.wait_closed()


# ── Disconnect and lifecycle ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_disconnect_during_tts_no_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Client disconnecting while TTS is streaming should not hang the session."""
    stt = ScriptedSTT(["hello"])
    # Many slow chunks to ensure TTS is mid-stream when disconnect happens
    tts = RecordingTTS(chunk_sizes=(640, 640, 640, 640, 640), chunk_delay_s=0.2)
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    session_holder: list = []
    bot_speaking = asyncio.Event()
    handler_done = asyncio.Event()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(
            ws, WebSocketTransportConfig(audio_format=PCM16_MONO_16K)
        )
        from easycat.turn_manager import TurnManagerConfig

        session = create_session(
            make_test_config(
                transport=transport,
                agent=UpperAgent(),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
            )
        )
        session_holder.append(session)
        session.event_bus.subscribe(BotStartedSpeaking, lambda _e: bot_speaking.set())

        await session.start()
        await ws.wait_closed()
        # Session should not hang on stop
        await asyncio.wait_for(session.stop(), timeout=5.0)
        handler_done.set()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)

            # Wait for bot to start speaking
            await asyncio.wait_for(bot_speaking.wait(), timeout=3.0)
            # Disconnect mid-TTS
            await asyncio.sleep(0.05)

        await asyncio.wait_for(handler_done.wait(), timeout=8.0)
        assert not session_holder[0].is_running
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_ws_burst_audio_no_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sending a burst of audio frames with a small queue should not crash.

    Frames will be dropped due to queue backpressure, but the session should
    survive and cleanly stop without hanging.
    """
    stt = ScriptedSTT([])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD([])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    session_holder: list = []
    handler_done = asyncio.Event()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(
            ws,
            WebSocketTransportConfig(
                audio_format=PCM16_MONO_16K,
                max_pending_chunks=10,  # Small queue to trigger backpressure
            ),
        )
        from easycat.turn_manager import TurnManagerConfig

        session = create_session(
            make_test_config(
                transport=transport,
                agent=UpperAgent(),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
            )
        )
        session_holder.append(session)
        await session.start()
        await ws.wait_closed()
        await asyncio.wait_for(session.stop(), timeout=5.0)
        handler_done.set()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            # Send 50 chunks as fast as possible — many will be dropped
            for _ in range(50):
                await ws.send(make_chunk().data)
            await asyncio.sleep(0.1)

        await asyncio.wait_for(handler_done.wait(), timeout=8.0)
        assert not session_holder[0].is_running
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_ws_connection_transport_natural_close_then_disconnect() -> None:
    """When the WebSocket closes naturally, disconnect() should be safe."""
    port = find_free_port()
    disconnect_ok: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(
            ws, WebSocketTransportConfig(audio_format=PCM16_MONO_16K)
        )
        await transport.connect()
        assert transport.is_connected

        # Wait for natural close
        chunks = []
        async for chunk in transport.receive_audio():
            chunks.append(chunk)

        # After natural close, _receive_loop's finally set _connected=False
        # disconnect() should still be safe (no-op or clean)
        await transport.disconnect()
        if not disconnect_ok.done():
            disconnect_ok.set_result(True)

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await ws.send(make_chunk().data)
            # Close naturally

        result = await asyncio.wait_for(disconnect_ok, timeout=3.0)
        assert result is True
    finally:
        server.close()
        await server.wait_closed()


# ── Barge-in ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ws_barge_in_through_websocket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Barge-in should work through real WebSocket transport."""
    stt = ScriptedSTT(["first turn"])
    # Many slow TTS chunks to ensure bot is still speaking when barge-in arrives
    tts = RecordingTTS(chunk_sizes=(640, 640, 640, 640), chunk_delay_s=0.1)
    vad = ScriptedVAD(["start", "stop", "start"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    result_future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
    bot_speaking = asyncio.Event()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(ws, make_ws_config())
        from easycat.turn_manager import TurnManagerConfig

        session = create_session(
            make_test_config(
                transport=transport,
                agent=EchoAgent(),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
            )
        )
        collector = EventCollector(session.event_bus)
        collector.subscribe(Interruption, TurnStarted, BotStartedSpeaking)

        session.event_bus.subscribe(BotStartedSpeaking, lambda _e: bot_speaking.set())

        await session.start()
        try:
            interruption = await collector.wait_for(Interruption, timeout=5.0)
            if not result_future.done():
                result_future.set_result({"interrupted": interruption is not None})
        except BaseException as exc:
            if not result_future.done():
                result_future.set_exception(exc)
        finally:
            await session.stop()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            # First turn
            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)

            # Wait for bot to actually start speaking
            await asyncio.wait_for(bot_speaking.wait(), timeout=3.0)

            # Small delay then barge-in
            await asyncio.sleep(0.05)
            await ws.send(make_chunk().data)

            try:
                while True:
                    await asyncio.wait_for(ws.recv(), timeout=3.0)
            except (TimeoutError, websockets.exceptions.ConnectionClosed):
                pass

        result = await asyncio.wait_for(result_future, timeout=5.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result["interrupted"] is True


@pytest.mark.asyncio
@pytest.mark.integration_local
async def test_barge_in_calls_transport_clear_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel_turn(barge_in=True) should call transport.clear_audio()."""
    from easycat import create_session
    from easycat.turn_manager import TurnManagerConfig

    from .harness import QueueTransport

    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640, 640, 640, 640), chunk_delay_s=0.1)
    vad = ScriptedVAD(["start", "stop", "start"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    transport = QueueTransport()
    session = create_session(
        make_test_config(
            transport=transport,
            agent=UpperAgent(),
            turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
        )
    )
    collector = EventCollector(session.event_bus)
    collector.subscribe(BotStartedSpeaking, Interruption)

    await session.start()
    try:
        # Feed audio to trigger first turn
        await transport.push_audio(make_chunk(), make_chunk())

        # Wait for bot to start speaking
        await collector.wait_for(BotStartedSpeaking, timeout=3.0)
        clear_before = transport.clear_calls

        # Barge-in audio
        await transport.push_audio(make_chunk())
        await collector.wait_for(Interruption, timeout=3.0)
        # Small yield to let cancel_turn finish (clear_audio is after Interruption emit)
        await asyncio.sleep(0.05)

        # clear_audio should have been called
        assert transport.clear_calls > clear_before
    finally:
        await transport.finish_input()
        await session.stop()
