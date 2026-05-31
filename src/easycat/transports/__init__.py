"""Transport implementations for EasyCat.

Provides LocalTransport (mic/speaker), WebSocketTransport, TwilioTransport,
WebRTCTransport, and WebTransportTransport.

Exports load lazily via PEP 562 so importing a single transport submodule
(e.g. ``from easycat.transports.local import LocalTransportConfig``) does not
drag in every other transport — keeping ``EasyConfig.mic(...)`` cold starts
cheap for local-mic developers who never touch Twilio/WebRTC/WebTransport.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

_LAZY_ATTR: dict[str, str] = {
    "LocalTransport": "easycat.transports.local",
    "LocalTransportConfig": "easycat.transports.local",
    "TwilioTransport": "easycat.transports.twilio_media",
    "TwilioTransportConfig": "easycat.transports.twilio_media",
    "TwilioConnectionTransport": "easycat.transports.twilio_media",
    "ICEServer": "easycat.transports.webrtc",
    "WebRTCTransport": "easycat.transports.webrtc",
    "WebRTCTransportConfig": "easycat.transports.webrtc",
    "WebSocketTransport": "easycat.transports.websocket",
    "WebSocketTransportConfig": "easycat.transports.websocket",
    "WebSocketConnectionTransport": "easycat.transports.websocket",
    "WebTransportTransport": "easycat.transports.webtransport",
    "WebTransportTransportConfig": "easycat.transports.webtransport",
    "WebTransportConnectionTransport": "easycat.transports.webtransport",
    "WebTransportServer": "easycat.transports.webtransport",
}

__all__ = sorted(_LAZY_ATTR)


if TYPE_CHECKING:
    from easycat.transports.local import LocalTransport, LocalTransportConfig
    from easycat.transports.twilio_media import (
        TwilioConnectionTransport,
        TwilioTransport,
        TwilioTransportConfig,
    )
    from easycat.transports.webrtc import ICEServer, WebRTCTransport, WebRTCTransportConfig
    from easycat.transports.websocket import (
        WebSocketConnectionTransport,
        WebSocketTransport,
        WebSocketTransportConfig,
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
