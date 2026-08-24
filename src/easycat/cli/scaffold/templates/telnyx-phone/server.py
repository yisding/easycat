"""FastAPI + Telnyx Call Control server for the scaffolded phone agent."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import websockets
from easycat import (
    EasyConfig,
    Session,
    SessionManager,
    TelephonyConfig,
    TelnyxConnectionTransport,
    create_session,
    require_env,
)
from easycat.server.transports import RuntimeSupervisor, WebSocketSessionRuntime
from easycat.telephony import (
    build_answer_payload,
    parse_telnyx_webhook,
    telnyx_webhook_idempotency_key,
    verify_telnyx_webhook_signature,
)
from easycat.telephony._stream_tokens import STREAM_TOKEN_PARAMETER, StreamTokenStore
from easycat.telephony.telnyx import (
    TELNYX_WEBHOOK_SIGNATURE_HEADER,
    TELNYX_WEBHOOK_TIMESTAMP_HEADER,
)
from easycat.telephony.telnyx_client import TelnyxCallControlClient
from easycat.transports import TelnyxTransportConfig
from easycat.transports._limits import MAX_WEBSOCKET_MESSAGE_BYTES
from fastapi import FastAPI, HTTPException, Request, Response
from websockets.asyncio.server import ServerConnection

from agent import make_agent


def create_app() -> FastAPI:
    require_env("OPENAI_API_KEY")
    stream_url = require_env("TELNYX_STREAM_URL")
    telnyx_api_key = require_env("TELNYX_API_KEY")
    telnyx_public_key = require_env("TELNYX_PUBLIC_KEY")
    max_sessions = int(os.getenv("TELNYX_MAX_SESSIONS", "8"))
    start_timeout_s = float(os.getenv("TELNYX_START_TIMEOUT_S", "10"))
    drain_timeout_s = float(os.getenv("TELNYX_DRAIN_TIMEOUT_S", "30"))
    force_shutdown_timeout_s = float(os.getenv("TELNYX_FORCE_SHUTDOWN_TIMEOUT_S", "10"))
    manager: SessionManager[int] = SessionManager()
    stream_tokens = StreamTokenStore(os.getenv("TELNYX_STREAM_TOKEN_SECRET") or None)

    async def build_session(ws: ServerConnection) -> Session | None:
        transport = TelnyxConnectionTransport(
            ws,
            config=TelnyxTransportConfig(stream_token_validator=stream_tokens.consume_start),
        )
        if not await transport.wait_for_start(timeout_s=start_timeout_s):
            return None
        config = EasyConfig.phone(
            provider="telnyx",
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

    runtime: WebSocketSessionRuntime[ServerConnection, Session] = WebSocketSessionRuntime(
        manager=manager,
        max_sessions=max_sessions,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="telnyx-scaffold-media-server",
        session_factory=build_session,
        capacity_reason="Too many active Telnyx sessions",
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        media_port = int(os.getenv("TELNYX_WS_PORT", "8766"))
        telnyx_ws = await websockets.serve(
            runtime.handle,
            "0.0.0.0",
            media_port,
            compression=None,
            max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
        )
        try:
            yield
        finally:
            await runtime.drain(
                telnyx_ws,
                drain_timeout_s=drain_timeout_s,
                force_timeout_s=force_shutdown_timeout_s,
            )

    app = FastAPI(lifespan=lifespan)

    async def verified_envelope(request: Request) -> dict[str, object]:
        body = await request.body()
        if not verify_telnyx_webhook_signature(
            payload=body,
            signature=request.headers.get(TELNYX_WEBHOOK_SIGNATURE_HEADER),
            timestamp=request.headers.get(TELNYX_WEBHOOK_TIMESTAMP_HEADER),
            public_key=telnyx_public_key,
        ):
            raise HTTPException(status_code=403)
        envelope = parse_telnyx_webhook(body)
        if envelope is None:
            raise HTTPException(status_code=400, detail="Invalid Telnyx webhook envelope")
        return envelope

    @app.post("/telnyx")
    async def telnyx_webhook(request: Request) -> Response:
        envelope = await verified_envelope(request)
        if envelope.get("event_type") != "call.initiated":
            # AMD results and streaming lifecycle deliveries need no server-side
            # command; ack them so Telnyx does not retry.
            return Response(status_code=200)
        payload = envelope.get("payload")
        call_control_id = str(
            (payload or {}).get("call_control_id")
            if isinstance(payload, dict)
            else envelope.get("call_control_id")
            or ""
        ).strip()
        if not call_control_id:
            raise HTTPException(
                status_code=400, detail="Telnyx call.initiated is missing call_control_id"
            )
        try:
            stream_token = stream_tokens.issue(
                idempotency_key=telnyx_webhook_idempotency_key(envelope),
                claims={"CallSid": call_control_id},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        client = TelnyxCallControlClient(telnyx_api_key)
        try:
            await client.answer(
                call_control_id,
                build_answer_payload(
                    stream_url=f"{stream_url}/?{STREAM_TOKEN_PARAMETER}={stream_token}",
                    client_state={"call_control_id": call_control_id},
                ),
            )
        finally:
            await client.close()
        return Response(status_code=200)

    return app
