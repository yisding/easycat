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
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import websockets
from websockets.asyncio.server import ServerConnection

from easycat._extras import require_module
from easycat._signals import create_shutdown_event
from easycat.transports._limits import MAX_WEBSOCKET_MESSAGE_BYTES

if TYPE_CHECKING:
    from easycat.config import EasyConfig

logger = logging.getLogger(__name__)

__all__ = ["TwilioVoiceServerConfig", "run_twilio_voice_app", "serve_twilio_voice_app"]


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

    ``max_sessions`` caps concurrent media sessions. The media listener defaults
    to a public bind (``host="0.0.0.0"``) and builds a full EasyCat session per
    accepted WebSocket *before* the first ``start`` frame's one-time stream
    token is validated, so an unauthenticated client could otherwise open
    idle/invalid sockets and exhaust provider connections or block real calls.
    The gate bounds that blast radius (mirroring the WebRTC/WebSocket session
    servers); over-limit connections are rejected with WebSocket close code
    ``1013`` (Try Again Later).
    """

    host: str = "0.0.0.0"
    media_port: int = 8766
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    stream_url: str | None = None
    stream_token_secret: str | None = None
    twilio_auth_token: str | None = None
    trust_proxy_headers: bool = False
    unsafe_allow_unsigned_webhooks: bool = False
    max_sessions: int = 64


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
    try:
        await runner.setup()
        site = web.TCPSite(runner, config.http_host, config.http_port)
        await site.start()
    except BaseException:
        media_server.close()
        await media_server.wait_closed()
        await runner.cleanup()
        raise
    return runner, site


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
    from easycat.telephony import twilio_stream_parameters_from_form
    from easycat.telephony.twiml import (
        reconstruct_public_url,
        validate_twilio_webhook_signature,
    )
    from easycat.transports import TwilioStreamTokenStore, TwilioTransportConfig
    from easycat.transports.twilio_media import (
        TwilioConnectionTransport,
        twiml_connect_stream,
    )

    manager: SessionManager[int] = SessionManager()
    stream_tokens = TwilioStreamTokenStore(config.stream_token_secret)
    session_slots = asyncio.Semaphore(config.max_sessions)

    async def handle_twilio_connection(ws: ServerConnection) -> None:
        # Gate before building/starting a session: this handler spins up a full
        # EasyCat session (provider connections included) for every accepted
        # WebSocket, and the one-time stream token is only validated once the
        # first ``start`` frame arrives inside the transport. Cap concurrency so
        # an unauthenticated client cannot exhaust provider connections or block
        # real calls by opening idle/invalid sockets.
        if session_slots.locked():
            await ws.close(code=1013, reason="Server is at the configured session limit")
            return
        async with session_slots:
            transport = TwilioConnectionTransport(
                ws,
                config=TwilioTransportConfig(stream_token_validator=stream_tokens.consume),
            )
            session = create_session(config_factory(transport))
            async with manager.connection(id(ws), session, runtime_feedback=True):
                await ws.wait_closed()

    async def handle_twiml(request: Any) -> Any:
        post = await request.post()
        form_items = list(post.items())
        # Authenticate the webhook before minting a stream token. Skipped only
        # when the operator explicitly opted into unsigned webhooks above.
        if config.twilio_auth_token:
            public_url = reconstruct_public_url(
                request.headers,
                request.path_qs,
                trust_proxy=config.trust_proxy_headers,
                default_scheme=request.scheme,
            )
            if not validate_twilio_webhook_signature(
                auth_token=config.twilio_auth_token,
                url=public_url,
                params=form_items,
                signature=request.headers.get("X-Twilio-Signature"),
            ):
                return web.Response(status=403, text="Twilio signature validation failed")
        assert config.stream_url is not None
        xml = twiml_connect_stream(
            config.stream_url,
            parameters=twilio_stream_parameters_from_form(form_items),
            stream_token=stream_tokens.issue(),
        )
        return web.Response(text=xml, content_type="application/xml")

    media_server = await websockets.serve(
        handle_twilio_connection,
        config.host,
        config.media_port,
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
        media_server.close()
        await media_server.wait_closed()
        await site.stop()
        await runner.cleanup()
        await manager.stop_all()


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
