"""Twilio Media Streams example with a TwiML webhook.

Setup:
  export OPENAI_API_KEY="..."
  export TWILIO_STREAM_URL="wss://your-public-host:8766"
  uv sync --extra telephony --extra openai-agents
  uv run uvicorn examples.twilio_app:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from easycat import EasyCatConfig, TelephonyConfig, TwilioTransportConfig, create_session
from easycat.transports.twilio_media import twiml_connect_stream

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import build_openai_agents_adapter, default_event_logging  # noqa: E402
from runtime_feedback import attach_runtime_feedback  # noqa: E402


def create_app(*, api_key: str | None = None, stream_url: str | None = None):
    api_key = api_key or os.getenv("OPENAI_API_KEY")
    stream_url = stream_url or os.getenv("TWILIO_STREAM_URL")
    if not api_key or not stream_url:
        raise RuntimeError("OPENAI_API_KEY and TWILIO_STREAM_URL are required.")

    try:
        adapter = build_openai_agents_adapter(instructions="You are a helpful voice assistant.")
    except SystemExit as exc:
        raise RuntimeError(str(exc)) from exc

    config = EasyCatConfig(
        openai_api_key=api_key,
        transport=TwilioTransportConfig(),
        telephony=TelephonyConfig(
            enable_dtmf_aggregator=True,
            enable_voicemail_detector=True,
        ),
        agent=adapter,
        wrap_agent=False,
        event_logging=default_event_logging(),
    )
    session = create_session(config)
    attach_runtime_feedback(session)

    from fastapi import FastAPI, Response

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await session.start()
        yield
        await session.stop()

    app = FastAPI(lifespan=lifespan)

    @app.post("/twiml")
    async def twiml() -> Response:
        xml = twiml_connect_stream(stream_url)
        return Response(content=xml, media_type="application/xml")

    return app
