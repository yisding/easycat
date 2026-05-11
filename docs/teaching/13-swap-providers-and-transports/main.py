"""Chapter 13 — swap providers AND transports.

One driver. Two orthogonal axes. Six combinations:

                Local     WebRTC     Twilio
  openai         ✓          ✓         ✓
  deepgram-eleven ✓         ✓         ✓

Only the **two Local cells** run out of the box — WebRTC and
Twilio need a connected client (browser or phone call) and are
covered by the respective examples. The *code shape* is the
same: `EasyConfig(transport=...)` is the only line that
changes.

    # Axis 1 — swap providers (same transport)
    uv run python docs/teaching/13-swap-providers-and-transports/main.py \\
        --provider-mix openai --transport local
    uv run python docs/teaching/13-swap-providers-and-transports/main.py \\
        --provider-mix deepgram-eleven --transport local

    # Axis 2 — swap transport (same providers)
    uv run python docs/teaching/13-swap-providers-and-transports/main.py \\
        --provider-mix openai --transport webrtc
    uv run python docs/teaching/13-swap-providers-and-transports/main.py \\
        --provider-mix openai --transport twilio

Dependencies:
    uv sync --extra quickstart --group dev
    For WebRTC: --extra webrtc
    For Twilio: --extra telephony
    OPENAI_API_KEY (always)
    DEEPGRAM_API_KEY, ELEVENLABS_API_KEY (for deepgram-eleven mix)
    TWIML/Twilio credentials (for twilio transport)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path

from easycat import (
    EasyConfig,
    LocalTransportConfig,
    attach_runtime_feedback,
    create_session,
    export_debug_bundle,
    wait_for_shutdown_signal,
)

RUNS_DIR = Path(__file__).parent / "runs"


def build_agent() -> object:
    """Simple OpenAI-Agents-SDK agent. Provider-agnostic — the agent
    doesn't know or care which STT/TTS/transport is wired."""
    from agents import Agent  # type: ignore[import-untyped]

    return Agent(
        name="assistant",
        instructions="You are a helpful voice assistant. Keep replies brief.",
    )


def transport_config(name: str):
    if name == "local":
        return LocalTransportConfig()
    if name == "webrtc":
        # Requires `uv sync --extra webrtc`. The browser client connects via
        # SDP offer/answer; see `examples/webrtc_server.py` for the HTTP
        # signalling endpoint that pairs with WebRTCTransport.
        from easycat import WebRTCTransportConfig

        return WebRTCTransportConfig()
    if name == "twilio":
        # Requires `uv sync --extra telephony`. A live phone call connects
        # via Twilio Media Streams over WebSocket; see
        # `examples/twilio_app.py` for the Flask app that wires this up.
        from easycat.transports.twilio_media import TwilioTransportConfig

        return TwilioTransportConfig()
    raise SystemExit(f"Unknown transport: {name}")


def provider_mix(name: str) -> dict:
    """Return the STT/TTS strings for the named mix.

    All values are string shortcuts — ``EasyConfig.__post_init__``
    parses them into concrete config objects via the factory.
    """
    if name == "openai":
        return {"stt": "openai", "tts": "openai"}
    if name == "deepgram-eleven":
        if not os.getenv("DEEPGRAM_API_KEY") or not os.getenv("ELEVENLABS_API_KEY"):
            raise SystemExit("deepgram-eleven mix needs DEEPGRAM_API_KEY + ELEVENLABS_API_KEY.")
        return {"stt": "deepgram/nova-2", "tts": "elevenlabs"}
    raise SystemExit(f"Unknown provider mix: {name}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider-mix", choices=("openai", "deepgram-eleven"), default="openai")
    ap.add_argument("--transport", choices=("local", "webrtc", "twilio"), default="local")
    args = ap.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("Set OPENAI_API_KEY.")

    tag = f"{args.provider_mix}-{args.transport}"
    print(f"=== {tag} ===")

    mix = provider_mix(args.provider_mix)
    config = EasyConfig(
        openai_api_key=os.environ["OPENAI_API_KEY"],
        agent=build_agent(),
        transport=transport_config(args.transport),
        debug="light",  # journal must be on so export_debug_bundle works
        **mix,
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    await session.start()
    print("Session started. Talk (or connect a client).  Ctrl-C to stop.\n")
    try:
        await wait_for_shutdown_signal(session)
    finally:
        RUNS_DIR.mkdir(exist_ok=True)
        path = RUNS_DIR / f"ch13-{tag}-{int(time.time())}.bundle"
        try:
            export_debug_bundle(session, path, overwrite=True)
            print(f"Wrote bundle → {path.relative_to(Path.cwd())}")
        except Exception as exc:  # noqa: BLE001 — teaching script
            print(f"(no bundle written: {exc})")


if __name__ == "__main__":
    asyncio.run(main())
