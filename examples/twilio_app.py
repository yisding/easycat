"""Twilio Media Streams example with per-call EasyCat sessions.

Setup:
  export OPENAI_API_KEY="..."
  export TWILIO_STREAM_URL="wss://your-public-host:8766"
  export TWILIO_ACCOUNT_SID="AC..."
  export TWILIO_AUTH_TOKEN="..."
  export TWILIO_VOICE_FROM="+15551234567"  # optional, enables POST /calls
  export TWILIO_TWIML_URL="https://your-public-host/twiml"
  export TWILIO_STATUS_CALLBACK_URL="https://your-public-host/status"
  export TWILIO_CALL_API_TOKEN="dev-only-token"
  export TWILIO_SMS_FROM="+15551234567"  # optional, enables send_sms actions
  uv sync --extra telephony --extra openai-agents
  uv run uvicorn examples.twilio_app:create_app --factory --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qsl

import websockets
from websockets.asyncio.server import ServerConnection

from easycat import (
    CallAnswered,
    CallEnded,
    CallFailed,
    EasyCatConfig,
    EventBus,
    SessionManager,
    TelephonyConfig,
    TwilioConnectionTransport,
    TwilioSessionActionConfig,
    attach_runtime_feedback,
    create_session,
)
from easycat.telephony import (
    OutboundCallManager,
    emit_call_status,
    validate_twilio_webhook_signature,
)
from easycat.transports.twilio_media import twiml_connect_stream


def create_app(*, api_key: str | None = None, stream_url: str | None = None):
    api_key = api_key or os.getenv("OPENAI_API_KEY")
    stream_url = stream_url or os.getenv("TWILIO_STREAM_URL")
    if not api_key or not stream_url:
        raise RuntimeError("OPENAI_API_KEY and TWILIO_STREAM_URL are required.")
    twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
    twilio_voice_from = os.getenv("TWILIO_VOICE_FROM", "")
    twilio_twiml_url = os.getenv("TWILIO_TWIML_URL", "")
    twilio_status_callback_url = os.getenv("TWILIO_STATUS_CALLBACK_URL", "")
    twilio_call_api_token = os.getenv("TWILIO_CALL_API_TOKEN", "")
    twilio_sms_from = os.getenv("TWILIO_SMS_FROM", "")

    manager: SessionManager[int] = SessionManager()
    sessions_by_call_sid: dict[str, Any] = {}
    outbound_bus: EventBus | None = None
    outbound_manager: OutboundCallManager | None = None
    if twilio_account_sid and twilio_auth_token and twilio_voice_from and twilio_twiml_url:
        outbound_bus = EventBus()
        outbound_manager = OutboundCallManager(
            outbound_bus,
            from_number=twilio_voice_from,
            twilio_account_sid=twilio_account_sid,
            twilio_auth_token=twilio_auth_token,
            twiml_url=twilio_twiml_url,
            status_callback_url=twilio_status_callback_url,
        )
        outbound_manager.start()

    async def handle_twilio_connection(ws: ServerConnection) -> None:
        from agents import Agent  # type: ignore[import-untyped]

        agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")
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
                agent=agent,
            )
        )
        attach_runtime_feedback(session)
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
            async with manager.connection(key, session):
                await ws.wait_closed()
        finally:
            if call_sid:
                sessions_by_call_sid.pop(call_sid, None)

    from fastapi import FastAPI, HTTPException, Request, Response

    def public_twilio_url(request: Request) -> str:
        # Twilio signs the exact public URL it requested. If this app is
        # behind a trusted proxy, configure forwarded headers or adapt this
        # reconstruction to your deployment's canonical external URL.
        proto = request.headers.get("x-forwarded-proto")
        host = request.headers.get("x-forwarded-host") or request.headers.get("host")
        if proto and host:
            scheme = proto.split(",", 1)[0].strip()
            public_host = host.split(",", 1)[0].strip()
            suffix = request.url.path
            if request.url.query:
                suffix = f"{suffix}?{request.url.query}"
            return f"{scheme}://{public_host}{suffix}"
        return str(request.url)

    async def twilio_form(request: Request) -> list[tuple[str, str]]:
        form_items = parse_qsl((await request.body()).decode(), keep_blank_values=True)
        if twilio_auth_token and not validate_twilio_webhook_signature(
            auth_token=twilio_auth_token,
            url=public_twilio_url(request),
            params=form_items,
            signature=request.headers.get("x-twilio-signature"),
        ):
            raise HTTPException(status_code=403)
        return form_items

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
        form = dict(form_items)
        parameters = {"Direction": form.get("Direction") or "inbound"}
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
        xml = twiml_connect_stream(stream_url, parameters=parameters)
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
        if not twilio_call_api_token:
            raise HTTPException(
                status_code=503,
                detail="Set TWILIO_CALL_API_TOKEN before exposing POST /calls.",
            )
        if request.headers.get("authorization") != f"Bearer {twilio_call_api_token}":
            raise HTTPException(status_code=401)
        payload = await request.json()
        to = str(payload.get("to", "")).strip()
        if not to:
            raise HTTPException(status_code=400, detail="JSON body must include 'to'.")
        call_sid = await outbound_manager.place_call(to)
        return {"call_sid": call_sid}

    return app
