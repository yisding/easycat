"""Twilio Media Streams example with a TwiML webhook.

Setup:
  export OPENAI_API_KEY="..."
  export TWILIO_STREAM_URL="wss://your-public-host:8766"
  uv sync --extra telephony --extra openai-agents
  uv run uvicorn examples.twilio_app:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os

from easycat import (
    EasyCatConfig,
    OpenAIAgentsAdapter,
    TelephonyConfig,
    TwilioTransportConfig,
    create_session,
)
from easycat.transports.twilio_media import twiml_connect_stream


def create_app(*, api_key: str | None = None, stream_url: str | None = None):
    api_key = api_key or os.getenv("OPENAI_API_KEY")
    stream_url = stream_url or os.getenv("TWILIO_STREAM_URL")
    if not api_key or not stream_url:
        raise RuntimeError("OPENAI_API_KEY and TWILIO_STREAM_URL are required.")

    try:
        from agents import Agent  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "OpenAI Agents SDK is required. Install with: uv add easycat[openai-agents]"
        ) from exc

    voice_agent = Agent(
        name="VoiceAssistant",
        instructions="You are a helpful voice assistant.",
    )
    adapter = OpenAIAgentsAdapter(voice_agent)

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=TwilioTransportConfig(),
        telephony=TelephonyConfig(
            enable_dtmf_aggregator=True,
            enable_voicemail_detector=True,
        ),
        agent=adapter,
        wrap_agent=False,
    )
    session = create_session(config)

    from fastapi import FastAPI, Response

    app = FastAPI()

    @app.on_event("startup")
    async def _startup() -> None:
        await session.start()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await session.stop()

    @app.post("/twiml")
    async def twiml() -> Response:
        xml = twiml_connect_stream(stream_url)
        return Response(content=xml, media_type="application/xml")

    return app
