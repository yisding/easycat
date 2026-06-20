"""North-star app-first voice bot: one ``VoiceApp``, one mode switch.

``VoiceApp`` is the product-level noun for a voice bot. Build it once with your
agent, then pick a mode at run time — no session wiring required:

    app.run("browser")     # WebRTC + bundled browser client (default)
    app.run("local")       # local microphone / speakers
    app.run("websocket")   # per-client WebSocket sessions

Setup: export OPENAI_API_KEY=...; uv sync --extra quickstart --extra webrtc --group dev
       uv run easycat doctor
       uv run easycat doctor --env-file .env  # if keys live in .env
       uv run easycat doctor --env-file .env --json  # for parseable checks
Run:   uv run python examples/voice_app.py
       uv run --env-file .env python examples/voice_app.py  # if keys live in .env

Then open the printed URL (`Open http://localhost:8080`) and click Start.
"""

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents and aiortc are required. For an app, run: "
        "uv add 'easycat[quickstart,webrtc]'. In this repo, run: "
        "uv sync --extra quickstart --extra webrtc --group dev"
    ) from exc

from easycat import VoiceApp, require_env


def main() -> None:
    require_env("OPENAI_API_KEY")
    app = VoiceApp(
        agent=Agent(name="assistant", instructions="You are a helpful voice assistant.")
    )
    # Switch one word to change deployment: "local", "websocket", or "browser".
    app.run("browser")


if __name__ == "__main__":
    main()

# Next, try (change one token):
#   app.run("local")              talk over your mic/speakers, no browser
#   app.run("websocket")          serve per-client WebSocket sessions
#   app.run("twilio", stream_url=...)  real phone calls (needs the telephony
#                                 extra + TWILIO_STREAM_URL + TWILIO_AUTH_TOKEN;
#                                 see examples/voice_app_twilio.py)
#   VoiceApp(agent=..., stt="deepgram/nova-2", tts="elevenlabs")  swap providers
#   uv run easycat serve --mode browser   the same path, from the CLI
