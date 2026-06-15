"""``VoiceServerConfig`` — the process-policy owner for :class:`VoiceServer`.

Ownership rule (MF-4e): ``VoiceServerConfig`` owns ALL process policy
(host/port, capacity, draining timeouts, auth, CORS, metrics, health). A
mounted :class:`~easycat.VoiceApp` contributes ONLY its per-transport
``config_factory`` (how to build an ``EasyConfig``/``Session`` for a
connection). The server therefore does NOT inherit a single app's
transport-server defaults (e.g. ``WebSocketSessionServerConfig``'s divergent
``max_sessions=10`` / ``port=8765``) — ``VoiceServerConfig`` keeps ``64`` /
``8080``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # ``AuthPolicy`` lands in M5 (``easycat.server.auth``). M4 only stores the
    # field; the non-loopback guard that consumes it is M5. Typing it behind
    # ``TYPE_CHECKING`` keeps M4 free of a runtime dependency on M5.
    from easycat.server.auth import AuthPolicy
else:  # pragma: no cover - runtime placeholder until M5 ships ``auth.py``
    AuthPolicy = Any


@dataclass
class VoiceServerConfig:
    """Process-policy configuration for :class:`VoiceServer`.

    M4 reads ``host`` / ``port`` / ``max_sessions`` / ``enable_websocket`` /
    ``enable_health``. The remaining fields are declared now so later
    milestones land cleanly, but several are inert in M4:

    * ``auth`` / ``unsafe_allow_no_auth`` — the unified guard that consumes them
      is M5; M4 stores them only.
    * ``enable_webrtc`` — WebRTC mounting is M7.
    * ``enable_metrics`` — metric emission/registration is M8.
    * ``manifest_path`` / ``profile`` — the manifest loader is M6a.
    """

    host: str = "127.0.0.1"
    port: int = 8080
    public_base_url: str | None = None
    max_sessions: int = 64
    drain_timeout_s: float = 30.0
    force_shutdown_timeout_s: float = 10.0
    # ``AuthPolicy`` is defined in M5; ``None`` is the M4 default (no guard).
    auth: AuthPolicy | None = None
    # Mirror of the ``AuthPolicy`` escape hatch: the ONLY way to bind a
    # non-loopback host with no token. Stored in M4; the guard is M5.
    unsafe_allow_no_auth: bool = False
    cors_allowed_origins: tuple[str, ...] = ()
    enable_websocket: bool = True
    enable_webrtc: bool = True
    enable_health: bool = True
    enable_metrics: bool = True
    manifest_path: Path | None = None
    profile: str = "default"
