from __future__ import annotations

import asyncio
import json

import pytest
import websockets

from easycat import WebSocketConnectionTransport, WebSocketTransportConfig, create_session
from easycat.events import AgentDelta, Interruption, STTFinal, TurnStarted

from .harness import (
    EventCollector,
    RecordingTTS,
    ScriptedSTT,
    ScriptedVAD,
    find_free_port,
    make_chunk,
    make_test_config,
    patch_provider_factories,
    wait_for_condition,
)


class TwoSentenceStreamingAgent:
    async def run(self, text: str) -> str:
        return "First sentence. Second sentence."

    async def run_streaming(self, text: str, **kwargs: object):
        del text, kwargs
        yield type(self)._event("First sentence. ")
        yield type(self)._event("Second sentence.")
        yield type(self)._done("First sentence. Second sentence.")

    @staticmethod
    def _event(text: str):
        from easycat.integrations.agents._stream_types import (
            AgentStreamEvent,
            AgentStreamEventType,
        )

        return AgentStreamEvent(type=AgentStreamEventType.TEXT_DELTA, text=text)

    @staticmethod
    def _done(text: str):
        from easycat.integrations.agents._stream_types import (
            AgentStreamEvent,
            AgentStreamEventType,
        )

        return AgentStreamEvent(type=AgentStreamEventType.DONE, text=text)


@pytest.mark.asyncio
@pytest.mark.integration_socket
async def test_create_session_websocket_streaming_barge_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stt = ScriptedSTT(["hello websocket"])
    tts = RecordingTTS(chunk_sizes=(640, 640), chunk_delay_s=0.05)
    vad = ScriptedVAD(["start", "stop", "start"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    server_result: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()

    async def handler(ws: object) -> None:
        transport = WebSocketConnectionTransport(
            ws,
            WebSocketTransportConfig(audio_format=make_chunk().format),
        )
        session = create_session(
            make_test_config(transport=transport, agent=TwoSentenceStreamingAgent())
        )
        collector = EventCollector(session.event_bus)
        collector.subscribe(TurnStarted, STTFinal, AgentDelta, Interruption)

        await session.start()
        try:
            await collector.wait_for(Interruption, timeout=3.0)
            await wait_for_condition(lambda: stt.start_calls >= 2, timeout=3.0)
            if not server_result.done():
                server_result.set_result(
                    {
                        "turn_starts": len(
                            [e for e in collector.events if isinstance(e, TurnStarted)]
                        ),
                        "stt_start_calls": stt.start_calls,
                        "tts_payloads": [payload.text for payload in tts.payloads],
                    }
                )
        except BaseException as exc:
            if not server_result.done():
                server_result.set_exception(exc)
        finally:
            await session.stop()

    server = await websockets.serve(handler, "127.0.0.1", port)
    try:
        async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
            ready = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert ready["type"] == "ready"

            await ws.send(json.dumps({"type": "config", "sample_rate": 16000}))
            await ws.send(make_chunk().data)
            await ws.send(make_chunk().data)

            fmt_msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
            assert fmt_msg == {"type": "audio_format", "sample_rate": 16000}

            first_audio = await asyncio.wait_for(ws.recv(), timeout=3.0)
            assert isinstance(first_audio, bytes)
            assert len(first_audio) == 640

            await ws.send(make_chunk().data)
            await asyncio.sleep(0.1)

        result = await asyncio.wait_for(server_result, timeout=4.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result["turn_starts"] >= 2
    assert result["stt_start_calls"] >= 2
    assert result["tts_payloads"]
    assert str(result["tts_payloads"][0]).startswith("First sentence.")
