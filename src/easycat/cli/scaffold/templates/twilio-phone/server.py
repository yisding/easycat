"""FastAPI + Twilio Media Streams server for the scaffolded phone agent."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import websockets
from agent import make_agent
from fastapi import FastAPI, HTTPException, Request, Response

from easycat import (
    EasyConfig,
    SessionManager,
    TelephonyConfig,
    TwilioConnectionTransport,
    create_session,
    require_env,
)
from easycat.server.transports import WebSocketSessionRuntime
from easycat.telephony import reconstruct_public_url, validate_twilio_webhook_signature
from easycat.transports import (
    TwilioStreamTokenStore,
    TwilioTransportConfig,
    twilio_websocket_signature_process_request,
)
from easycat.transports.twilio_media import twiml_connect_stream


def _public_twilio_url(request: Request, *, trust_proxy_headers: bool) -> str:
    path = request.url.path
    if request.url.query:
        path = f"{path}?{request.url.query}"
    return reconstruct_public_url(
        request.headers,
        path,
        trust_proxy=trust_proxy_headers,
        default_scheme=request.url.scheme,
    )


def _stream_parameters(form: dict[str, str]) -> dict[str, str]:
    parameters = {"Direction": form.get("Direction") or "inbound"}
    for name in ("From", "To", "CallerName", "FromCity", "FromState", "FromZip", "FromCountry"):
        if form.get(name):
            parameters[name] = form[name]
    return parameters


def create_app() -> FastAPI:
    require_env("OPENAI_API_KEY")
    stream_url = require_env("TWILIO_STREAM_URL")
    twilio_auth_token = require_env("TWILIO_AUTH_TOKEN")
    trust_proxy_headers = os.getenv("TRUST_PROXY_HEADERS", "").lower() in {"1", "true", "yes"}
    max_sessions = int(os.getenv("TWILIO_MAX_SESSIONS", "8"))
    drain_timeout_s = float(os.getenv("TWILIO_DRAIN_TIMEOUT_S", "30"))
    force_shutdown_timeout_s = float(os.getenv("TWILIO_FORCE_SHUTDOWN_TIMEOUT_S", "10"))
    manager: SessionManager[int] = SessionManager()
    stream_tokens = TwilioStreamTokenStore(os.getenv("TWILIO_STREAM_TOKEN_SECRET") or None)

    def build_session(ws: object) -> object:
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
        return create_session(config)

    runtime = WebSocketSessionRuntime(
        manager=manager,
        max_sessions=max_sessions,
        session_factory=build_session,
        capacity_reason="Too many active Twilio sessions",
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        port = int(os.getenv("TWILIO_WS_PORT", "8766"))
        process_request = twilio_websocket_signature_process_request(
            twilio_auth_token,
            stream_url,
        )
        twilio_ws = await websockets.serve(
            runtime.handle,
            "0.0.0.0",
            port,
            process_request=process_request,
            compression=None,
        )
        try:
            yield
        finally:
            await runtime.drain(
                twilio_ws,
                drain_timeout_s=max(drain_timeout_s, 0.0),
                force_timeout_s=max(force_shutdown_timeout_s, 0.0),
            )

    app = FastAPI(lifespan=lifespan)

    async def signed_twilio_form(request: Request) -> list[tuple[str, str]]:
        body = (await request.body()).decode()
        form_items = parse_qsl(body, keep_blank_values=True)
        if not validate_twilio_webhook_signature(
            auth_token=twilio_auth_token,
            url=_public_twilio_url(request, trust_proxy_headers=trust_proxy_headers),
            params=form_items,
            signature=request.headers.get("x-twilio-signature"),
        ):
            raise HTTPException(status_code=403)
        return form_items

    @app.post("/twiml")
    async def twiml(request: Request) -> Response:
        form_items = await signed_twilio_form(request)
        form = dict(form_items)
        xml = twiml_connect_stream(
            stream_url,
            parameters=_stream_parameters(form),
            stream_token=stream_tokens.issue(),
        )
        return Response(content=xml, media_type="application/xml")

    return app
