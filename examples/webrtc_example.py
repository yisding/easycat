"""Web-based voice chat with an EasyCat agent.

Serves a web page and runs an EasyCat voice session over WebSocket.
The browser captures microphone audio via getUserMedia (WebRTC API),
sends PCM16 frames over WebSocket, and plays back the agent's audio.

The browser may capture at a higher sample rate (typically 48 kHz) than
the 16 kHz that EasyCat's pipeline expects.  A thin resampling transport
wrapper converts inbound audio to 16 kHz and normalises outbound audio
to 16 kHz so the client always receives a consistent format.

Setup:
    export OPENAI_API_KEY="..."
    uv add easycat[openai-agents]
    uv run python examples/webrtc_example.py

Then open http://localhost:8080/webrtc_example.html in your browser.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import signal
import threading
from collections.abc import AsyncIterator
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from easycat import (
    PCM16_MONO_16K,
    AudioChunk,
    EasyCatConfig,
    OpenAIAgentsAdapter,
    WebSocketTransportConfig,
    create_session,
)
from easycat.audio_utils import resample_chunk

logger = logging.getLogger(__name__)

HTTP_PORT = 8080
WS_PORT = 8765

_STATIC_DIR = str(Path(__file__).parent)

# ── Resampling transport wrapper ─────────────────────────────────


class _ResamplingTransport:
    """Wraps a Transport and resamples all audio to/from 16 kHz.

    Inbound:  client rate (e.g. 48 kHz) → 16 kHz for the EasyCat pipeline.
    Outbound: TTS rate (e.g. 24 kHz)    → 16 kHz for the browser client.
    """

    _INTERNAL_RATE = PCM16_MONO_16K.sample_rate  # 16 000

    def __init__(self, inner: object) -> None:
        self._inner = inner

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
        if chunk.format.sample_rate != self._INTERNAL_RATE:
            chunk = resample_chunk(chunk, self._INTERNAL_RATE)
        await self._inner.send_audio(chunk)  # type: ignore[attr-defined]


# ── HTTP server ──────────────────────────────────────────────────


def _run_http_server() -> None:
    """Serve static files from the examples directory in a background thread."""
    handler = functools.partial(SimpleHTTPRequestHandler, directory=_STATIC_DIR)
    httpd = HTTPServer(("0.0.0.0", HTTP_PORT), handler)
    httpd.serve_forever()


# ── Main ─────────────────────────────────────────────────────────


async def main() -> None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY is required.")

    try:
        from agents import Agent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SystemExit(
            "OpenAI Agents SDK is required. Install with: uv add easycat[openai-agents]"
        ) from exc

    voice_agent = Agent(
        name="VoiceAssistant",
        instructions="You are a helpful voice assistant. Keep responses concise.",
    )
    adapter = OpenAIAgentsAdapter(voice_agent)

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=WebSocketTransportConfig(port=WS_PORT),
        agent=adapter,
        wrap_agent=False,
    )
    session = create_session(config)

    # Wrap the transport so the pipeline always sees 16 kHz audio regardless
    # of the browser's actual capture rate.
    session.transport = _ResamplingTransport(session.transport)

    # Serve the HTML client on a background thread.
    http_thread = threading.Thread(target=_run_http_server, daemon=True)
    http_thread.start()
    print(f"Open http://localhost:{HTTP_PORT}/webrtc_example.html in your browser")

    # Start the EasyCat session (launches WebSocket server on WS_PORT).
    await session.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    await stop_event.wait()
    await session.stop()


if __name__ == "__main__":
    asyncio.run(main())
