"""FastAPI + Twilio Media Streams server for the scaffolded phone agent."""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import websockets
from fastapi import FastAPI, Response
from websockets.asyncio.server import ServerConnection

from agent import make_agent
from easycat import (
    EasyConfig,
    SessionManager,
    TelephonyConfig,
    TwilioConnectionTransport,
    create_session,
    require_env,
)
from easycat.transports import TwilioStreamTokenStore, TwilioTransportConfig
from easycat.transports.twilio_media import twiml_connect_stream


def create_app() -> FastAPI:
    require_env("OPENAI_API_KEY")
    stream_url = require_env("TWILIO_STREAM_URL")
    manager: SessionManager[int] = SessionManager()
    stream_tokens = TwilioStreamTokenStore(os.getenv("TWILIO_STREAM_TOKEN_SECRET") or None)

    async def handle_call(ws: ServerConnection) -> None:
        transport = TwilioConnectionTransport(
            ws,
            config=TwilioTransportConfig(stream_token_validator=stream_tokens.consume),
        )
        config = EasyConfig(
            transport=transport,
            telephony=TelephonyConfig(
                enable_dtmf_aggregator=True,
                enable_voicemail_detector=True,
            ),
            agent=make_agent(),
            **__EASYCAT_CONFIG_EXTRA__,  # noqa: F821
        )
        async with manager.connection(id(ws), create_session(config), runtime_feedback=True):
            await ws.wait_closed()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        port = int(os.getenv("TWILIO_WS_PORT", "8766"))
        twilio_ws = await websockets.serve(handle_call, "0.0.0.0", port)
        try:
            yield
        finally:
            twilio_ws.close()
            await twilio_ws.wait_closed()
            await manager.stop_all()

    app = FastAPI(lifespan=lifespan)

    @app.post("/twiml")
    async def twiml() -> Response:
        xml = twiml_connect_stream(stream_url, stream_token=stream_tokens.issue())
        return Response(content=xml, media_type="application/xml")

    return app
