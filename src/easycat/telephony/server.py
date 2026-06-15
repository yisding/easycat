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

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import websockets
from websockets.asyncio.server import ServerConnection

from easycat._extras import require_module
from easycat._signals import create_shutdown_event

if TYPE_CHECKING:
    from easycat.config import EasyConfig

logger = logging.getLogger(__name__)

__all__ = ["TwilioVoiceServerConfig", "serve_twilio_voice_app"]


@dataclass(frozen=True)
class TwilioVoiceServerConfig:
    """Settings for :func:`serve_twilio_voice_app`.

    ``enable_dtmf_aggregator`` / ``enable_voicemail_detector`` are deliberately
    absent: they live on :class:`~easycat.config.TelephonyConfig` (default
    ``False``) and are opted in per-connection through the ``config_factory``.
    """

    host: str = "0.0.0.0"
    media_port: int = 8766
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    stream_url: str | None = None
    stream_token_secret: str | None = None


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

    from easycat.config import create_session
    from easycat.session_manager import SessionManager
    from easycat.telephony import twilio_stream_parameters_from_form
    from easycat.transports import TwilioStreamTokenStore, TwilioTransportConfig
    from easycat.transports.twilio_media import (
        TwilioConnectionTransport,
        twiml_connect_stream,
    )

    manager: SessionManager[int] = SessionManager()
    stream_tokens = TwilioStreamTokenStore(config.stream_token_secret)

    async def handle_twilio_connection(ws: ServerConnection) -> None:
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
        xml = twiml_connect_stream(
            config.stream_url,
            parameters=twilio_stream_parameters_from_form(form_items),
            stream_token=stream_tokens.issue(),
        )
        return web.Response(text=xml, content_type="application/xml")

    media_server = await websockets.serve(handle_twilio_connection, config.host, config.media_port)

    app = web.Application()
    app.router.add_post("/twiml", handle_twiml)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.http_host, config.http_port)
    await site.start()
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
