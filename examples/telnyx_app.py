"""Telnyx Call Control example with per-call EasyCat sessions.

Setup:
  export OPENAI_API_KEY="..."
  export TELNYX_STREAM_URL="wss://your-public-host:8766"
  export TELNYX_API_KEY="..."
  export TELNYX_PUBLIC_KEY="..."  # Ed25519 public key from the Telnyx portal
  export TELNYX_STREAM_TOKEN_SECRET="..."  # optional, pins stream-token signing key
  export TELNYX_MAX_SESSIONS="8"  # optional
  export TELNYX_CONNECTION_ID="..."  # optional, enables POST /calls
  export TELNYX_VOICE_FROM="+15551234567"  # optional, enables POST /calls
  export TELNYX_WEBHOOK_URL="https://your-public-host/status"  # optional, enables POST /calls
  export TELNYX_CALL_API_TOKEN="dev-only-token"
  uv sync --extra openai --extra telnyx --extra telephony-fastapi --extra openai-agents --group dev
  uv run easycat doctor
  uv run easycat doctor --env-file .env  # if keys live in .env
  uv run easycat doctor --env-file .env --json  # for parseable checks
  uv run uvicorn examples.telnyx_app:create_app --factory --host 0.0.0.0
  uv run --env-file .env uvicorn examples.telnyx_app:create_app --factory --host 0.0.0.0

Telnyx signs the HTTP webhooks (Ed25519 over ``{timestamp}|{raw_body}``) but NOT
the media WebSocket handshake, so every verified ``call.initiated`` mints a
one-time call-bound stream token that the answer command embeds in
``stream_url``. Outbound dials point their webhooks at ``/status`` so billable
call placement never reuses the inbound answer path.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import websockets
from websockets.asyncio.server import ServerConnection

from easycat import (
    EasyConfig,
    EventBus,
    Session,
    SessionManager,
    TelephonyConfig,
    TelnyxConnectionTransport,
    create_session,
    require_env,
)
from easycat.server.transports import RuntimeSupervisor, WebSocketSessionRuntime
from easycat.telephony import (
    bearer_token_matches,
    build_answer_payload,
    parse_telnyx_webhook,
    telnyx_app_settings_from_env,
    telnyx_webhook_idempotency_key,
    verify_telnyx_webhook_signature,
)
from easycat.telephony._stream_tokens import STREAM_TOKEN_PARAMETER, StreamTokenStore
from easycat.telephony.outbound import (
    OutboundCallManager,
    TelnyxOutboundClient,
    emit_telnyx_call_event,
)
from easycat.telephony.telnyx import (
    TELNYX_WEBHOOK_SIGNATURE_HEADER,
    TELNYX_WEBHOOK_TIMESTAMP_HEADER,
)
from easycat.telephony.telnyx_client import TelnyxCallControlClient
from easycat.transports import TelnyxTransportConfig
from easycat.transports._limits import MAX_WEBSOCKET_MESSAGE_BYTES


def create_app(*, api_key: str | None = None, stream_url: str | None = None):
    api_key = api_key or require_env("OPENAI_API_KEY")
    settings = telnyx_app_settings_from_env(stream_url=stream_url)
    if not settings.public_key:
        raise RuntimeError("TELNYX_PUBLIC_KEY is required to verify Ed25519-signed webhooks.")
    if not settings.api_key:
        raise RuntimeError("TELNYX_API_KEY is required to answer calls via Call Control.")

    manager = SessionManager()
    stream_tokens = StreamTokenStore(settings.stream_token_secret or None)
    outbound_bus = EventBus()

    voice_from = os.getenv("TELNYX_VOICE_FROM", "").strip()
    status_url = os.getenv("TELNYX_WEBHOOK_URL", "").strip()
    outbound_manager: OutboundCallManager | None = None
    if settings.connection_id and voice_from and status_url:
        outbound_client = TelnyxOutboundClient(
            settings.api_key, connection_id=settings.connection_id, webhook_url=status_url
        )
        outbound_manager = OutboundCallManager(
            outbound_bus, from_number=voice_from, client=outbound_client
        )
        outbound_manager.start()

    async def build_session(ws: ServerConnection) -> Session | None:
        from agents import Agent  # type: ignore[import-untyped]

        transport = TelnyxConnectionTransport(
            ws,
            config=TelnyxTransportConfig(stream_token_validator=stream_tokens.consume_start),
        )
        if not await transport.wait_for_start(timeout_s=settings.start_timeout_s):
            return None

        agent = Agent(name="assistant", instructions="You are a helpful voice assistant.")
        telephony = TelephonyConfig(enable_dtmf_aggregator=True, enable_voicemail_detector=True)
        actions = settings.telnyx_session_actions()
        if actions is not None:
            telephony.telnyx_actions = actions
        try:
            return create_session(
                EasyConfig.phone(
                    provider="telnyx",
                    openai_api_key=api_key,
                    transport=transport,
                    telephony=telephony,
                    agent=agent,
                )
            )
        except BaseException:
            await transport.disconnect()
            raise

    runtime: WebSocketSessionRuntime[ServerConnection, Session] = WebSocketSessionRuntime(
        manager=manager,
        max_sessions=settings.max_sessions,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="telnyx-example-media-server",
        session_factory=build_session,
        runtime_feedback=True,
        capacity_reason="Too many active Telnyx sessions",
    )

    from fastapi import FastAPI, HTTPException, Request, Response

    async def verified_envelope(request: Request) -> dict[str, object]:
        body = await request.body()
        if not verify_telnyx_webhook_signature(
            payload=body,
            signature=request.headers.get(TELNYX_WEBHOOK_SIGNATURE_HEADER),
            timestamp=request.headers.get(TELNYX_WEBHOOK_TIMESTAMP_HEADER),
            public_key=settings.public_key,
        ):
            raise HTTPException(status_code=403)
        return parse_telnyx_webhook(body)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        telnyx_server = await websockets.serve(
            runtime.handle,
            "0.0.0.0",
            settings.ws_port,
            compression=None,
            max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
        )
        try:
            yield
        finally:
            if outbound_manager is not None:
                outbound_manager.stop()
            await runtime.drain(
                telnyx_server,
                drain_timeout_s=settings.drain_timeout_s,
                force_timeout_s=settings.force_shutdown_timeout_s,
            )

    app = FastAPI(lifespan=lifespan)

    @app.post("/telnyx")
    async def telnyx(request: Request) -> Response:
        envelope = await verified_envelope(request)
        if envelope.get("event_type") != "call.initiated":
            # AMD/streaming-lifecycle deliveries are answered by the session pipeline; ack them.
            return Response(status_code=200)
        raw_payload = envelope.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        call_control_id = str(
            payload.get("call_control_id") or envelope.get("call_control_id") or ""
        ).strip()
        if not call_control_id:
            raise HTTPException(400, detail="Telnyx call.initiated missing call_control_id")
        stream_token = stream_tokens.issue(
            idempotency_key=telnyx_webhook_idempotency_key(envelope),
            claims={"CallSid": call_control_id},
        )
        client = TelnyxCallControlClient(settings.api_key)
        try:
            await client.answer(
                call_control_id,
                build_answer_payload(
                    stream_url=f"{settings.stream_url}/?{STREAM_TOKEN_PARAMETER}={stream_token}",
                    client_state={"call_control_id": call_control_id},
                ),
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Telnyx answer failed: {exc}") from exc
        finally:
            await client.close()
        return Response(status_code=200)

    @app.post("/status")
    async def status(request: Request) -> Response:
        envelope = await verified_envelope(request)
        await emit_telnyx_call_event(envelope, outbound_bus)
        return Response(status_code=204)

    @app.post("/calls")
    async def calls(request: Request) -> dict[str, str]:
        if outbound_manager is None:
            raise HTTPException(
                status_code=503,
                detail="Set TELNYX_CONNECTION_ID, TELNYX_VOICE_FROM, and TELNYX_WEBHOOK_URL.",
            )
        call_api_token = os.getenv("TELNYX_CALL_API_TOKEN", "").strip()
        if not call_api_token:
            raise HTTPException(503, detail="Set TELNYX_CALL_API_TOKEN to expose POST /calls.")
        if not bearer_token_matches(request.headers.get("authorization"), call_api_token):
            raise HTTPException(status_code=401)
        payload = await request.json()
        to = str(payload.get("to", "")).strip()
        if not to:
            raise HTTPException(status_code=400, detail="JSON body must include 'to'.")
        call_control_id = await outbound_manager.place_call(to)
        return {"call_control_id": call_control_id}

    return app
