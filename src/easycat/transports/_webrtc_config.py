"""WebRTC transport configuration and environment helpers."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from easycat._audio_utils import validate_pcm16_format
from easycat._net import normalize_auth_token
from easycat.audio_format import PCM16_MONO_16K, AudioFormat
from easycat.transports._limits import DEFAULT_INBOUND_AUDIO_MAX_BYTES
from easycat.transports._webrtc_stats import default_webrtc_stats_path

_CORS_ALLOW_METHODS = "POST, GET, OPTIONS"
_CORS_ALLOW_HEADERS = "Content-Type, Authorization"


@dataclass
class ICEServer:
    """STUN or TURN server descriptor."""

    urls: str | list[str]
    username: str | None = None
    credential: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if isinstance(self.urls, str):
            self.urls = [self.urls]


@dataclass
class WebRTCTransportConfig:
    """Configuration for the WebRTC transport and signaling surfaces.

    Parameters
    ----------
    host:
        Bind address for the HTTP signaling server.
    port:
        Listen port for the HTTP signaling server.
    ice_servers:
        STUN/TURN servers for ICE negotiation. Defaults to Google's public
        STUN server. Add a TURN server for NAT traversal when needed.
    audio_format:
        Target audio format for the pipeline side (default 16 kHz PCM16 mono).
    max_pending_chunks:
        Maximum number of inbound audio chunks to buffer before dropping.
    static_dir:
        Directory to serve static files from the signaling server. Defaults to
        the bundled demo client; set to ``None`` to disable static files.
    expose_ice_credentials:
        Include TURN entries that have both a username and credential in the
        public ``/config`` response. Without this opt-in, browser config
        contains STUN URLs only. Prefer short-lived credentials when enabling
        it.
    cors_allowed_origins:
        Exact cross-origin browser origins allowed to call the signaling API.
        The bundled browser client is same-origin and needs no CORS opt-in.
    stats_path:
        Optional JSONL destination for sanitized browser WebRTC stats. Defaults
        to ``EASYCAT_WEBRTC_STATS_PATH`` when that environment variable is set.
    auth_token:
        Shared secret required by ``/config``, ``/offer``, and ``/stats``.
        Clients present it as an ``Authorization: Bearer`` token.
    allow_query_token:
        Also accept a ``?token=`` query parameter. Disabled by default.
    max_sessions:
        Maximum concurrent browser offers accepted by the multi-session server.
    stats_max_records:
        Maximum JSONL records written to ``stats_path`` by this process.
    stats_max_file_bytes:
        Maximum artifact size before new snapshots are rejected.
    stats_max_requests_per_minute:
        In-memory rate limit for accepted ``/stats`` snapshots.
    """

    _BUNDLED_STATIC_DIR: ClassVar[str] = str(Path(__file__).parent / "static")
    _USE_BUNDLED: ClassVar[str] = "__USE_BUNDLED__"

    host: str = "127.0.0.1"
    port: int = 8080
    ice_servers: list[ICEServer] = field(
        default_factory=lambda: [ICEServer(urls="stun:stun.l.google.com:19302")]
    )
    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_16K)
    max_pending_chunks: int = 200
    static_dir: str | None = _USE_BUNDLED
    expose_ice_credentials: bool = False
    cors_allowed_origins: tuple[str, ...] = ()
    stats_path: str | None = field(default_factory=default_webrtc_stats_path)
    auth_token: str | None = field(default=None, repr=False)
    allow_query_token: bool = False
    max_sessions: int = 64
    stats_max_records: int = 1_000
    stats_max_file_bytes: int = 1_048_576
    stats_max_requests_per_minute: int = 120
    max_pending_bytes: int = DEFAULT_INBOUND_AUDIO_MAX_BYTES

    def __post_init__(self) -> None:
        validate_pcm16_format("audio_format", self.audio_format)
        if self.audio_format.channels != 1:
            raise ValueError(
                "audio_format must be mono PCM16 audio "
                f"(got channels={self.audio_format.channels!r})"
            )


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def sanitize_webrtc_base(raw: str) -> str:
    """Return a safe same-origin path prefix from an untrusted base value."""
    if not raw or not raw.startswith("/"):
        return ""
    if "//" in raw or "\\" in raw or ":" in raw or "?" in raw or "#" in raw:
        return ""
    if ".." in raw.split("/"):
        return ""
    return raw.rstrip("/")


def webrtc_ice_servers_from_env(
    *,
    turn_url_env: str = "TURN_SERVER_URL",
    turn_username_env: str = "TURN_USERNAME",
    turn_credential_env: str = "TURN_CREDENTIAL",
    include_public_stun: bool = True,
) -> list[ICEServer]:
    """Build STUN/TURN servers from the standard WebRTC environment."""
    servers: list[ICEServer] = []
    if include_public_stun:
        servers.append(ICEServer(urls="stun:stun.l.google.com:19302"))

    turn_url = os.getenv(turn_url_env, "").strip()
    if turn_url:
        servers.append(
            ICEServer(
                urls=turn_url,
                username=os.getenv(turn_username_env),
                credential=os.getenv(turn_credential_env),
            )
        )
    return servers


def webrtc_transport_config_from_env(
    *,
    host_env: str = "SIGNALING_HOST",
    port_env: str = "SIGNALING_PORT",
    auth_token_env: str = "WEBRTC_SIGNALING_TOKEN",
    max_sessions_env: str = "WEBRTC_MAX_SESSIONS",
    expose_ice_credentials_env: str = "WEBRTC_EXPOSE_ICE_CREDENTIALS",
    static_dir: str | None = WebRTCTransportConfig._USE_BUNDLED,
) -> WebRTCTransportConfig:
    """Build browser WebRTC transport config from deployment environment."""
    return WebRTCTransportConfig(
        host=os.getenv(host_env, "127.0.0.1"),
        port=int(os.getenv(port_env, "8080")),
        ice_servers=webrtc_ice_servers_from_env(),
        static_dir=static_dir,
        auth_token=normalize_auth_token(os.getenv(auth_token_env)),
        max_sessions=int(os.getenv(max_sessions_env, "64")),
        expose_ice_credentials=_env_flag(expose_ice_credentials_env),
    )
