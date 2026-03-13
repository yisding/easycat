"""WebRTC + FastAPI observability demo.

Runs the standard EasyCat WebRTC signaling/session pipeline and a FastAPI UI
that visualizes live conversation events (ASR, LLM, TTS, barge-in, and tools)
in an Audacity-style multi-track timeline.

Setup:
    export OPENAI_API_KEY="..."
    uv sync --extra openai-agents --extra webrtc --extra fastapi
    uv run python examples/webrtc_observability_server.py

Open:
    http://localhost:8090
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from easycat import (
    AgentDelta,
    AgentFinal,
    AudioIn,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    EasyCatConfig,
    ICEServer,
    Interruption,
    OpenAIAgentsAdapter,
    STTFinal,
    STTPartial,
    ToolCallResult,
    ToolCallStarted,
    TTSAudio,
    WebRTCTransportConfig,
    create_session,
)

_STATIC_DIR = str(Path(__file__).parent / "webrtc_static")
_OBSERVABILITY_HTML = Path(__file__).parent / "webrtc_static" / "webrtc_observability.html"


def _build_ice_servers() -> list[ICEServer]:
    servers: list[ICEServer] = [ICEServer(urls="stun:stun.l.google.com:19302")]

    turn_url = os.getenv("TURN_SERVER_URL")
    if turn_url:
        servers.append(
            ICEServer(
                urls=turn_url,
                username=os.getenv("TURN_USERNAME", ""),
                credential=os.getenv("TURN_CREDENTIAL", ""),
            )
        )

    return servers


def _serialize_event(event: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": type(event).__name__,
        "timestamp": getattr(event, "timestamp", time.monotonic()),
    }

    if isinstance(event, AudioIn):
        payload["track"] = "audio_to_asr"
        payload["bytes"] = len(event.chunk.data)
        payload["duration_ms"] = event.chunk.duration_ms
    elif isinstance(event, TTSAudio):
        payload["track"] = "audio_from_tts"
        payload["bytes"] = len(event.chunk.data)
        payload["duration_ms"] = event.chunk.duration_ms
    elif isinstance(event, STTPartial | STTFinal):
        payload["track"] = "asr_text"
        payload["text"] = event.text
    elif isinstance(event, AgentDelta | AgentFinal):
        payload["track"] = "llm_text"
        payload["text"] = event.text
    elif isinstance(event, ToolCallStarted):
        payload["track"] = "tool_calls"
        payload["tool_name"] = event.tool_name
        payload["call_id"] = event.call_id
    elif isinstance(event, ToolCallResult):
        payload["track"] = "tool_calls"
        payload["call_id"] = event.call_id
        payload["result"] = event.result
    elif isinstance(event, Interruption):
        payload["track"] = "barge_in"
    elif isinstance(event, BotStartedSpeaking | BotStoppedSpeaking):
        payload["track"] = "playback"

    if is_dataclass(event):
        # Keep primitive correlation fields where available.
        data = asdict(event)
        for key in ("session_id", "turn_id"):
            if key in data and data[key] is not None:
                payload[key] = data[key]

    return payload


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required.")

    try:
        from agents import Agent, function_tool  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "OpenAI Agents SDK is required. Install with: uv sync --extra openai-agents"
        ) from exc

    try:
        import uvicorn
        from fastapi import FastAPI
        from fastapi.responses import HTMLResponse, StreamingResponse
    except ImportError as exc:
        raise SystemExit(
            "FastAPI + uvicorn are required. Install with: uv sync --group dev"
        ) from exc

    @function_tool
    def add_numbers(a: float, b: float) -> float:
        """Return a + b."""
        return a + b

    @function_tool
    def multiply_numbers(a: float, b: float) -> float:
        """Return a * b."""
        return a * b

    voice_agent = Agent(
        name="ObservableVoiceAssistant",
        instructions=(
            "You are a helpful voice assistant. Keep responses concise. "
            "When a user asks math questions, use the available math tools."
        ),
        tools=[add_numbers, multiply_numbers],
    )
    adapter = OpenAIAgentsAdapter(voice_agent)

    signaling_host = os.getenv("SIGNALING_HOST", "0.0.0.0")
    signaling_port = int(os.getenv("SIGNALING_PORT", "8080"))
    dashboard_host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    dashboard_port = int(os.getenv("DASHBOARD_PORT", "8090"))

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=WebRTCTransportConfig(
            host=signaling_host,
            port=signaling_port,
            ice_servers=_build_ice_servers(),
            static_dir=_STATIC_DIR,
        ),
        agent=adapter,
        wrap_agent=False,
    )
    session = create_session(config)

    event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=5000)

    def _on_event(event: Any) -> None:
        item = _serialize_event(event)
        try:
            event_queue.put_nowait(item)
        except asyncio.QueueFull:
            logging.warning("Observability queue full; dropping event %s", item.get("type"))

    for event_type in (
        AudioIn,
        STTPartial,
        STTFinal,
        AgentDelta,
        AgentFinal,
        TTSAudio,
        ToolCallStarted,
        ToolCallResult,
        Interruption,
        BotStartedSpeaking,
        BotStoppedSpeaking,
    ):
        session.subscribe_event(event_type, _on_event)

    app = FastAPI(title="EasyCat WebRTC Observability")

    @app.get("/", response_class=HTMLResponse)
    async def observability_page() -> str:
        return _OBSERVABILITY_HTML.read_text(encoding="utf-8")

    @app.get("/events")
    async def stream_events() -> StreamingResponse:
        async def gen() -> Any:
            while True:
                item = await event_queue.get()
                yield f"data: {json.dumps(item)}\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    await session.start()

    uvicorn_cfg = uvicorn.Config(app, host=dashboard_host, port=dashboard_port, log_level="info")
    uvicorn_server = uvicorn.Server(uvicorn_cfg)
    server_task = asyncio.create_task(uvicorn_server.serve())

    print(f"WebRTC signaling:   http://localhost:{signaling_port}/webrtc_client.html")
    print(f"Observability UI:   http://localhost:{dashboard_port}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()

    uvicorn_server.should_exit = True
    await server_task
    await session.stop()


if __name__ == "__main__":
    asyncio.run(main())
