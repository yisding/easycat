"""FastAPI + Twilio Media Streams server for the scaffolded phone agent."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import websockets
from agent import make_agent
from fastapi import FastAPI, HTTPException, Request, Response
from websockets.asyncio.server import ServerConnection

from easycat import (
    EasyConfig,
    Session,
    SessionManager,
    TelephonyConfig,
    TwilioConnectionTransport,
    create_session,
    require_env,
)
from easycat.server.transports import WebSocketSessionRuntime
from easycat.telephony import (
    reconstruct_public_url,
    twilio_webhook_idempotency_key,
    validate_twilio_webhook_signature,
)
from easycat.transports import (
    TwilioStreamTokenStore,
    TwilioTransportConfig,
    twilio_websocket_signature_process_request,
)
from easycat.transports._limits import MAX_WEBSOCKET_MESSAGE_BYTES
from easycat.transports.twilio_media import twiml_connect_stream


def _public_twilio_url(
    request: Request,
    *,
    configured_url: str,
    trust_proxy: bool,
) -> str:
    if configured_url:
        return configured_url
    raw_path = request.scope.get("raw_path", b"/twiml").decode("ascii")
    query = request.scope.get("query_string", b"").decode("ascii")
    path = f"{raw_path}?{query}" if query else raw_path
    return reconstruct_public_url(
        request.headers,
        path,
        trust_proxy=trust_proxy,
        default_scheme=request.url.scheme,
    )


def _stream_parameters(form: dict[str, str]) -> dict[str, str]:
    parameters: dict[str, str] = {"Direction": form.get("Direction") or "inbound"}
    for name in ("From", "To", "CallerName", "FromCity", "FromState", "FromZip", "FromCountry"):
        if form.get(name):
            parameters[name] = form[name]
    return parameters


def create_app() -> FastAPI:
    require_env("OPENAI_API_KEY")
    stream_url = require_env("TWILIO_STREAM_URL")
    twilio_auth_token = require_env("TWILIO_AUTH_TOKEN")
    trust_proxy_headers = os.getenv("TRUST_PROXY_HEADERS", "").lower() in {
        "1",
        "true",
        "yes",
    }
    max_sessions = int(os.getenv("TWILIO_MAX_SESSIONS", "8"))
    start_timeout_s = float(os.getenv("TWILIO_START_TIMEOUT_S", "10"))
    drain_timeout_s = float(os.getenv("TWILIO_DRAIN_TIMEOUT_S", "30"))
    force_shutdown_timeout_s = float(os.getenv("TWILIO_FORCE_SHUTDOWN_TIMEOUT_S", "10"))
    public_twiml_url = os.getenv("TWILIO_PUBLIC_TWIML_URL", "").strip()
    manager: SessionManager[int] = SessionManager()
    stream_tokens = TwilioStreamTokenStore(os.getenv("TWILIO_STREAM_TOKEN_SECRET") or None)

    async def build_session(ws: ServerConnection) -> Session | None:
        transport = TwilioConnectionTransport(
            ws,
            config=TwilioTransportConfig(stream_token_validator=stream_tokens.consume_start),
        )
        if not await transport.wait_for_start(timeout_s=start_timeout_s):
            return None
        config = EasyConfig(
            transport=transport,
            telephony=TelephonyConfig(
                enable_dtmf_aggregator=True,
                enable_voicemail_detector=True,
            ),
            agent=make_agent(),
            **__EASYCAT_CONFIG_EXTRA__,  # noqa: F821
        )
        try:
            return create_session(config)
        except BaseException:
            await transport.disconnect()
            raise

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
            max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
        )
        try:
            yield
        finally:
            await runtime.drain(
                twilio_ws,
                drain_timeout_s=drain_timeout_s,
                force_timeout_s=force_shutdown_timeout_s,
            )

    app = FastAPI(lifespan=lifespan)

    async def signed_twilio_form(request: Request) -> list[tuple[str, str]]:
        body = (await request.body()).decode()
        form_items = parse_qsl(body, keep_blank_values=True)
        if not validate_twilio_webhook_signature(
            auth_token=twilio_auth_token,
            url=_public_twilio_url(
                request,
                configured_url=public_twiml_url,
                trust_proxy=trust_proxy_headers,
            ),
            params=form_items,
            signature=request.headers.get("x-twilio-signature"),
        ):
            raise HTTPException(status_code=403)
        return form_items

    @app.post("/twiml")
    async def twiml(request: Request) -> Response:
        form_items = await signed_twilio_form(request)
        form = dict(form_items)
        call_sid = form.get("CallSid", "").strip()
        if not call_sid:
            raise HTTPException(status_code=400, detail="Twilio webhook is missing CallSid")
        parameters = _stream_parameters(form)
        xml = twiml_connect_stream(
            stream_url,
            parameters=parameters,
            stream_token=stream_tokens.issue(
                idempotency_key=twilio_webhook_idempotency_key(
                    url=_public_twilio_url(
                        request,
                        configured_url=public_twiml_url,
                        trust_proxy=trust_proxy_headers,
                    ),
                    params=form_items,
                    signature=request.headers.get("x-twilio-signature"),
                ),
                claims={"CallSid": call_sid, **parameters},
            ),
        )
        return Response(content=xml, media_type="application/xml")

    return app
