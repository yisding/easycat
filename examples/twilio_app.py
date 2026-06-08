"""Twilio Media Streams example with per-call EasyCat sessions.

Setup:
  export OPENAI_API_KEY="..."
  export TWILIO_STREAM_URL="wss://your-public-host:8766"
  export TWILIO_ACCOUNT_SID="AC..."
  export TWILIO_AUTH_TOKEN="..."
  export TWILIO_STREAM_TOKEN_SECRET="..."  # optional, pins stream-token signing key
  export TWILIO_VOICE_FROM="+15551234567"  # optional, enables POST /calls
  export TWILIO_TWIML_URL="https://your-public-host/twiml"
  export TWILIO_STATUS_CALLBACK_URL="https://your-public-host/status"
  export TWILIO_CALL_API_TOKEN="dev-only-token"
  export TWILIO_SMS_FROM="+15551234567"  # optional, enables send_sms actions
  uv sync --extra openai --extra telephony --extra openai-agents --group dev
  uv run easycat doctor
  uv run easycat doctor --env-file .env  # if keys live in .env
  uv run easycat doctor --env-file .env --json  # for parseable checks
  uv run uvicorn examples.twilio_app:create_app --factory --host 0.0.0.0
  uv run --env-file .env uvicorn examples.twilio_app:create_app --factory --host 0.0.0.0
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection

from easycat import (
    CallAnswered,
    CallEnded,
    CallFailed,
    EasyConfig,
    EventBus,
    SessionManager,
    TelephonyConfig,
    TwilioConnectionTransport,
    create_session,
    require_env,
)
from easycat.telephony import (
    TwilioWebhookSignatureError,
    emit_call_status,
    twilio_app_settings_from_env,
    twilio_form_items_from_request,
    twilio_stream_parameters_from_form,
)
from easycat.transports import TwilioStreamTokenStore, TwilioTransportConfig
from easycat.transports.twilio_media import twiml_connect_stream


def create_app(*, api_key: str | None = None, stream_url: str | None = None):
    api_key = api_key or require_env("OPENAI_API_KEY")
    settings = twilio_app_settings_from_env(stream_url=stream_url)

    manager: SessionManager[int] = SessionManager()
    sessions_by_call_sid: dict[str, Any] = {}
    stream_tokens = TwilioStreamTokenStore(settings.stream_token_secret_or_auth_token)
    outbound_bus: EventBus | None = EventBus()
    outbound_manager = settings.start_outbound_manager(outbound_bus)
    if outbound_manager is None:
        outbound_bus = None

    async def handle_twilio_connection(ws: ServerConnection) -> None:
        from agents import Agent  # type: ignore[import-untyped]

        agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")
        transport = TwilioConnectionTransport(
            ws,
            config=TwilioTransportConfig(stream_token_validator=stream_tokens.consume),
        )
        telephony = TelephonyConfig(
            enable_dtmf_aggregator=True,
            enable_voicemail_detector=True,
        )
        actions = settings.twilio_session_actions()
        if actions is not None:
            telephony.twilio_actions = actions
        session = create_session(
            EasyConfig(
                openai_api_key=api_key,
                transport=transport,
                telephony=telephony,
                agent=agent,
            )
        )
        key = id(ws)
        call_sid: str | None = None

        def remember_call(event: CallAnswered) -> None:
            nonlocal call_sid
            if event.call_sid:
                call_sid = event.call_sid
                sessions_by_call_sid[event.call_sid] = session

        def forget_call(event: CallEnded | CallFailed) -> None:
            nonlocal call_sid
            if event.call_sid:
                sessions_by_call_sid.pop(event.call_sid, None)
            if event.call_sid == call_sid:
                call_sid = None

        session.event_bus.subscribe(CallAnswered, remember_call)
        session.event_bus.subscribe(CallEnded, forget_call)
        session.event_bus.subscribe(CallFailed, forget_call)
        try:
            async with manager.connection(key, session, runtime_feedback=True):
                await ws.wait_closed()
        finally:
            if call_sid:
                sessions_by_call_sid.pop(call_sid, None)

    from fastapi import FastAPI, HTTPException, Request, Response

    async def twilio_form(request: Request) -> list[tuple[str, str]]:
        try:
            return await twilio_form_items_from_request(
                request,
                auth_token=settings.auth_token or None,
            )
        except TwilioWebhookSignatureError:
            raise HTTPException(status_code=403) from None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        twilio_server = await websockets.serve(handle_twilio_connection, "0.0.0.0", 8766)
        try:
            yield
        finally:
            if outbound_manager is not None:
                outbound_manager.stop()
            twilio_server.close()
            await twilio_server.wait_closed()
            await manager.stop_all()

    app = FastAPI(lifespan=lifespan)

    @app.post("/twiml")
    async def twiml(request: Request) -> Response:
        form_items = await twilio_form(request)
        xml = twiml_connect_stream(
            settings.stream_url,
            parameters=twilio_stream_parameters_from_form(form_items),
            stream_token=stream_tokens.issue(),
        )
        return Response(content=xml, media_type="application/xml")

    @app.post("/status")
    async def status(request: Request) -> Response:
        form_items = await twilio_form(request)
        form = dict(form_items)
        if outbound_bus is not None:
            await emit_call_status(form, outbound_bus)
        session = sessions_by_call_sid.get(form.get("CallSid", ""))
        if session is not None:
            await emit_call_status(form, session.event_bus)
        return Response(status_code=204)

    @app.post("/calls")
    async def calls(request: Request) -> dict[str, str]:
        if outbound_manager is None:
            raise HTTPException(
                status_code=503,
                detail=(
                    "Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_VOICE_FROM, "
                    "and TWILIO_TWIML_URL to enable outbound calling."
                ),
            )
        if not settings.call_api_token:
            raise HTTPException(
                status_code=503,
                detail="Set TWILIO_CALL_API_TOKEN before exposing POST /calls.",
            )
        if request.headers.get("authorization") != f"Bearer {settings.call_api_token}":
            raise HTTPException(status_code=401)
        payload = await request.json()
        to = str(payload.get("to", "")).strip()
        if not to:
            raise HTTPException(status_code=400, detail="JSON body must include 'to'.")
        call_sid = await outbound_manager.place_call(to)
        return {"call_sid": call_sid}

    return app
