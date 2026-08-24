"""Reusable two-listener Telnyx Call Control voice server.

This mirrors :mod:`easycat.telephony.server` (the Twilio Media Streams voice
server) for Telnyx. It runs two listeners:

* an :mod:`aiohttp.web` HTTP listener on ``http_port`` serving the ``POST
  /telnyx`` webhook route. Ed25519-verified ``call.initiated`` deliveries mint
  a one-time stream token, then answer the call via Call Control with the
  token embedded in the ``stream_url`` query string,
* a raw :func:`websockets.serve` listener on ``media_port`` that accepts
  Telnyx media-stream connections, consumes the one-time handshake token via
  ``wait_for_start()``, and builds one EasyCat session per call.

Token orchestration lives **above** the transport here — the transport class
never owns the app server. Unlike Twilio, Telnyx does not sign the media
WebSocket handshake, so the only handshake credential is the one-time token
minted by the webhook listener; per-connection telephony behavior still flows
through the ``config_factory`` via :class:`~easycat.config.TelephonyConfig`,
NOT through :class:`TelnyxVoiceServerConfig`.

These symbols are internal-module imports (like
``run_websocket_config_server``): import them from
``easycat.telephony.telnyx_server`` directly — they are intentionally NOT
top-level ``easycat`` exports.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import websockets

from easycat._concurrency import RuntimeSupervisor
from easycat._extras import require_module
from easycat._signals import create_shutdown_event
from easycat.server.transports import WebSocketSessionRuntime
from easycat.teardown_budgets import (
    SERVER_DRAIN_TIMEOUT_S,
    SERVER_FORCE_SHUTDOWN_TIMEOUT_S,
)
from easycat.telephony._stream_tokens import STREAM_TOKEN_PARAMETER, StreamTokenStore
from easycat.telephony.telnyx import (
    TELNYX_WEBHOOK_SIGNATURE_HEADER,
    TELNYX_WEBHOOK_TIMESTAMP_HEADER,
)
from easycat.transports._limits import MAX_WEBSOCKET_MESSAGE_BYTES

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

    from easycat.config import EasyConfig
    from easycat.session import Session

logger = logging.getLogger(__name__)

__all__ = ["TelnyxVoiceServerConfig", "run_telnyx_voice_app", "serve_telnyx_voice_app"]


def _issue_call_bound_stream_token(
    store: StreamTokenStore,
    call_control_id: str,
    *,
    idempotency_key: str | None,
) -> str:
    return store.issue(
        idempotency_key=idempotency_key,
        claims={"CallSid": call_control_id},
    )


async def _handle_telnyx_webhook_request(
    request: Any,
    *,
    web: Any,
    config: TelnyxVoiceServerConfig,
    stream_tokens: Any,
) -> Any:
    from easycat.telephony.telnyx import (
        parse_telnyx_webhook,
        telnyx_webhook_idempotency_key,
        verify_telnyx_webhook_signature,
    )

    body = await request.read()
    if config.telnyx_public_key and not verify_telnyx_webhook_signature(
        payload=body,
        signature=request.headers.get(TELNYX_WEBHOOK_SIGNATURE_HEADER),
        timestamp=request.headers.get(TELNYX_WEBHOOK_TIMESTAMP_HEADER),
        public_key=config.telnyx_public_key,
    ):
        return web.Response(status=403, text="Telnyx signature validation failed")

    envelope = parse_telnyx_webhook(body)
    if envelope is None:
        return web.Response(status=400, text="Invalid Telnyx webhook envelope")
    if envelope.get("event_type") != "call.initiated":
        # Non-lifecycle deliveries (AMD results, streaming failures answered by
        # the session pipeline) need no server-side command; ack them so Telnyx
        # does not retry.
        return web.Response(status=200)

    raw_payload = envelope.get("payload")
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    call_control_id = str(
        payload.get("call_control_id") or envelope.get("call_control_id") or ""
    ).strip()
    if not call_control_id:
        return web.Response(status=400, text="Telnyx call.initiated missing call_control_id")

    assert config.stream_url is not None
    grant_key = telnyx_webhook_idempotency_key(envelope)
    try:
        stream_token = _issue_call_bound_stream_token(
            stream_tokens,
            call_control_id,
            idempotency_key=grant_key,
        )
    except ValueError as exc:
        return web.Response(status=400, text=str(exc))

    from easycat.telephony.telnyx import build_answer_payload
    from easycat.telephony.telnyx_client import TelnyxApiError, TelnyxCallControlClient

    answer_payload = build_answer_payload(
        stream_url=f"{config.stream_url}/?{STREAM_TOKEN_PARAMETER}={stream_token}",
        client_state={"call_control_id": call_control_id},
    )
    assert config.telnyx_api_key is not None
    client = TelnyxCallControlClient(config.telnyx_api_key)
    try:
        await client.answer(call_control_id, answer_payload)
    except TelnyxApiError as exc:
        logger.warning("Telnyx answer command failed for %s: %s", call_control_id, exc)
        return web.Response(status=502, text=f"Telnyx answer failed: {exc.status}")
    except Exception:
        logger.warning("Telnyx answer command raised", exc_info=True)
        return web.Response(status=502, text="Telnyx answer failed")
    finally:
        await client.close()
    return web.Response(status=200)


@dataclass(frozen=True)
class TelnyxVoiceServerConfig:
    """Settings for :func:`serve_telnyx_voice_app`.

    ``enable_dtmf_aggregator`` / ``enable_voicemail_detector`` are deliberately
    absent: they live on :class:`~easycat.config.TelephonyConfig` (default
    ``False``) and are opted in per-connection through the ``config_factory``.

    ``telnyx_api_key`` is the Call Control Bearer token used to answer inbound
    calls (distinct from the browser/websocket ``serve_token`` that gates the
    signaling bind). Because the webhook listener defaults to a public bind
    (``http_host="0.0.0.0"``) and every accepted ``call.initiated`` mints a
    media stream token, the webhook is authenticated by default:
    ``telnyx_public_key`` (the portal's Ed25519 public key) verifies each
    delivery, and serving without it raises unless
    ``unsafe_allow_unsigned_webhooks=True`` opts into an unauthenticated
    endpoint. Unlike Twilio there is no URL reconstruction step — Ed25519
    signs ``{timestamp}|{raw_body}`` only — so no trust-proxy option exists.

    The media WebSocket handshake carries NO Telnyx signature; its only
    credential is the one-time stream token embedded in the answer command's
    ``stream_url``, validated (and consumed exactly once) by the transport's
    ``start`` preflight. ``max_sessions`` caps concurrent media sockets and
    sessions, including idle pre-start sockets. Invalid sockets never build an
    EasyCat session, and ``start_timeout_s`` bounds how long a socket may hold
    a slot without presenting a token-valid ``start`` frame.
    """

    host: str = "0.0.0.0"
    media_port: int = 8766
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    webhook_path: str = "/telnyx"
    stream_url: str | None = None
    stream_token_secret: str | None = field(default=None, repr=False)
    telnyx_api_key: str | None = field(default=None, repr=False)
    telnyx_public_key: str | None = None
    unsafe_allow_unsigned_webhooks: bool = False
    max_sessions: int = 64
    start_timeout_s: float = 10.0
    drain_timeout_s: float = SERVER_DRAIN_TIMEOUT_S
    force_shutdown_timeout_s: float = SERVER_FORCE_SHUTDOWN_TIMEOUT_S


async def _start_webhook_http_listener(
    web: Any,
    config: TelnyxVoiceServerConfig,
    handle_webhook: Callable[[Any], Any],
    media_server: Any,
) -> tuple[Any, Any]:
    """Start the aiohttp webhook listener, returning ``(runner, site)``.

    The media WebSocket listener is already bound by the time this runs. If HTTP
    listener setup fails (for example because ``http_port`` is already in use),
    close the media listener and tear down the partially set-up runner before
    re-raising, so a startup failure does not leak the bound media port.
    """
    app = web.Application()
    app.router.add_post(config.webhook_path, handle_webhook)
    runner = web.AppRunner(app)
    started = False
    try:
        await runner.setup()
        site = web.TCPSite(runner, config.http_host, config.http_port)
        await site.start()
        started = True
    finally:
        if not started:
            # HTTP setup can fail after the raw media listener was bound. Run
            # every independent rollback stage even if one listener operation
            # fails, while preserving the original startup exception (or task
            # cancellation) as the outcome.
            try:
                media_server.close()
            except Exception:
                logger.warning(
                    "Telnyx media listener close failed during HTTP startup rollback",
                    exc_info=True,
                )
            try:
                await runner.cleanup()
            except Exception:
                logger.warning(
                    "Telnyx HTTP runner cleanup failed during startup rollback",
                    exc_info=True,
                )
            finally:
                # Cancellation while runner cleanup is suspended must not skip
                # ownership of the already-closed raw media listener.
                try:
                    await media_server.wait_closed()
                except Exception:
                    logger.warning(
                        "Telnyx media listener wait failed during HTTP startup rollback",
                        exc_info=True,
                    )
    return runner, site


async def _shutdown_telnyx_voice_app(
    *,
    runtime: Any,
    media_server: Any,
    site: Any,
    runner: Any,
    config: TelnyxVoiceServerConfig,
) -> None:
    """Drain media sessions even when the webhook listener cleanup fails."""
    listener_error: Exception | None = None
    body_error: BaseException | None = None

    def record_listener_error(stage: str, exc: Exception) -> None:
        nonlocal listener_error
        if listener_error is None:
            listener_error = exc
        else:
            logger.warning("Telnyx %s also failed", stage)

    try:
        # Set the drain fence before stopping the HTTP listener so accepted
        # media connections cannot turn into new sessions during shutdown. A
        # listener implementation can still reject ``close()``; that must not
        # skip the independent HTTP cleanup or the runtime's session drain.
        try:
            runtime.start_draining(media_server)
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            record_listener_error("media listener close", exc)
        try:
            await site.stop()
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            record_listener_error("HTTP site stop", exc)
    except BaseException as exc:
        # Cleanup below still owns the runner and live media sessions, but a
        # cancellation (or another non-ordinary exception) from the shutdown
        # body must remain the caller-visible outcome.
        body_error = exc
        raise
    finally:
        # Nest the remaining stages so a cancellation during either listener
        # await still runs the session drain. ``CancelledError`` deliberately
        # is not caught: once cleanup has been given its chance, it propagates
        # to the caller with ordinary cancellation semantics.
        try:
            try:
                await runner.cleanup()
            except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
                record_listener_error("HTTP runner cleanup", exc)
        finally:
            try:
                await runtime.drain(
                    media_server,
                    drain_timeout_s=config.drain_timeout_s,
                    force_timeout_s=config.force_shutdown_timeout_s,
                )
            except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
                record_listener_error("media runtime drain", exc)
        if body_error is None and listener_error is not None:
            raise listener_error


async def serve_telnyx_voice_app(
    config_factory: Callable[[Any], EasyConfig],
    config: TelnyxVoiceServerConfig,
) -> None:
    """Serve a Telnyx voice app: raw media WebSocket + aiohttp webhook route.

    ``config_factory`` receives a per-call
    :class:`~easycat.transports.TelnyxConnectionTransport` and returns the
    :class:`~easycat.config.EasyConfig` passed to
    :func:`easycat.create_session`. This helper owns both listeners' lifecycle
    and tears every session down on shutdown.

    ``config.stream_url`` is required — the answer command cannot be built
    without the ``wss://`` media URL Telnyx connects back to.

    The ``POST {webhook_path}`` endpoint is authenticated by default: every
    accepted ``call.initiated`` mints a media stream token and issues a
    billable answer command, so ``config`` must carry ``telnyx_public_key``
    (Ed25519 delivery verification) unless ``unsafe_allow_unsigned_webhooks=``
    ``True``, and must always carry ``telnyx_api_key`` to answer the call.
    """
    if not config.stream_url:
        raise ValueError(
            "TelnyxVoiceServerConfig.stream_url is required: the answer command needs "
            "the wss:// media URL Telnyx connects back to. Set stream_url (or the "
            "TELNYX_STREAM_URL env var when running through VoiceApp)."
        )
    if not math.isfinite(config.start_timeout_s) or config.start_timeout_s <= 0:
        raise ValueError("TelnyxVoiceServerConfig.start_timeout_s must be positive")
    if not config.telnyx_api_key:
        raise ValueError(
            "TelnyxVoiceServerConfig.telnyx_api_key is required so POST "
            f"{config.webhook_path} can answer verified call.initiated deliveries via "
            "Call Control. Set telnyx_api_key (or the TELNYX_API_KEY env var when "
            "running through VoiceApp)."
        )

    # aiohttp is the only thing that makes telnyx mode need the telnyx extra;
    # websockets and TelnyxConnectionTransport import at base. Gate it here so a
    # missing extra surfaces as a clear, actionable error.
    web = require_module("aiohttp.web", extra="telnyx", purpose="VoiceApp telnyx mode")

    if not config.telnyx_public_key and not config.unsafe_allow_unsigned_webhooks:
        raise ValueError(
            "TelnyxVoiceServerConfig.telnyx_public_key is required so POST "
            f"{config.webhook_path} can verify Ed25519 signatures before minting a "
            "media stream token. Set telnyx_public_key (or the TELNYX_PUBLIC_KEY env "
            "var when running through VoiceApp), or pass "
            "unsafe_allow_unsigned_webhooks=True to accept unauthenticated webhooks."
        )

    from easycat.config import create_session
    from easycat.session_manager import SessionManager
    from easycat.transports.telnyx_media import TelnyxConnectionTransport
    from easycat.transports.telnyx_media import TelnyxTransportConfig as _TelnyxTransportConfig

    manager: SessionManager[int] = SessionManager()
    stream_tokens = StreamTokenStore(config.stream_token_secret)

    async def build_session(ws: ServerConnection) -> Session | None:
        transport = TelnyxConnectionTransport(
            ws,
            config=_TelnyxTransportConfig(stream_token_validator=stream_tokens.consume_start),
        )
        if not await transport.wait_for_start(timeout_s=config.start_timeout_s):
            return None
        try:
            return create_session(config_factory(transport))
        except BaseException:
            await transport.disconnect()
            raise

    runtime: WebSocketSessionRuntime[ServerConnection, Session] = WebSocketSessionRuntime(
        manager=manager,
        max_sessions=config.max_sessions,
        runtime_supervisor=RuntimeSupervisor(capacity=1),
        runtime_id="telnyx-media-server",
        session_factory=build_session,
        runtime_feedback=True,
    )

    async def handle_webhook(request: Any) -> Any:
        return await _handle_telnyx_webhook_request(
            request,
            web=web,
            config=config,
            stream_tokens=stream_tokens,
        )

    media_server = await websockets.serve(
        runtime.handle,
        config.host,
        config.media_port,
        compression=None,
        max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
    )

    # Start the webhook HTTP listener; the helper closes the already-bound media
    # listener if its own setup fails (e.g. http_port in use) so the port is not
    # leaked before the steady-state try/finally below takes over teardown.
    runner, site = await _start_webhook_http_listener(web, config, handle_webhook, media_server)
    logger.info(
        "Telnyx voice server ready: media ws://%s:%s, webhook http://%s:%s%s",
        config.host,
        config.media_port,
        config.http_host,
        config.http_port,
        config.webhook_path,
    )

    event = create_shutdown_event()
    try:
        await event.wait()
    finally:
        await _shutdown_telnyx_voice_app(
            runtime=runtime,
            media_server=media_server,
            site=site,
            runner=runner,
            config=config,
        )


def run_telnyx_voice_app(
    config_factory: Callable[[Any], EasyConfig],
    config: TelnyxVoiceServerConfig,
) -> None:
    """Run a Telnyx voice app from a synchronous entry point.

    Thin ``asyncio.run`` wrapper around :func:`serve_telnyx_voice_app`,
    mirroring :func:`~easycat.telephony.server.run_twilio_voice_app` and
    :func:`~easycat.server.websocket.run_websocket_config_server` so the
    loop ownership lives next to the async server rather than in the caller.
    """
    asyncio.run(serve_telnyx_voice_app(config_factory, config))
