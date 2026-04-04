"""Twilio Media Streams example with per-call EasyCat sessions.

Setup:
  export OPENAI_API_KEY="..."
  export TWILIO_STREAM_URL="wss://your-public-host:8766"
  export TWILIO_ACCOUNT_SID="AC..."
  export TWILIO_AUTH_TOKEN="..."
  export TWILIO_SMS_FROM="+15551234567"  # optional, enables send_sms actions
  uv sync --extra telephony --extra openai-agents
  uv run uvicorn examples.twilio_app:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import websockets
from websockets.asyncio.server import ServerConnection

from easycat import (
    EasyCatConfig,
    SessionManager,
    TelephonyConfig,
    TwilioConnectionTransport,
    TwilioSessionActionConfig,
    attach_runtime_feedback,
    build_openai_agents_adapter,
    create_session,
    default_event_logging,
)
from easycat.transports.twilio_media import twiml_connect_stream


def create_app(*, api_key: str | None = None, stream_url: str | None = None):
    api_key = api_key or os.getenv("OPENAI_API_KEY")
    stream_url = stream_url or os.getenv("TWILIO_STREAM_URL")
    if not api_key or not stream_url:
        raise RuntimeError("OPENAI_API_KEY and TWILIO_STREAM_URL are required.")
    twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_sms_from = os.getenv("TWILIO_SMS_FROM", "")

    manager: SessionManager[int] = SessionManager()

    async def handle_twilio_connection(ws: ServerConnection) -> None:
        adapter = build_openai_agents_adapter(instructions="You are a helpful voice assistant.")
        transport = TwilioConnectionTransport(ws)
        telephony = TelephonyConfig(
            enable_dtmf_aggregator=True,
            enable_voicemail_detector=True,
        )
        if twilio_account_sid and twilio_auth_token:
            telephony.twilio_actions = TwilioSessionActionConfig(
                account_sid=twilio_account_sid,
                auth_token=twilio_auth_token,
                sms_from_number=twilio_sms_from,
            )
        session = create_session(
            EasyCatConfig(
                openai_api_key=api_key,
                transport=transport,
                telephony=telephony,
                agent=adapter,
                event_logging=default_event_logging(),
            )
        )
        attach_runtime_feedback(session)
        key = id(ws)
        async with manager.connection(key, session):
            await ws.wait_closed()

    from fastapi import FastAPI, Response

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        twilio_server = await websockets.serve(handle_twilio_connection, "0.0.0.0", 8766)
        try:
            yield
        finally:
            twilio_server.close()
            await twilio_server.wait_closed()
            await manager.stop_all()

    app = FastAPI(lifespan=lifespan)

    @app.post("/twiml")
    async def twiml() -> Response:
        xml = twiml_connect_stream(stream_url)
        return Response(content=xml, media_type="application/xml")

    return app
