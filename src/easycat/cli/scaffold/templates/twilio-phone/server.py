"""FastAPI + Twilio Media Streams server for the scaffolded phone agent."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import websockets
from fastapi import FastAPI, HTTPException, Request, Response
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
from easycat.telephony import reconstruct_public_url, validate_twilio_webhook_signature
from easycat.transports import TwilioStreamTokenStore, TwilioTransportConfig
from easycat.transports.twilio_media import twiml_connect_stream


def create_app() -> FastAPI:
    require_env("OPENAI_API_KEY")
    stream_url = require_env("TWILIO_STREAM_URL")
    twilio_auth_token = require_env("TWILIO_AUTH_TOKEN")
    trust_proxy_headers = os.getenv("TRUST_PROXY_HEADERS", "").lower() in {"1", "true", "yes"}
    max_sessions = int(os.getenv("TWILIO_MAX_SESSIONS", "8"))
    manager: SessionManager[int] = SessionManager()
    session_slots = asyncio.Semaphore(max_sessions)
    stream_tokens = TwilioStreamTokenStore(os.getenv("TWILIO_STREAM_TOKEN_SECRET") or None)

    async def handle_call(ws: ServerConnection) -> None:
        if session_slots.locked():
            await ws.close(code=1013, reason="Too many active Twilio sessions")
            return

        async with session_slots:
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
            async with manager.connection(id(ws), create_session(config)):
                await ws.wait_closed()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        port = int(os.getenv("TWILIO_WS_PORT", "8766"))
        twilio_ws = await websockets.serve(handle_call, "0.0.0.0", port, compression=None)
        try:
            yield
        finally:
            twilio_ws.close()
            await twilio_ws.wait_closed()
            await manager.stop_all()

    app = FastAPI(lifespan=lifespan)

    def public_twilio_url(request: Request) -> str:
        path = request.url.path
        if request.url.query:
            path = f"{path}?{request.url.query}"
        return reconstruct_public_url(
            request.headers,
            path,
            trust_proxy=trust_proxy_headers,
            default_scheme=request.url.scheme,
        )

    async def signed_twilio_form(request: Request) -> list[tuple[str, str]]:
        body = (await request.body()).decode()
        form_items = parse_qsl(body, keep_blank_values=True)
        if not validate_twilio_webhook_signature(
            auth_token=twilio_auth_token,
            url=public_twilio_url(request),
            params=form_items,
            signature=request.headers.get("x-twilio-signature"),
        ):
            raise HTTPException(status_code=403)
        return form_items

    @app.post("/twiml")
    async def twiml(request: Request) -> Response:
        form_items = await signed_twilio_form(request)
        form = dict(form_items)
        parameters: dict[str, str] = {"Direction": form.get("Direction") or "inbound"}
        for name in (
            "From",
            "To",
            "CallerName",
            "FromCity",
            "FromState",
            "FromZip",
            "FromCountry",
        ):
            if form.get(name):
                parameters[name] = form[name]
        xml = twiml_connect_stream(
            stream_url,
            parameters=parameters,
            stream_token=stream_tokens.issue(),
        )
        return Response(content=xml, media_type="application/xml")

    return app
