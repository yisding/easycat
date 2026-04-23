"""WebRTC voice chat with the bundled debugger UI tailing the journal.

Same WebRTC pipeline as ``webrtc_server.py`` plus
``easycat.debugger.serve_session(...)`` running on port 8090. The
debugger UI shows a live timeline, per-turn waterfall, records
inspector, transcript, cost rollup, and audio playback — all driven by
the journal so the same UI works on a recorded ``RunBundle`` after the
session ends.

Setup: export OPENAI_API_KEY=...
       uv sync --extra openai-agents --extra webrtc --extra debugger
Run:   uv run python examples/webrtc_observability_server.py

Open:
       http://localhost:8080/webrtc_observability.html   (combined view: mic
                                                          + Start/Stop on top,
                                                          debugger UI below)
       http://localhost:8080/webrtc_client.html          (just the bot)
       http://localhost:8090                             (just the debugger)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

try:
    from agents import Agent, function_tool  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. Install with: uv sync --extra openai-agents"
    ) from exc

from easycat import (
    EasyCatConfig,
    ICEServer,
    WebRTCTransportConfig,
    attach_runtime_feedback,
    create_session,
    require_env,
    wait_for_shutdown_signal,
)
from easycat.debugger import serve_session

_STATIC_DIR = str(Path(__file__).parent / "webrtc_static")


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


@function_tool
def add_numbers(a: float, b: float) -> float:
    """Return a + b."""
    return a + b


@function_tool
def multiply_numbers(a: float, b: float) -> float:
    """Return a * b."""
    return a * b


async def main() -> None:
    api_key = require_env("OPENAI_API_KEY")

    signaling_host = os.getenv("SIGNALING_HOST", "0.0.0.0")
    signaling_port = int(os.getenv("SIGNALING_PORT", "8080"))
    debugger_host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    debugger_port = int(os.getenv("DASHBOARD_PORT", "8090"))

    session = create_session(
        EasyCatConfig(
            openai_api_key=api_key,
            transport=WebRTCTransportConfig(
                host=signaling_host,
                port=signaling_port,
                ice_servers=_build_ice_servers(),
                static_dir=_STATIC_DIR,
            ),
            agent=Agent(
                name="assistant",
                instructions=(
                    "You are a helpful voice assistant. Keep responses concise. "
                    "When the user asks math questions, use the available tools."
                ),
                tools=[add_numbers, multiply_numbers],
            ),
            # ``debug="light"`` keeps the journal in memory so the debugger UI
            # has records to read.
            debug="light",
        )
    )
    attach_runtime_feedback(session)

    serve_session(
        session,
        in_thread=True,
        host=debugger_host,
        port=debugger_port,
        open_browser=False,
        allow_remote=debugger_host not in ("127.0.0.1", "localhost"),
    )

    print(f"WebRTC + debugger: http://localhost:{signaling_port}/webrtc_observability.html")
    print(f"WebRTC client only: http://localhost:{signaling_port}/webrtc_client.html")
    print(f"Debugger only:     http://{debugger_host}:{debugger_port}")

    await session.start()
    await wait_for_shutdown_signal(session)


if __name__ == "__main__":
    asyncio.run(main())
