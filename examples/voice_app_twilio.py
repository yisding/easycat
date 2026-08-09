"""App-first Twilio phone bot: one ``VoiceApp``, one mode switch.

``VoiceApp.run("twilio")`` delegates to the reusable two-listener Twilio server
helper in ``easycat.telephony.server`` — a raw media WebSocket plus an HTTP
``/twiml`` route. Per-call telephony behavior (DTMF aggregation, voicemail
detection) is opted in through the ``config_factory`` via ``TelephonyConfig``,
not through server config.

Setup: export OPENAI_API_KEY=...; export TWILIO_STREAM_URL=wss://your-host:8766
       export TWILIO_AUTH_TOKEN=...  # signs/validates the POST /twiml webhook
       # Behind a TLS proxy: export TRUST_PROXY_HEADERS=1
       # Or pin the signed URL: export TWILIO_PUBLIC_TWIML_URL=https://your-host/twiml
       uv sync --extra openai --extra telephony --extra openai-agents --group dev
       uv run easycat doctor
       uv run easycat doctor --env-file .env  # if keys live in .env
       uv run easycat doctor --env-file .env --json  # for parseable checks
Run:   uv run python examples/voice_app_twilio.py
       uv run --env-file .env python examples/voice_app_twilio.py  # if keys live in .env

Point your Twilio number's voice webhook at this server's POST /twiml route.
``POST /twiml`` mints a media stream token on every request, so the webhook is
authenticated by default: ``TWILIO_AUTH_TOKEN`` validates the
``X-Twilio-Signature`` header before a token is issued. Behind a TLS-terminating
proxy, set ``TRUST_PROXY_HEADERS=1`` so the signed public URL is reconstructed
from forwarded headers, or set ``TWILIO_PUBLIC_TWIML_URL`` to the exact public
route used in Twilio's request signature.

For the lower-level FastAPI reference (outbound calls, status callbacks, SMS),
see examples/twilio_app.py.
"""

try:
    from agents import Agent  # type: ignore[import-untyped]
except ImportError as exc:
    raise SystemExit(
        "openai-agents is required. For an app, run: "
        "uv add 'easycat[openai,telephony,openai-agents]'. In this repo, run: "
        "uv sync --extra openai --extra telephony --extra openai-agents --group dev"
    ) from exc

from easycat import EasyConfig, TelephonyConfig, VoiceApp, require_env
from easycat.transports import TwilioConnectionTransport


def main() -> None:
    require_env("OPENAI_API_KEY")
    stream_url = require_env("TWILIO_STREAM_URL")
    twilio_auth_token = require_env("TWILIO_AUTH_TOKEN")

    def config_factory(transport: TwilioConnectionTransport) -> EasyConfig:
        return EasyConfig.phone(
            transport=transport,
            agent=Agent(name="assistant", instructions="You are a helpful phone assistant."),
            telephony=TelephonyConfig(
                enable_dtmf_aggregator=True,
                enable_voicemail_detector=True,
            ),
        )

    app = VoiceApp(config_factory=config_factory)
    app.run("twilio", stream_url=stream_url, twilio_auth_token=twilio_auth_token)


if __name__ == "__main__":
    main()
