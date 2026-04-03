"""End-to-end tests for session lifecycle: stop, shutdown, hang prevention, WebRTC edge cases.

Consolidated from test_stop_hang.py and test_transport_e2e.py.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json

import pytest
import websockets

from easycat import (
    WebSocketConnectionTransport,
    WebSocketTransportConfig,
    create_session,
)
from easycat.audio_format import PCM16_MONO_16K

from .harness import (
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

_HAS_AIOHTTP = importlib.util.find_spec("aiohttp") is not None
_HAS_AIORTC = importlib.util.find_spec("aiortc") is not None
_HAS_WEBRTC_DEPS = _HAS_AIORTC and _HAS_AIOHTTP


# ── Agent helpers ──────────────────────────────────────────────────


class UpperAgent:
    async def run(self, text: str) -> str:
        return text.upper()


class SlowAgent:
    """Agent that takes a configurable time to respond."""

    def __init__(self, delay: float = 2.0) -> None:
        self._delay = delay

    async def run(self, text: str) -> str:
        await asyncio.sleep(self._delay)
        return text.upper()


# ── Session stop after disconnect ──────────────────────────────────


@pytest.mark.asyncio
async def test_is_running_becomes_false_after_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After transport disconnect, is_running should become False automatically."""
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    from easycat.turn_manager import TurnManagerConfig

    port = find_free_port()
    result_future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(
            ws, WebSocketTransportConfig(audio_format=PCM16_MONO_16K)
        )
        session = create_session(
            make_test_config(
                transport=transport,
                agent=UpperAgent(),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=1),
            )
        )
        await session.start()
        assert session.is_running

        # Wait for client disconnect
        await ws.wait_closed()
        # Give pipeline task time to notice the sentinel
        await asyncio.sleep(0.3)

        # is_running should be False now (pipeline exited)
        if not result_future.done():
            result_future.set_result(session.is_running)
        await session.stop()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)
            await asyncio.sleep(0.2)

        is_running = await asyncio.wait_for(result_future, timeout=5.0)
        assert not is_running, "is_running should be False after transport disconnect"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_poll_is_running_then_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common pattern 'while is_running ... finally stop()' should work."""
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    from easycat.turn_manager import TurnManagerConfig

    port = find_free_port()
    session_stopped = asyncio.Event()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(
            ws, WebSocketTransportConfig(audio_format=PCM16_MONO_16K)
        )
        session = create_session(
            make_test_config(
                transport=transport,
                agent=UpperAgent(),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=1),
            )
        )
        await session.start()
        try:
            while session.is_running:
                await asyncio.sleep(0.05)
        finally:
            await session.stop()
            session_stopped.set()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)
            await asyncio.sleep(0.1)

        # This should complete — not hang
        await asyncio.wait_for(session_stopped.wait(), timeout=5.0)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_stop_completes_after_slow_agent_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """stop() should complete promptly even if a slow agent was interrupted."""
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    from easycat.turn_manager import TurnManagerConfig

    port = find_free_port()
    stop_result: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    handler_done = asyncio.Event()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(
            ws, WebSocketTransportConfig(audio_format=PCM16_MONO_16K)
        )
        session = create_session(
            make_test_config(
                transport=transport,
                agent=SlowAgent(),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=1),
            )
        )
        await session.start()
        await ws.wait_closed()
        try:
            await asyncio.wait_for(session.stop(), timeout=5.0)
            stop_result.set_result("ok")
        except TimeoutError:
            stop_result.set_result("hung")
            await session.shutdown()
        handler_done.set()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            ready = json.loads(await ws.recv())
            assert ready["type"] == "ready"
            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)
            # Disconnect while agent is still processing
            await asyncio.sleep(0.1)

        await asyncio.wait_for(handler_done.wait(), timeout=10.0)
        assert stop_result.result() == "ok"
    finally:
        server.close()
        await server.wait_closed()


# ── Session double-stop and shutdown ───────────────────────────────


@pytest.mark.asyncio
async def test_session_double_stop_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling stop() twice should not raise or hang."""
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    stop_results: list[str] = []
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
        await session.start()
        await ws.wait_closed()

        await asyncio.wait_for(session.stop(), timeout=3.0)
        stop_results.append("first_stop_ok")

        await asyncio.wait_for(session.stop(), timeout=3.0)
        stop_results.append("second_stop_ok")

        handler_done.set()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()  # ready
            await ws.send(make_chunk().data)
            await asyncio.sleep(0.1)

        await asyncio.wait_for(handler_done.wait(), timeout=5.0)
    finally:
        server.close()
        await server.wait_closed()

    assert stop_results == ["first_stop_ok", "second_stop_ok"]


@pytest.mark.asyncio
async def test_session_stop_then_shutdown_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling stop() then shutdown() should not raise or hang."""
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    results: list[str] = []
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
        await session.start()
        await ws.wait_closed()

        await asyncio.wait_for(session.stop(), timeout=3.0)
        results.append("stop_ok")

        await asyncio.wait_for(session.shutdown(), timeout=3.0)
        results.append("shutdown_ok")

        handler_done.set()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()
            await ws.send(make_chunk().data)
            await asyncio.sleep(0.1)

        await asyncio.wait_for(handler_done.wait(), timeout=5.0)
    finally:
        server.close()
        await server.wait_closed()

    assert results == ["stop_ok", "shutdown_ok"]


@pytest.mark.asyncio
async def test_session_shutdown_without_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling shutdown() directly (skipping stop()) should not hang."""
    stt = ScriptedSTT(["hello"])
    tts = RecordingTTS(chunk_sizes=(640, 640, 640), chunk_delay_s=0.1)
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    handler_done = asyncio.Event()

    async def handler(ws) -> None:
        transport = WebSocketConnectionTransport(
            ws, WebSocketTransportConfig(audio_format=PCM16_MONO_16K)
        )
        from easycat.turn_manager import TurnManagerConfig

        session = create_session(
            make_test_config(
                transport=transport,
                agent=SlowAgent(delay=0.3),
                turn_taking=TurnManagerConfig(end_of_turn_silence_ms=FAST_TURN_MS),
            )
        )
        await session.start()
        await ws.wait_closed()
        # Force-close everything
        await asyncio.wait_for(session.shutdown(), timeout=5.0)
        assert not session.is_running
        handler_done.set()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            await ws.recv()
            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)
            await asyncio.sleep(0.1)

        await asyncio.wait_for(handler_done.wait(), timeout=8.0)
    finally:
        server.close()
        await server.wait_closed()


# ── WebRTC edge cases ──────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
async def test_webrtc_offer_with_empty_sdp_rejected() -> None:
    """POST /offer with empty SDP string should return 400."""
    import aiohttp

    from easycat.transports.webrtc import WebRTCTransport, WebRTCTransportConfig

    port = find_free_port()
    config = WebRTCTransportConfig(host="127.0.0.1", port=port)
    transport = WebRTCTransport(config)
    await transport.connect()

    try:
        async with aiohttp.ClientSession() as session:
            # Empty SDP string
            async with session.post(
                f"http://127.0.0.1:{port}/offer",
                json={"sdp": "", "type": "offer"},
            ) as resp:
                assert resp.status == 400
                data = await resp.json()
                assert "error" in data

            # Whitespace-only SDP
            async with session.post(
                f"http://127.0.0.1:{port}/offer",
                json={"sdp": "   ", "type": "offer"},
            ) as resp:
                assert resp.status == 400
    finally:
        await transport.disconnect()


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
async def test_webrtc_offer_with_wrong_type_rejected() -> None:
    """POST /offer with type != 'offer' should return 400."""
    import aiohttp

    from easycat.transports.webrtc import WebRTCTransport, WebRTCTransportConfig

    port = find_free_port()
    config = WebRTCTransportConfig(host="127.0.0.1", port=port)
    transport = WebRTCTransport(config)
    await transport.connect()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/offer",
                json={"sdp": "v=0\r\n...", "type": "answer"},
            ) as resp:
                assert resp.status == 400
    finally:
        await transport.disconnect()


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
async def test_webrtc_offer_with_array_body_rejected() -> None:
    """POST /offer with a JSON array body should return 400."""
    import aiohttp

    from easycat.transports.webrtc import WebRTCTransport, WebRTCTransportConfig

    port = find_free_port()
    config = WebRTCTransportConfig(host="127.0.0.1", port=port)
    transport = WebRTCTransport(config)
    await transport.connect()

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"http://127.0.0.1:{port}/offer",
                json=["not", "a", "dict"],
            ) as resp:
                assert resp.status == 400
    finally:
        await transport.disconnect()


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
async def test_webrtc_disconnect_without_peer_connected() -> None:
    """disconnect() without a peer connection should clean up the HTTP server."""
    from easycat.transports.webrtc import WebRTCTransport, WebRTCTransportConfig

    port = find_free_port()
    config = WebRTCTransportConfig(host="127.0.0.1", port=port)
    transport = WebRTCTransport(config)

    await transport.connect()
    assert transport.is_connected

    await transport.disconnect()
    assert not transport.is_connected

    # HTTP server should be down
    import aiohttp

    async with aiohttp.ClientSession() as session:
        with pytest.raises(aiohttp.ClientError):
            await session.get(f"http://127.0.0.1:{port}/health")


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
async def test_webrtc_send_audio_without_peer_is_noop() -> None:
    """send_audio without a connected peer should be silently ignored."""
    from easycat.transports.webrtc import WebRTCTransport, WebRTCTransportConfig

    port = find_free_port()
    config = WebRTCTransportConfig(host="127.0.0.1", port=port)
    transport = WebRTCTransport(config)
    await transport.connect()

    try:
        # No peer connected, send_audio should be a no-op
        chunk = make_chunk()
        await transport.send_audio(chunk)  # Should not raise
    finally:
        await transport.disconnect()


@pytest.mark.asyncio
@pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
async def test_webrtc_clear_audio_without_peer() -> None:
    """clear_audio without a peer should not raise."""
    from easycat.transports.webrtc import WebRTCTransport, WebRTCTransportConfig

    port = find_free_port()
    config = WebRTCTransportConfig(host="127.0.0.1", port=port)
    transport = WebRTCTransport(config)
    await transport.connect()

    try:
        await transport.clear_audio()  # Should not raise
    finally:
        await transport.disconnect()
