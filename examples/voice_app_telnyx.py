"""App-first Telnyx phone bot: one ``VoiceApp``, one mode switch.

``VoiceApp.run("telnyx")`` delegates to the reusable two-listener Telnyx server
helper in ``easycat.telephony.telnyx_server`` — an aiohttp ``POST /telnyx``
webhook plus a raw media WebSocket. Per-call telephony behavior (DTMF
aggregation, voicemail detection) is opted in through the ``config_factory``
via ``TelephonyConfig``, not through server config.

Setup: export OPENAI_API_KEY=...; export TELNYX_STREAM_URL=wss://your-host:8766
       export TELNYX_API_KEY=...      # answers calls via Call Control
       export TELNYX_PUBLIC_KEY=...   # Ed25519 key verifying POST /telnyx deliveries
       uv sync --extra openai --extra telnyx --extra openai-agents --group dev
       uv run easycat doctor
       uv run easycat doctor --env-file .env  # if keys live in .env
       uv run easycat doctor --env-file .env --json  # for parseable checks
Run:   uv run python examples/voice_app_telnyx.py
       uv run --env-file .env python examples/voice_app_telnyx.py  # if keys live in .env

Point your Call Control application's webhook at this server's POST /telnyx
route. Unlike Twilio, Telnyx signs only the HTTP webhook (Ed25519 over
``{timestamp}|{raw_body}``); the media WebSocket handshake carries no
signature, so each verified ``call.initiated`` mints a one-time call-bound
stream token that the answer command embeds in ``stream_url``.

For the lower-level FastAPI reference (outbound calls, status callbacks), see
examples/telnyx_app.py.
"""

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. For an app, run: "
        "uv add 'easycat[openai,telnyx,openai-agents]'. In this repo, run: "
        "uv sync --extra openai --extra telnyx --extra openai-agents --group dev"
    ) from exc

from easycat import EasyConfig, TelephonyConfig, VoiceApp, require_env
from easycat.transports import TelnyxConnectionTransport


def main() -> None:
    require_env("OPENAI_API_KEY")
    stream_url = require_env("TELNYX_STREAM_URL")
    telnyx_api_key = require_env("TELNYX_API_KEY")
    telnyx_public_key = require_env("TELNYX_PUBLIC_KEY")

    def config_factory(transport: TelnyxConnectionTransport) -> EasyConfig:
        return EasyConfig.phone(
            provider="telnyx",
            transport=transport,
            agent=Agent(name="assistant", instructions="You are a helpful phone assistant."),
            telephony=TelephonyConfig(
                enable_dtmf_aggregator=True,
                enable_voicemail_detector=True,
            ),
        )

    app = VoiceApp(config_factory=config_factory)
    app.run(
        "telnyx",
        stream_url=stream_url,
        telnyx_api_key=telnyx_api_key,
        telnyx_public_key=telnyx_public_key,
    )


if __name__ == "__main__":
    main()
