"""Transport implementations for EasyCat.

Provides LocalTransport (mic/speaker), WebSocketTransport, TwilioTransport,
WebRTCTransport, and WebTransportTransport.

Also exports the building blocks for out-of-tree transports:
``AudioQueueMixin`` (inbound audio queue, ``receive_audio`` iterator, and
``TransportDegraded`` emission), ``ServerTransportBase`` (for transports that
host a WebSocket server), and the ``TransportDegraded`` event itself. See
``docs/extending/transport.md`` for the provider-author guide.

Exports load lazily via PEP 562 so importing a single transport submodule
(e.g. ``from easycat.transports.local import LocalTransportConfig``) does not
drag in every other transport — keeping ``EasyConfig.mic(...)`` cold starts
cheap for local-mic developers who never touch Twilio/WebRTC/WebTransport.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

_LAZY_ATTR: dict[str, str] = {
    "AudioQueueMixin": "easycat.transports._base",
    "ServerTransportBase": "easycat.transports._base",
    "TransportDegraded": "easycat.events",
    "LocalTransport": "easycat.transports.local",
    "LocalTransportConfig": "easycat.transports.local",
    "TelnyxTransport": "easycat.transports.telnyx_media",
    "TelnyxTransportConfig": "easycat.transports.telnyx_media",
    "TelnyxConnectionTransport": "easycat.transports.telnyx_media",
    "TwilioTransport": "easycat.transports.twilio_media",
    "TwilioTransportConfig": "easycat.transports.twilio_media",
    "TwilioConnectionTransport": "easycat.transports.twilio_media",
    "StreamTokenContext": "easycat.transports.twilio_media",
    "TwilioStreamTokenStore": "easycat.transports.twilio_media",
    "twilio_websocket_signature_process_request": "easycat.transports.twilio_media",
    "TWILIO_STREAM_TOKEN_PARAMETER": "easycat.transports.twilio_media",
    "ICEServer": "easycat.transports._webrtc_config",
    "WebRTCTransport": "easycat.transports.webrtc",
    "WebRTCTransportConfig": "easycat.transports._webrtc_config",
    "webrtc_ice_servers_from_env": "easycat.transports._webrtc_config",
    "webrtc_transport_config_from_env": "easycat.transports._webrtc_config",
    "WebSocketTransport": "easycat.transports.websocket",
    "WebSocketTransportConfig": "easycat.transports.websocket",
    "WebSocketConnectionTransport": "easycat.transports.websocket",
    "WebSocketSessionServerConfig": "easycat.transports.websocket",
    "websocket_session_server_config_from_env": "easycat.transports.websocket",
    "WebTransportTransport": "easycat.transports.webtransport",
    "WebTransportTransportConfig": "easycat.transports.webtransport",
    "WebTransportConnectionTransport": "easycat.transports.webtransport",
    "WebTransportServer": "easycat.transports.webtransport",
}

__all__ = sorted(_LAZY_ATTR)  # noqa: PLE0605 exports are generated from the public registry


if TYPE_CHECKING:
    from easycat.events import TransportDegraded
    from easycat.transports._base import AudioQueueMixin, ServerTransportBase
    from easycat.transports._webrtc_config import (
        ICEServer,
        WebRTCTransportConfig,
        webrtc_ice_servers_from_env,
        webrtc_transport_config_from_env,
    )
    from easycat.transports.local import LocalTransport, LocalTransportConfig
    from easycat.transports.telnyx_media import (
        TelnyxConnectionTransport,
        TelnyxTransport,
        TelnyxTransportConfig,
    )
    from easycat.transports.twilio_media import (
        TWILIO_STREAM_TOKEN_PARAMETER,
        StreamTokenContext,
        TwilioConnectionTransport,
        TwilioStreamTokenStore,
        TwilioTransport,
        TwilioTransportConfig,
        twilio_websocket_signature_process_request,
    )
    from easycat.transports.webrtc import WebRTCTransport
    from easycat.transports.websocket import (
        WebSocketConnectionTransport,
        WebSocketSessionServerConfig,
        WebSocketTransport,
        WebSocketTransportConfig,
        websocket_session_server_config_from_env,
    )
    from easycat.transports.webtransport import (
        WebTransportConnectionTransport,
        WebTransportServer,
        WebTransportTransport,
        WebTransportTransportConfig,
    )


def __getattr__(name: str):  # PEP 562
    """Lazy re-export dispatcher. Runs once per attribute per process."""
    try:
        module_path = _LAZY_ATTR[name]
    except KeyError:
        raise AttributeError(f"module 'easycat.transports' has no attribute {name!r}") from None
    module = importlib.import_module(module_path)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(list(globals()) + list(_LAZY_ATTR)))
