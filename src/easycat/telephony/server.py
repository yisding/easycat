"""Reusable two-listener Twilio Media Streams voice server.

This extracts the server shape from ``examples/twilio_app.py`` and the
``twilio-phone`` scaffold template into a reusable helper so ``VoiceApp`` can
delegate to it for ``run("twilio")`` / ``serve("twilio")``. Like those, it runs
two listeners:

* a raw :func:`websockets.serve` listener on ``media_port`` that accepts Twilio
  Media Streams connections and builds one EasyCat session per call, and
* an :mod:`aiohttp.web` HTTP listener on ``http_port`` that serves the ``POST
  /twiml`` route, returning ``<Connect><Stream>`` TwiML pointed at the media
  listener.

TwiML/token orchestration lives **above** the transport here — the transport
class never owns the app server. Per-connection telephony behavior
(``enable_dtmf_aggregator`` / ``enable_voicemail_detector``) is threaded through
the ``config_factory`` via :class:`~easycat.config.TelephonyConfig`, NOT through
:class:`TwilioVoiceServerConfig`; putting those flags on the server config would
duplicate the :class:`TelephonyConfig` fields and invert their ``False``
default.

These symbols are internal-module imports (like
``run_websocket_config_server``): import them from
``easycat.telephony.server`` directly — they are intentionally NOT top-level
``easycat`` exports.
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
from easycat.telephony._stream_tokens import StreamTokenStore
from easycat.transports._limits import MAX_WEBSOCKET_MESSAGE_BYTES

if TYPE_CHECKING:
    from websockets.asyncio.server import ServerConnection

    from easycat.config import EasyConfig
    from easycat.session import Session

logger = logging.getLogger(__name__)

__all__ = ["TwilioVoiceServerConfig", "run_twilio_voice_app", "serve_twilio_voice_app"]


def _issue_call_bound_stream_token(
    store: StreamTokenStore,
    form_items: list[tuple[str, str]],
    *,
    idempotency_key: str | None,
) -> str:
    from easycat.telephony import twilio_stream_parameters_from_form

    call_sid = dict(form_items).get("CallSid", "").strip()
    if not call_sid:
        raise ValueError("Twilio webhook is missing CallSid")
    parameters = twilio_stream_parameters_from_form(form_items)
    return store.issue(
        idempotency_key=idempotency_key,
        claims={"CallSid": call_sid, **parameters},
    )


async def _handle_twiml_request(
    request: Any,
    *,
    web: Any,
    config: TwilioVoiceServerConfig,
    stream_tokens: Any,
) -> Any:
    from easycat.telephony import (
        twilio_stream_parameters_from_form,
        twilio_webhook_idempotency_key,
    )
    from easycat.telephony.twiml import (
        reconstruct_public_url,
        validate_twilio_webhook_signature,
    )
    from easycat.transports.twilio_media import twiml_connect_stream

    post = await request.post()
    form_items = list(post.items())
    public_url = config.public_twiml_url or reconstruct_public_url(
        request.headers,
        getattr(request, "raw_path", request.path_qs),
        trust_proxy=config.trust_proxy_headers,
        default_scheme=request.scheme,
    )
    signature = request.headers.get("X-Twilio-Signature")
    if config.twilio_auth_token and not validate_twilio_webhook_signature(
        auth_token=config.twilio_auth_token,
        url=public_url,
        params=form_items,
        signature=signature,
    ):
        return web.Response(status=403, text="Twilio signature validation failed")

    assert config.stream_url is not None
    grant_key = twilio_webhook_idempotency_key(
        url=public_url,
        params=form_items,
        signature=signature,
    )
    try:
        stream_token = _issue_call_bound_stream_token(
            stream_tokens,
            form_items,
            idempotency_key=grant_key,
        )
    except ValueError as exc:
        return web.Response(status=400, text=str(exc))
    xml = twiml_connect_stream(
        config.stream_url,
        parameters=twilio_stream_parameters_from_form(form_items),
        stream_token=stream_token,
    )
    return web.Response(text=xml, content_type="application/xml")


@dataclass(frozen=True)
class TwilioVoiceServerConfig:
    """Settings for :func:`serve_twilio_voice_app`.

    ``enable_dtmf_aggregator`` / ``enable_voicemail_detector`` are deliberately
    absent: they live on :class:`~easycat.config.TelephonyConfig` (default
    ``False``) and are opted in per-connection through the ``config_factory``.

    ``twilio_auth_token`` is the Twilio account auth token used to validate the
    ``X-Twilio-Signature`` header on inbound ``POST /twiml`` webhooks (distinct
    from the browser/websocket ``serve_token`` that gates the signaling bind).
    Because the TwiML listener defaults to a public bind
    (``http_host="0.0.0.0"``) and every accepted request mints a media stream
    token, the webhook is authenticated by default: serving without
    ``twilio_auth_token`` raises unless ``unsafe_allow_unsigned_webhooks=True``
    opts into an unauthenticated endpoint. Set ``trust_proxy_headers=True`` when
    running behind a
    TLS-terminating proxy/load balancer so the public URL Twilio signed is
    reconstructed from ``X-Forwarded-Proto`` / ``X-Forwarded-Host``.

    The media listener validates Twilio's ``X-Twilio-Signature`` during the
    WebSocket handshake when an auth token is configured. The one-time token in
    the first ``start`` frame remains an independent replay and claims check.
    ``max_sessions`` caps concurrent media sockets and sessions, including idle
    pre-start sockets. Invalid sockets never build an EasyCat session, and
    ``start_timeout_s`` bounds how long a socket may hold a slot without
    presenting an authenticated Twilio ``start`` frame.
    """

    host: str = "0.0.0.0"
    media_port: int = 8766
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    stream_url: str | None = None
    stream_token_secret: str | None = field(default=None, repr=False)
    twilio_auth_token: str | None = field(default=None, repr=False)
    trust_proxy_headers: bool = False
    unsafe_allow_unsigned_webhooks: bool = False
    max_sessions: int = 64
    start_timeout_s: float = 10.0
    public_twiml_url: str | None = None
    drain_timeout_s: float = SERVER_DRAIN_TIMEOUT_S
    force_shutdown_timeout_s: float = SERVER_FORCE_SHUTDOWN_TIMEOUT_S


async def _start_twiml_http_listener(
    web: Any,
    config: TwilioVoiceServerConfig,
    handle_twiml: Callable[[Any], Any],
    media_server: Any,
) -> tuple[Any, Any]:
    """Start the aiohttp ``POST /twiml`` listener, returning ``(runner, site)``.

    The media WebSocket listener is already bound by the time this runs. If HTTP
    listener setup fails (for example because ``http_port`` is already in use),
    close the media listener and tear down the partially set-up runner before
    re-raising, so a startup failure does not leak the bound media port.
    """
    app = web.Application()
    app.router.add_post("/twiml", handle_twiml)
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
                    "Twilio media listener close failed during HTTP startup rollback",
                    exc_info=True,
                )
            try:
                await runner.cleanup()
            except Exception:
                logger.warning(
                    "Twilio HTTP runner cleanup failed during startup rollback",
                    exc_info=True,
                )
            finally:
                # Cancellation while runner cleanup is suspended must not skip
                # ownership of the already-closed raw media listener.
                try:
                    await media_server.wait_closed()
                except Exception:
                    logger.warning(
                        "Twilio media listener wait failed during HTTP startup rollback",
                        exc_info=True,
                    )
    return runner, site


async def _shutdown_twilio_voice_app(
    *,
    runtime: Any,
    media_server: Any,
    site: Any,
    runner: Any,
    config: TwilioVoiceServerConfig,
) -> None:
    """Drain media sessions even when the TwiML listener cleanup fails."""
    listener_error: Exception | None = None
    body_error: BaseException | None = None

    def record_listener_error(stage: str, exc: Exception) -> None:
        nonlocal listener_error
        if listener_error is None:
            listener_error = exc
        else:
            logger.warning("Twilio %s also failed", stage)

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


async def serve_twilio_voice_app(
    config_factory: Callable[[Any], EasyConfig],
    config: TwilioVoiceServerConfig,
) -> None:
    """Serve a Twilio voice app: raw media WebSocket + aiohttp TwiML HTTP route.

    ``config_factory`` receives a per-call
    :class:`~easycat.transports.TwilioConnectionTransport` and returns the
    :class:`~easycat.config.EasyConfig` passed to
    :func:`easycat.create_session`. This helper owns both listeners' lifecycle
    and tears every session down on shutdown.

    ``config.stream_url`` is required — TwiML cannot be built without the
    ``wss://`` media URL Twilio should dial back into.

    The ``POST /twiml`` webhook is authenticated by default: every accepted
    request mints a media stream token, so an unauthenticated public listener
    would let anyone obtain a token the media WebSocket accepts. ``config``
    must therefore carry a ``twilio_auth_token`` (validated against the
    ``X-Twilio-Signature`` header) unless ``unsafe_allow_unsigned_webhooks=True``.
    """
    if not config.stream_url:
        raise ValueError(
            "TwilioVoiceServerConfig.stream_url is required: TwiML needs the "
            "wss:// media URL Twilio connects back to. Set stream_url (or the "
            "TWILIO_STREAM_URL env var when running through VoiceApp)."
        )
    if not math.isfinite(config.start_timeout_s) or config.start_timeout_s <= 0:
        raise ValueError("TwilioVoiceServerConfig.start_timeout_s must be positive")

    # aiohttp is the only thing that makes twilio mode need the telephony extra;
    # websockets and TwilioConnectionTransport import at base. Gate it here so a
    # missing extra surfaces as a clear, actionable error.
    web = require_module("aiohttp.web", extra="telephony", purpose="VoiceApp twilio mode")

    if not config.twilio_auth_token and not config.unsafe_allow_unsigned_webhooks:
        raise ValueError(
            "TwilioVoiceServerConfig.twilio_auth_token is required so POST /twiml "
            "can validate the X-Twilio-Signature header before minting a media "
            "stream token. Set twilio_auth_token (or the TWILIO_AUTH_TOKEN env var "
            "when running through VoiceApp), or pass unsafe_allow_unsigned_webhooks="
            "True to accept unauthenticated webhooks."
        )

    from easycat.config import create_session
    from easycat.session_manager import SessionManager
    from easycat.transports import (
        TwilioStreamTokenStore,
        TwilioTransportConfig,
        twilio_websocket_signature_process_request,
    )
    from easycat.transports.twilio_media import TwilioConnectionTransport

    manager: SessionManager[int] = SessionManager()
    stream_tokens = TwilioStreamTokenStore(config.stream_token_secret)

    async def build_session(ws: ServerConnection) -> Session | None:
        transport = TwilioConnectionTransport(
            ws,
            config=TwilioTransportConfig(stream_token_validator=stream_tokens.consume_start),
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
        runtime_id="twilio-media-server",
        session_factory=build_session,
        runtime_feedback=True,
    )

    async def handle_twiml(request: Any) -> Any:
        return await _handle_twiml_request(
            request,
            web=web,
            config=config,
            stream_tokens=stream_tokens,
        )

    process_request = (
        twilio_websocket_signature_process_request(
            config.twilio_auth_token,
            config.stream_url,
        )
        if config.twilio_auth_token
        else None
    )
    media_server = await websockets.serve(
        runtime.handle,
        config.host,
        config.media_port,
        process_request=process_request,
        compression=None,
        max_size=MAX_WEBSOCKET_MESSAGE_BYTES,
    )

    # Start the TwiML HTTP listener; the helper closes the already-bound media
    # listener if its own setup fails (e.g. http_port in use) so the port is not
    # leaked before the steady-state try/finally below takes over teardown.
    runner, site = await _start_twiml_http_listener(web, config, handle_twiml, media_server)
    logger.info(
        "Twilio voice server ready: media ws://%s:%s, TwiML http://%s:%s/twiml",
        config.host,
        config.media_port,
        config.http_host,
        config.http_port,
    )

    event = create_shutdown_event()
    try:
        await event.wait()
    finally:
        await _shutdown_twilio_voice_app(
            runtime=runtime,
            media_server=media_server,
            site=site,
            runner=runner,
            config=config,
        )


def run_twilio_voice_app(
    config_factory: Callable[[Any], EasyConfig],
    config: TwilioVoiceServerConfig,
) -> None:
    """Run a Twilio voice app from a synchronous entry point.

    Thin ``asyncio.run`` wrapper around :func:`serve_twilio_voice_app`,
    mirroring :func:`~easycat.server.webrtc_routes.run_webrtc_config_server` and
    :func:`~easycat.server.websocket.run_websocket_config_server` so the
    loop ownership lives next to the async server rather than in the caller.
    """
    asyncio.run(serve_twilio_voice_app(config_factory, config))
