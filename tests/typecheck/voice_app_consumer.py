"""Static downstream contract for VoiceApp's beginner-facing call shapes."""

from collections.abc import Callable
from typing import Any, Literal

from easycat import EasyConfig, VoiceApp
from easycat.session import Session

Mode = Literal["local", "browser", "websocket", "twilio", "telnyx"]


def build_apps(
    agent: Any,
    config: EasyConfig,
    config_factory: Callable[[Any], EasyConfig],
) -> tuple[VoiceApp, VoiceApp, VoiceApp]:
    high_level = VoiceApp(
        agent=agent,
        stt="deepgram/nova-2",
        tts="openai",
        vad="silero",
        debug="light",
        host="127.0.0.1",
        port=8080,
        serve_token=None,
        max_sessions=10,
        dev=True,
    )
    static = VoiceApp(config=config)
    per_connection = VoiceApp(config_factory=config_factory)
    return high_level, static, per_connection


def run_literal_modes(app: VoiceApp) -> Session:
    app.run("local", stt="openai/realtime", debug="full")
    app.run("mic", tts="openai")
    app.run(
        "browser",
        host="0.0.0.0",
        port=8080,
        serve_token="secret",
        max_sessions=20,
        unsafe_allow_no_auth=False,
        announce=False,
    )
    app.run("websocket", host="127.0.0.1", port=8765, max_sessions=20)
    app.run("ws", serve_token="secret")
    app.run(
        "twilio",
        host="0.0.0.0",
        media_port=8766,
        http_host="0.0.0.0",
        http_port=8000,
        stream_url="wss://example.test/media",
        stream_token_secret="stream-secret",
        twilio_auth_token="twilio-secret",
        trust_proxy_headers=True,
        unsafe_allow_unsigned_webhooks=False,
        max_sessions=20,
        start_timeout_s=10.0,
        public_twiml_url="https://example.test/twiml",
        drain_timeout_s=30.0,
        force_shutdown_timeout_s=5.0,
    )
    app.run("phone", stream_url=None, twilio_auth_token=None)
    app.run(
        "telnyx",
        host="0.0.0.0",
        media_port=8766,
        http_host="0.0.0.0",
        http_port=8000,
        webhook_path="/telnyx",
        stream_url="wss://example.test/media",
        stream_token_secret="stream-secret",
        telnyx_api_key="telnyx-key",
        telnyx_public_key="telnyx-public-key",
        unsafe_allow_unsigned_webhooks=False,
        max_sessions=20,
        start_timeout_s=10.0,
        drain_timeout_s=30.0,
        force_shutdown_timeout_s=5.0,
    )
    app.session("local", agent=object(), vad="silero")
    return app.session("mic", debug="off")


def run_selected_mode(app: VoiceApp, mode: Mode) -> None:
    """A validated union of canonical modes remains accepted."""
    app.run(mode)


async def serve_literal_modes(app: VoiceApp) -> None:
    await app.serve("local", debug="light")
    await app.serve("browser", announce=False)
    await app.serve("ws", serve_token="secret")
    await app.serve(
        "phone",
        stream_url="wss://example.test/media",
        twilio_auth_token="twilio-secret",
    )
    await app.serve(
        "telnyx",
        stream_url="wss://example.test/media",
        telnyx_api_key="telnyx-key",
        telnyx_public_key="telnyx-public-key",
    )
