"""WebRTC + FastAPI observability demo.

Runs the standard EasyCat WebRTC signaling/session pipeline and a FastAPI UI
that visualizes live conversation events (ASR, LLM, TTS, barge-in, and tools)
in an Audacity-style multi-track timeline.

Setup:
    export OPENAI_API_KEY="..."
    # telephony extra includes FastAPI/uvicorn
    uv sync --extra openai-agents --extra webrtc --extra telephony
    uv run python examples/webrtc_observability_server.py

Open:
    http://localhost:8090
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from easycat import (
    ALL_EVENTS,
    AgentDelta,
    AgentFinal,
    AudioIn,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    EasyCatConfig,
    Error,
    ICEServer,
    Interruption,
    ReconnectAttempt,
    ReconnectFailure,
    ReconnectSuccess,
    STTFinal,
    STTPartial,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
    TTSAudio,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
    WebRTCTransportConfig,
    attach_runtime_feedback,
    build_openai_agents_adapter,
    create_session,
    default_event_logging,
    require_env,
    wait_for_shutdown_signal,
)

logger = logging.getLogger(__name__)

_STATIC_DIR = str(Path(__file__).parent / "webrtc_static")
_OBSERVABILITY_HTML = Path(__file__).parent / "webrtc_static" / "webrtc_observability.html"


def _build_ice_servers() -> list[ICEServer]:
    """Build ICE server list from environment variables."""
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
    """Serialize an EasyCat event into a JSON-friendly dict for SSE clients."""
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
    elif isinstance(event, ToolCallDelta):
        payload["track"] = "tool_calls"
        payload["call_id"] = event.call_id
        payload["text"] = event.delta
    elif isinstance(event, ToolCallResult):
        payload["track"] = "tool_calls"
        payload["call_id"] = event.call_id
        payload["result"] = event.result
    elif isinstance(event, Interruption):
        payload["track"] = "barge_in"
    elif isinstance(event, BotStartedSpeaking | BotStoppedSpeaking):
        payload["track"] = "playback"
    elif isinstance(event, TurnStarted | TurnEnded):
        payload["track"] = "turn_lifecycle"
    elif isinstance(event, VADStartSpeaking | VADStopSpeaking):
        payload["track"] = "vad"
    elif isinstance(event, Error):
        payload["track"] = "errors"
        payload["message"] = str(event.exception)
        payload["context"] = event.context
    elif isinstance(event, ReconnectAttempt | ReconnectSuccess | ReconnectFailure):
        payload["track"] = "reconnect"
        payload["provider"] = event.provider
        if isinstance(event, ReconnectAttempt):
            payload["attempt"] = event.attempt
        elif isinstance(event, ReconnectFailure):
            payload["error"] = event.error

    for key in ("session_id", "turn_id"):
        val = getattr(event, key, None)
        if val is not None:
            payload[key] = val

    return payload


class _Broadcaster:
    """Fan-out event broadcaster for SSE clients.

    Each connected SSE client gets its own asyncio.Queue.  Events are
    broadcast to all connected clients.  Disconnected clients are
    automatically cleaned up.
    """

    def __init__(self, maxsize: int = 2000) -> None:
        self._clients: dict[int, asyncio.Queue[dict[str, Any] | None]] = {}
        self._next_id = 0
        self._maxsize = maxsize

    def connect(self) -> tuple[int, asyncio.Queue[dict[str, Any] | None]]:
        """Register a new client.  Returns (client_id, queue)."""
        client_id = self._next_id
        self._next_id += 1
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=self._maxsize)
        self._clients[client_id] = q
        logger.info("SSE client %d connected (%d total)", client_id, len(self._clients))
        return client_id, q

    def disconnect(self, client_id: int) -> None:
        """Remove a client by id."""
        self._clients.pop(client_id, None)
        logger.info("SSE client %d disconnected (%d remaining)", client_id, len(self._clients))

    def broadcast(self, item: dict[str, Any]) -> None:
        """Send an item to all connected clients, dropping on overflow."""
        for client_id, q in list(self._clients.items()):
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                logger.warning(
                    "SSE client %d queue full; dropping event %s",
                    client_id,
                    item.get("type"),
                )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    api_key = require_env("OPENAI_API_KEY")

    try:
        from agents import function_tool  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "OpenAI Agents SDK is required. Install with: uv sync --extra openai-agents"
        ) from exc

    @function_tool
    def add_numbers(a: float, b: float) -> float:
        """Return a + b."""
        return a + b

    @function_tool
    def multiply_numbers(a: float, b: float) -> float:
        """Return a * b."""
        return a * b

    adapter = build_openai_agents_adapter(
        instructions=(
            "You are a helpful voice assistant. Keep responses concise. "
            "When a user asks math questions, use the available math tools."
        ),
        tools=[add_numbers, multiply_numbers],
    )

    try:
        import uvicorn
        from fastapi import FastAPI, Request
        from fastapi.responses import HTMLResponse, StreamingResponse
    except ImportError as exc:
        raise SystemExit(
            "FastAPI + uvicorn are required. Install with: uv sync --extra telephony"
        ) from exc

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
        event_logging=default_event_logging(),
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    broadcaster = _Broadcaster()

    def _on_event(event: Any) -> None:
        broadcaster.broadcast(_serialize_event(event))

    session.subscribe_events(ALL_EVENTS, _on_event)

    app = FastAPI(title="EasyCat WebRTC Observability")

    signaling_url = f"http://localhost:{signaling_port}"

    @app.get("/", response_class=HTMLResponse)
    async def observability_page() -> str:
        html = _OBSERVABILITY_HTML.read_text(encoding="utf-8")
        return html.replace("{{SIGNALING_URL}}", signaling_url)

    @app.get("/events")
    async def stream_events(request: Request) -> StreamingResponse:
        client_id, queue = broadcaster.connect()

        async def gen() -> Any:
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        # Send SSE keepalive comment to detect broken connections.
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        break
                    yield f"data: {json.dumps(item)}\n\n"
            finally:
                broadcaster.disconnect(client_id)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    await session.start()

    uvicorn_cfg = uvicorn.Config(app, host=dashboard_host, port=dashboard_port, log_level="info")
    uvicorn_server = uvicorn.Server(uvicorn_cfg)
    # Prevent uvicorn from overriding our signal handlers so
    # wait_for_shutdown_signal() can trigger a clean EasyCat shutdown.
    uvicorn_server.install_signal_handlers = lambda: None  # type: ignore[assignment]
    server_task = asyncio.create_task(uvicorn_server.serve())

    print(f"WebRTC signaling:   http://localhost:{signaling_port}/webrtc_client.html")
    print(f"Observability UI:   http://localhost:{dashboard_port}")

    await wait_for_shutdown_signal(session)

    uvicorn_server.should_exit = True
    await server_task


if __name__ == "__main__":
    asyncio.run(main())
