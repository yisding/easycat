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
from typing import Literal

from easycat._numeric import is_finite_number

# ``AuthPolicy`` is a real type as of M5 (``easycat.server.auth``). The import
# is light (auth.py pulls only hmac/dataclasses/typing/os and the leaf
# ``is_loopback_host`` from ``easycat._net``), so importing it at module load
# does not pull aiohttp or any heavy SDK and keeps ``import easycat.server`` light.
from easycat.server.auth import AuthPolicy
from easycat.server.transports import _validate_max_sessions


@dataclass
class VoiceServerConfig:
    """Process-policy configuration for :class:`VoiceServer`.

    M4 reads ``host`` / ``port`` / ``max_sessions`` / ``enable_websocket`` /
    ``enable_health``. M5 makes ``auth`` / ``unsafe_allow_no_auth`` /
    ``allow_query_token`` LIVE (the unified bind guard + ``/ws`` authorization).
    The remaining fields are declared now so later milestones land cleanly, but
    several are still inert:

    * ``auth`` / ``unsafe_allow_no_auth`` / ``allow_query_token`` — LIVE in M5
      (the unified guard + ``/ws`` authorization consume them).
    * ``enable_webrtc`` — LIVE in M7: when ``True`` and a ``session_factory`` is
      configured, ``VoiceServer`` mounts the WebRTC routes under ``/webrtc/*`` on
      the health listener, sharing the capacity gate and active-session set with
      ``/ws``.
    * ``enable_metrics`` — metric emission/registration is M8.
    * ``manifest_path`` / ``profile`` — the manifest loader is M6a.
    """

    host: str = "127.0.0.1"
    port: int = 8080
    public_base_url: str | None = None
    max_sessions: int = 64
    drain_timeout_s: float = 30.0
    force_shutdown_timeout_s: float = 10.0
    drain_mode: Literal["stop_sessions", "await_natural_end"] = "stop_sessions"
    # The unified auth policy (M5). ``None`` means no token policy — subject to
    # the non-loopback bind guard, which still raises for a non-loopback host
    # unless ``unsafe_allow_no_auth`` is set.
    auth: AuthPolicy | None = None
    # Mirror of the ``AuthPolicy`` escape hatch: the ONLY way to bind a
    # non-loopback host with no token. Default keeps the unified guard armed.
    unsafe_allow_no_auth: bool = False
    # Mirror of ``BearerTokenAuth.allow_query_token`` (default OFF — a breaking
    # change for the browser WS client, which cannot set handshake headers).
    # When ``auth`` is a ``BearerTokenAuth`` the server honors its own
    # ``allow_query_token``; this field is the process-layer default for
    # policies the server constructs from env.
    allow_query_token: bool = False
    cors_allowed_origins: tuple[str, ...] = ()
    enable_websocket: bool = True
    # LIVE in M7: mount the WebRTC routes under ``/webrtc/*`` (requires a
    # ``session_factory``).
    enable_webrtc: bool = True
    enable_health: bool = True
    enable_metrics: bool = True
    manifest_path: Path | None = None
    profile: str = "default"

    def __post_init__(self) -> None:
        _validate_max_sessions(self.max_sessions)
        for name, value in (
            ("drain_timeout_s", self.drain_timeout_s),
            ("force_shutdown_timeout_s", self.force_shutdown_timeout_s),
        ):
            if not is_finite_number(value) or value < 0:
                raise ValueError(f"{name} must be a finite number >= 0")
