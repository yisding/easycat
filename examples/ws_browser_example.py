"""Web-based voice chat with an EasyCat agent.

Serves a web page and runs an EasyCat voice session over WebSocket.
The browser captures microphone audio via getUserMedia (WebRTC API),
sends PCM16 frames over WebSocket, and plays back the agent's audio.

The browser may capture at a higher sample rate (typically 48 kHz) than
the 16 kHz that EasyCat's pipeline expects.  A thin resampling transport
wrapper converts inbound audio to 16 kHz.  Outbound TTS audio is passed
through at its native rate (e.g. 24 kHz) and the server sends an
``audio_format`` control message so the client knows the playback rate.

Setup:
    export OPENAI_API_KEY="..."
    uv sync --extra openai-agents
    uv run python examples/ws_browser_example.py

Then open http://localhost:8080/ws_browser_client.html in your browser.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import threading
from collections.abc import AsyncIterator
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from easycat import (
    PCM16_MONO_16K,
    AudioChunk,
    EasyCatConfig,
    WebSocketTransportConfig,
    attach_runtime_feedback,
    build_openai_agents_adapter,
    create_session,
    default_event_logging,
    require_env,
    wait_for_shutdown_signal,
)
from easycat.audio_utils import resample_chunk

logger = logging.getLogger(__name__)

HTTP_PORT = 8080
WS_PORT = 8765

_STATIC_DIR = str(Path(__file__).parent)

# ── Resampling transport wrapper ─────────────────────────────────


class _ResamplingTransport:
    """Wraps a Transport, resamples inbound audio and forwards TTS at native rate.

    Inbound:  client rate (e.g. 48 kHz) → 16 kHz for the EasyCat pipeline.
    Outbound: passed through at the TTS provider's native rate (e.g. 24 kHz).
              An ``audio_format`` JSON control message is sent to the client
              whenever the outbound sample rate changes so it can create
              playback buffers at the correct rate.
    """

    _INTERNAL_RATE = PCM16_MONO_16K.sample_rate  # 16 000

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self._outbound_rate: int | None = None

    async def connect(self) -> None:
        await self._inner.connect()  # type: ignore[attr-defined]

    async def disconnect(self) -> None:
        await self._inner.disconnect()  # type: ignore[attr-defined]

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        async for chunk in self._inner.receive_audio():  # type: ignore[attr-defined]
            if chunk.format.sample_rate != self._INTERNAL_RATE:
                logger.debug(
                    "Resampling inbound audio %d → %d Hz",
                    chunk.format.sample_rate,
                    self._INTERNAL_RATE,
                )
                chunk = resample_chunk(chunk, self._INTERNAL_RATE)
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> None:
        rate = chunk.format.sample_rate
        # Notify the client when the outbound sample rate changes so it
        # can create AudioBuffers at the right rate.
        if rate != self._outbound_rate:
            ws = getattr(self._inner, "_ws", None)
            if ws is not None:
                try:
                    await ws.send(json.dumps({"type": "audio_format", "sample_rate": rate}))
                except Exception:
                    logger.debug("Could not send audio_format message", exc_info=True)
            self._outbound_rate = rate
            logger.info("Outbound audio rate: %d Hz", rate)
        await self._inner.send_audio(chunk)  # type: ignore[attr-defined]


# ── HTTP server ──────────────────────────────────────────────────


def _run_http_server() -> None:
    """Serve static files from the examples directory in a background thread."""
    handler = functools.partial(SimpleHTTPRequestHandler, directory=_STATIC_DIR)
    httpd = HTTPServer(("0.0.0.0", HTTP_PORT), handler)
    httpd.serve_forever()


# ── Main ─────────────────────────────────────────────────────────


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")
    adapter = build_openai_agents_adapter(
        instructions="You are a helpful voice assistant. Keep responses concise."
    )

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=WebSocketTransportConfig(port=WS_PORT),
        agent=adapter,
        wrap_agent=False,
        event_logging=default_event_logging(),
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    # Wrap the transport so the pipeline always sees 16 kHz audio regardless
    # of the browser's actual capture rate.
    session.transport = _ResamplingTransport(session.transport)

    # Serve the HTML client on a background thread.
    http_thread = threading.Thread(target=_run_http_server, daemon=True)
    http_thread.start()
    print(f"Open http://localhost:{HTTP_PORT}/ws_browser_client.html in your browser")

    # Start the EasyCat session (launches WebSocket server on WS_PORT).
    await session.start()

    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
