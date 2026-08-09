"""WebRTC voice chat with the bundled debugger UI tailing the journal.

Same WebRTC pipeline as ``webrtc_server.py`` plus
``easycat.debugger.serve_session(...)`` running on port 8090. The
debugger UI shows a live timeline, per-turn waterfall, records
inspector, transcript, and audio playback — all driven by
the journal so the same UI works on a recorded ``RunBundle`` after the
session ends.

Setup: export OPENAI_API_KEY=...
       uv sync --extra openai --extra openai-agents --extra webrtc --extra debugger --group dev
       uv run easycat doctor
       uv run easycat doctor --env-file .env  # if keys live in .env
       uv run easycat doctor --env-file .env --json  # for parseable checks
Run:   uv run python examples/webrtc_observability_server.py
       uv run --env-file .env python examples/webrtc_observability_server.py

Open:
       http://localhost:8080/webrtc_observability.html   (combined view: mic
                                                          + Start/Stop on top,
                                                          debugger UI below)
       http://localhost:8080/webrtc_client.html          (just the bot)
       http://localhost:8090                             (just the debugger)

When ``WEBRTC_SIGNALING_TOKEN`` is set, use the ``#token=`` URLs printed at
startup and replace the placeholder with the URL-encoded token value.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from agents import Agent, function_tool  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. For an app, run: "
        "uv add 'easycat[openai,openai-agents,webrtc,debugger]'. In this repo, run: "
        "uv sync --extra openai --extra openai-agents --extra webrtc --extra debugger --group dev"
    ) from exc

from easycat import EasyConfig, create_session
from easycat.debugger import serve_session
from easycat.helpers import run_session
from easycat.transports import webrtc_transport_config_from_env

_STATIC_DIR = str(Path(__file__).parent / "webrtc_static")


@function_tool
def add_numbers(a: float, b: float) -> float:
    """Return a + b."""
    return a + b


@function_tool
def multiply_numbers(a: float, b: float) -> float:
    """Return a * b."""
    return a * b


def main() -> None:
    debugger_host = os.getenv("DASHBOARD_HOST", "127.0.0.1")
    debugger_port = int(os.getenv("DASHBOARD_PORT", "8090"))
    transport = webrtc_transport_config_from_env(static_dir=_STATIC_DIR)

    session = create_session(
        EasyConfig.browser(
            transport=transport,
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

    serve_session(
        session,
        in_thread=True,
        host=debugger_host,
        port=debugger_port,
        open_browser=False,
        allow_remote=debugger_host not in ("127.0.0.1", "localhost"),
    )

    token_fragment = "#token=<URL_ENCODED_TOKEN>" if transport.auth_token else ""
    combined_url = (
        f"http://localhost:{transport.port}/webrtc_observability.html"
        f"?debugger_port={debugger_port}{token_fragment}"
    )
    client_token_fragment = "#token=<URL_ENCODED_TOKEN>" if transport.auth_token else ""
    print(f"WebRTC + debugger: {combined_url}")
    print(
        f"WebRTC client only: "
        f"http://localhost:{transport.port}/webrtc_client.html{client_token_fragment}"
    )
    print(f"Debugger only:     http://{debugger_host}:{debugger_port}")

    run_session(session)


if __name__ == "__main__":
    main()
