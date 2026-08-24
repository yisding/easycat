"""Telnyx app settings helpers shared by examples and small FastAPI apps."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from easycat.teardown_budgets import (
    SERVER_DRAIN_TIMEOUT_S,
    SERVER_FORCE_SHUTDOWN_TIMEOUT_S,
)

if TYPE_CHECKING:
    from easycat.telephony.session_actions import TelnyxSessionActionConfig


def _settings_value(value: str | None) -> str:
    """Normalize env/settings values so blank secrets do not count as configured."""
    return (value or "").strip()


def _non_negative_float_setting(
    env: Mapping[str, str],
    name: str,
    *,
    default: float,
) -> float:
    raw = _settings_value(env.get(name))
    try:
        value = float(raw) if raw else default
    except ValueError:
        value = -1.0
    if not math.isfinite(value) or value < 0:
        raise RuntimeError(f"{name} must be a non-negative number")
    return value


def _positive_float_setting(
    env: Mapping[str, str],
    name: str,
    *,
    default: float,
) -> float:
    raw = _settings_value(env.get(name))
    try:
        value = float(raw) if raw else default
    except (TypeError, ValueError, OverflowError):
        value = 0.0
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} must be a positive number")
    return value


def _positive_int_setting(
    env: Mapping[str, str],
    name: str,
    *,
    default: int,
) -> int:
    raw = _settings_value(env.get(name))
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = 0
    if value <= 0:
        raise RuntimeError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True, slots=True)
class TelnyxAppSettings:
    """Environment-derived settings for a Telnyx Call Control app."""

    stream_url: str
    api_key: str = field(default="", repr=False)
    public_key: str = ""
    connection_id: str = ""
    ws_port: int = 8766
    stream_token_secret: str = field(default="", repr=False)
    max_sessions: int = 64
    start_timeout_s: float = 10.0
    drain_timeout_s: float = SERVER_DRAIN_TIMEOUT_S
    force_shutdown_timeout_s: float = SERVER_FORCE_SHUTDOWN_TIMEOUT_S

    @property
    def webhook_verification_enabled(self) -> bool:
        return bool(self.public_key)

    @property
    def telnyx_actions_enabled(self) -> bool:
        return bool(self.api_key)

    def telnyx_session_actions(self) -> TelnyxSessionActionConfig | None:
        """Return Telnyx session-action config when credentials are available."""
        if not self.telnyx_actions_enabled:
            return None
        from easycat.telephony.session_actions import TelnyxSessionActionConfig

        return TelnyxSessionActionConfig(api_key=self.api_key, connection_id=self.connection_id)


def telnyx_app_settings_from_env(
    *,
    stream_url: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> TelnyxAppSettings:
    """Read the standard Telnyx example/app environment variables.

    ``TELNYX_STREAM_URL`` must be the public ``wss://`` URL Telnyx dials back
    into; anything else is rejected here so a misconfigured ``https://`` or
    plain-host value fails at startup instead of after Telnyx answers a call.
    """
    env = os.environ if environ is None else environ
    resolved_stream_url = _settings_value(stream_url) or _settings_value(
        env.get("TELNYX_STREAM_URL")
    )
    if not resolved_stream_url:
        raise RuntimeError(
            "TELNYX_STREAM_URL is required. Set it to the public wss:// URL Telnyx should "
            "connect to."
        )
    if not resolved_stream_url.lower().startswith("wss://"):
        raise RuntimeError(f"TELNYX_STREAM_URL must use wss:// (got {resolved_stream_url!r})")

    max_sessions = _positive_int_setting(env, "TELNYX_MAX_SESSIONS", default=64)
    ws_port = _positive_int_setting(env, "TELNYX_WS_PORT", default=8766)
    start_timeout_s = _positive_float_setting(env, "TELNYX_START_TIMEOUT_S", default=10.0)

    return TelnyxAppSettings(
        stream_url=resolved_stream_url,
        api_key=_settings_value(env.get("TELNYX_API_KEY")),
        public_key=_settings_value(env.get("TELNYX_PUBLIC_KEY")),
        connection_id=_settings_value(env.get("TELNYX_CONNECTION_ID")),
        ws_port=ws_port,
        stream_token_secret=_settings_value(env.get("TELNYX_STREAM_TOKEN_SECRET")),
        max_sessions=max_sessions,
        start_timeout_s=start_timeout_s,
        drain_timeout_s=_non_negative_float_setting(
            env,
            "TELNYX_DRAIN_TIMEOUT_S",
            default=SERVER_DRAIN_TIMEOUT_S,
        ),
        force_shutdown_timeout_s=_non_negative_float_setting(
            env,
            "TELNYX_FORCE_SHUTDOWN_TIMEOUT_S",
            default=SERVER_FORCE_SHUTDOWN_TIMEOUT_S,
        ),
    )


__all__ = [
    "TelnyxAppSettings",
    "telnyx_app_settings_from_env",
]
