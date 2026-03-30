from __future__ import annotations

import asyncio
import base64
import json

import pytest
import websockets

from easycat import TwilioConnectionTransport, create_session
from easycat.events import DTMF, PlaybackMarkAck, STTFinal
from easycat.transports.twilio_media import pcm16_to_mulaw

from .harness import (
    EventCollector,
    RecordingTTS,
    ScriptedSTT,
    ScriptedVAD,
    find_free_port,
    make_chunk,
    make_test_config,
    patch_provider_factories,
    twilio_dtmf_message,
    twilio_mark_message,
    twilio_media_message,
    twilio_start_message,
    twilio_stop_message,
)


class BasicAgent:
    async def run(self, text: str) -> str:
        return f"Reply: {text}"


@pytest.mark.asyncio
@pytest.mark.integration_socket
async def test_create_session_twilio_emits_dtmf_and_playback_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stt = ScriptedSTT(["hello twilio"])
    tts = RecordingTTS(chunk_sizes=(4096,))
    vad = ScriptedVAD(["start", "stop"])
    patch_provider_factories(monkeypatch, stt=stt, tts=tts, vad=vad)

    port = find_free_port()
    server_result: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()

    async def handler(ws: object) -> None:
        transport = TwilioConnectionTransport(ws)
        session = create_session(make_test_config(transport=transport, agent=BasicAgent()))
        collector = EventCollector(session.event_bus)
        collector.subscribe(DTMF, PlaybackMarkAck, STTFinal)

        await session.start()
        try:
            dtmf = await collector.wait_for(DTMF, timeout=3.0)
            ack = await collector.wait_for(PlaybackMarkAck, timeout=3.0)
            stt_final = await collector.wait_for(STTFinal, timeout=3.0)
            if not server_result.done():
                server_result.set_result(
                    {
                        "digit": dtmf.digit,
                        "mark_name": ack.mark_name,
                        "transcript": stt_final.text,
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
            await ws.send(twilio_start_message())

            payload = base64.b64encode(pcm16_to_mulaw(make_chunk().data)).decode("ascii")
            await ws.send(twilio_media_message(payload))
            await ws.send(twilio_media_message(payload))
            await ws.send(twilio_dtmf_message("7"))

            outbound_media: dict[str, object] | None = None
            outbound_mark: dict[str, object] | None = None
            while outbound_media is None or outbound_mark is None:
                message = json.loads(await asyncio.wait_for(ws.recv(), timeout=3.0))
                if message["event"] == "media":
                    outbound_media = message
                elif message["event"] == "mark":
                    outbound_mark = message

            assert outbound_media is not None
            assert outbound_mark is not None

            await ws.send(twilio_mark_message(outbound_mark["mark"]["name"]))
            await ws.send(twilio_stop_message())

        result = await asyncio.wait_for(server_result, timeout=4.0)
    finally:
        server.close()
        await server.wait_closed()

    assert result["digit"] == "7"
    assert str(result["mark_name"]).startswith("ec_playback_")
    assert result["transcript"] == "hello twilio"
