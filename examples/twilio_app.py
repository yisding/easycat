# ruff: noqa: E501

"""Twilio Media Streams example with per-call EasyCat sessions.

Setup:
  export OPENAI_API_KEY="..."
  export TWILIO_STREAM_URL="wss://your-public-host:8766"
  export TWILIO_ACCOUNT_SID="AC..."
  export TWILIO_AUTH_TOKEN="..."
  export TWILIO_STREAM_TOKEN_SECRET="..."  # optional, pins stream-token signing key
  export TWILIO_MAX_SESSIONS="8"  # optional
  export TWILIO_VOICE_FROM="+15551234567"  # optional, enables POST /calls
  export TWILIO_TWIML_URL="https://your-public-host/twiml"
  export TWILIO_STATUS_CALLBACK_URL="https://your-public-host/status"
  export TWILIO_CALL_API_TOKEN="dev-only-token"
  export TWILIO_SMS_FROM="+15551234567"  # optional, enables send_sms actions
  uv sync --extra openai --extra telephony --extra telephony-fastapi --extra openai-agents --group dev
  uv run easycat doctor
  uv run easycat doctor --env-file .env  # if keys live in .env
  uv run easycat doctor --env-file .env --json  # for parseable checks
  uv run uvicorn examples.twilio_app:create_app --factory --host 0.0.0.0
  uv run --env-file .env uvicorn examples.twilio_app:create_app --factory --host 0.0.0.0
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import websockets
from websockets.asyncio.server import ServerConnection

from easycat import (
    EasyConfig,
    EventBus,
    SessionManager,
    TelephonyConfig,
    TwilioConnectionTransport,
    create_session,
    require_env,
)
from easycat.telephony import (
    TwilioCallSessionIndex,
    TwilioWebhookSignatureError,
    bearer_token_matches,
    emit_call_status,
    twilio_app_settings_from_env,
    twilio_form_items_from_request,
    twilio_public_url_from_request,
    twilio_stream_parameters_from_form,
    twilio_webhook_idempotency_key,
)
from easycat.transports import (
    TwilioStreamTokenStore,
    TwilioTransportConfig,
    twilio_websocket_signature_process_request,
)
from easycat.transports.twilio_media import twiml_connect_stream


def create_app(*, api_key: str | None = None, stream_url: str | None = None):
    api_key = api_key or require_env("OPENAI_API_KEY")
    settings = twilio_app_settings_from_env(stream_url=stream_url, require_auth_token=True)

    manager: SessionManager[int] = SessionManager()
    session_slots = asyncio.Semaphore(settings.max_sessions)
    sessions_by_call_sid = TwilioCallSessionIndex()
    stream_tokens = TwilioStreamTokenStore(settings.stream_token_secret_or_auth_token)
    outbound_bus = EventBus()
    outbound_manager = settings.start_outbound_manager(outbound_bus)

    async def handle_twilio_connection(ws: ServerConnection) -> None:
        from agents import Agent  # type: ignore[import-untyped]

        if session_slots.locked():
            await ws.close(code=1013, reason="Too many active Twilio sessions")
            return
        async with session_slots:
            transport = TwilioConnectionTransport(
                ws,
                config=TwilioTransportConfig(stream_token_validator=stream_tokens.consume_start),
            )
            if not await transport.wait_for_start(timeout_s=settings.start_timeout_s):
                return

            agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")
            telephony = TelephonyConfig(
                enable_dtmf_aggregator=True, enable_voicemail_detector=True
            )
            actions = settings.twilio_session_actions()
            if actions is not None:
                telephony.twilio_actions = actions
            session_config = EasyConfig(
                openai_api_key=api_key,
                transport=transport,
                telephony=telephony,
                agent=agent,
            )
            try:
                session = create_session(session_config)
            except BaseException:
                await transport.disconnect()
                raise
            cleanup_index = sessions_by_call_sid.track(session)
            try:
                async with manager.connection(id(ws), session, runtime_feedback=True):
                    await ws.wait_closed()
            finally:
                cleanup_index()

    from fastapi import FastAPI, HTTPException, Request, Response

    async def twilio_form(request: Request) -> list[tuple[str, str]]:
        try:
            return await twilio_form_items_from_request(
                request,
                auth_token=settings.auth_token or None,
                public_url=settings.public_twiml_url or None,
            )
        except TwilioWebhookSignatureError:
            raise HTTPException(status_code=403) from None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        twilio_server = await websockets.serve(
            handle_twilio_connection,
            "0.0.0.0",
            8766,
            process_request=twilio_websocket_signature_process_request(
                settings.auth_token, settings.stream_url
            ),
            compression=None,
        )
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
        call_sid = form.get("CallSid", "").strip()
        if not call_sid:
            raise HTTPException(status_code=400, detail="Twilio webhook is missing CallSid")
        parameters = twilio_stream_parameters_from_form(form_items)
        public_url = settings.public_twiml_url or twilio_public_url_from_request(request)
        xml = twiml_connect_stream(
            settings.stream_url,
            parameters=parameters,
            stream_token=stream_tokens.issue(
                idempotency_key=twilio_webhook_idempotency_key(
                    url=public_url,
                    params=form_items,
                    signature=request.headers.get("x-twilio-signature"),
                ),
                claims={"CallSid": call_sid, **parameters},
            ),
        )
        return Response(content=xml, media_type="application/xml")

    @app.post("/status")
    async def status(request: Request) -> Response:
        form_items = await twilio_form(request)
        form = dict(form_items)
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
        if not bearer_token_matches(request.headers.get("authorization"), settings.call_api_token):
            raise HTTPException(status_code=401)
        payload = await request.json()
        to = str(payload.get("to", "")).strip()
        if not to:
            raise HTTPException(status_code=400, detail="JSON body must include 'to'.")
        call_sid = await outbound_manager.place_call(to)
        return {"call_sid": call_sid}

    return app
