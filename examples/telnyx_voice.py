"""Telnyx Call Control voice agent with per-call EasyCat sessions.

Setup:
  Run:   uv run python examples/telnyx_voice.py
  Run with .env:  uv run --env-file .env python examples/telnyx_voice.py
  uv run easycat doctor
  uv run easycat doctor --env-file .env
  uv run easycat doctor --env-file .env --json
  export OPENAI_API_KEY="..."
  export TELNYX_STREAM_URL="wss://your-public-host:8766"
  export TELNYX_API_KEY="..."           # Call Control Bearer token
  export TELNYX_PUBLIC_KEY="..."        # Ed25519 public key from the portal
  export TELNYX_STREAM_TOKEN_SECRET="..."  # optional, pins stream-token signing key
  uv sync --extra openai --extra telnyx --extra openai-agents --group dev
  uv run easycat doctor --env-file .env
  uv run --env-file .env python examples/telnyx_voice.py
"""

from __future__ import annotations

import os

from easycat.voice_app import VoiceApp


def main() -> None:
    """Run the Telnyx voice app."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is required. Set it in your environment or .env file.")

    app = VoiceApp(stt="openai", tts="openai")
    app.run("telnyx")


if __name__ == "__main__":
    main()
