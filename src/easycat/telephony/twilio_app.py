"""Twilio app settings helpers shared by examples and small FastAPI apps."""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hmac import compare_digest
from typing import TYPE_CHECKING, Any

from easycat.events import CallAnswered, CallEnded, CallFailed, EventBus
from easycat.teardown_budgets import (
    SERVER_DRAIN_TIMEOUT_S,
    SERVER_FORCE_SHUTDOWN_TIMEOUT_S,
)

if TYPE_CHECKING:
    from easycat.telephony.outbound import OutboundCallManager
    from easycat.telephony.session_actions import TwilioSessionActionConfig


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
class TwilioAppSettings:
    """Environment-derived settings for a Twilio Media Streams app."""

    stream_url: str
    account_sid: str = ""
    auth_token: str = field(default="", repr=False)
    voice_from: str = ""
    twiml_url: str = ""
    status_callback_url: str = ""
    call_api_token: str = field(default="", repr=False)
    sms_from: str = ""
    stream_token_secret: str = field(default="", repr=False)
    max_sessions: int = 64
    start_timeout_s: float = 10.0
    public_twiml_url: str = ""
    drain_timeout_s: float = SERVER_DRAIN_TIMEOUT_S
    force_shutdown_timeout_s: float = SERVER_FORCE_SHUTDOWN_TIMEOUT_S

    @property
    def stream_token_secret_or_auth_token(self) -> str | None:
        return self.stream_token_secret or self.auth_token or None

    @property
    def outbound_calling_enabled(self) -> bool:
        return bool(self.account_sid and self.auth_token and self.voice_from and self.twiml_url)

    @property
    def twilio_actions_enabled(self) -> bool:
        return bool(self.account_sid and self.auth_token)

    def twilio_session_actions(self) -> TwilioSessionActionConfig | None:
        """Return Twilio session-action config when credentials are available."""
        if not self.twilio_actions_enabled:
            return None
        from easycat.telephony.session_actions import TwilioSessionActionConfig

        return TwilioSessionActionConfig(
            account_sid=self.account_sid,
            auth_token=self.auth_token,
            sms_from_number=self.sms_from,
        )

    def start_outbound_manager(self, event_bus: EventBus) -> OutboundCallManager | None:
        """Start outbound calling when the required Twilio env vars are present."""
        if not self.outbound_calling_enabled:
            return None
        from easycat.telephony.outbound import OutboundCallManager

        manager = OutboundCallManager(
            event_bus,
            from_number=self.voice_from,
            twilio_account_sid=self.account_sid,
            twilio_auth_token=self.auth_token,
            twiml_url=self.twiml_url,
            status_callback_url=self.status_callback_url,
        )
        manager.start()
        return manager


class TwilioCallSessionIndex:
    """Track active call SIDs without duplicating event wiring in app servers."""

    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}

    def track(self, session: Any) -> Callable[[], None]:
        call_sid: str | None = None

        def remember(event: CallAnswered) -> None:
            nonlocal call_sid
            if event.call_sid:
                if call_sid and call_sid != event.call_sid:
                    self._sessions.pop(call_sid, None)
                call_sid = event.call_sid
                self._sessions[event.call_sid] = session

        def forget(event: CallEnded | CallFailed) -> None:
            nonlocal call_sid
            if event.call_sid:
                self._sessions.pop(event.call_sid, None)
            if event.call_sid == call_sid:
                call_sid = None

        session.event_bus.subscribe(CallAnswered, remember)
        session.event_bus.subscribe(CallEnded, forget)
        session.event_bus.subscribe(CallFailed, forget)

        def cleanup() -> None:
            session.event_bus.unsubscribe(CallAnswered, remember)
            session.event_bus.unsubscribe(CallEnded, forget)
            session.event_bus.unsubscribe(CallFailed, forget)
            if call_sid:
                self._sessions.pop(call_sid, None)

        return cleanup

    def get(self, call_sid: str) -> Any | None:
        return self._sessions.get(call_sid)


def bearer_token_matches(header: str | None, token: str) -> bool:
    """Return whether an Authorization header matches without timing leaks.

    Fails closed on an unconfigured token: with ``token == ""`` — which is what
    ``TwilioAppSettings.call_api_token`` holds when ``TWILIO_CALL_API_TOKEN``
    is unset — a bare ``Authorization: Bearer`` header would otherwise compare
    equal and authenticate every such request. This mirrors
    :func:`~easycat.telephony.twiml.validate_twilio_webhook_signature`, which
    already returns ``False`` when its auth token is empty (gh 1105).
    """
    if not token:
        return False
    scheme, separator, provided = (header or "").partition(" ")
    if separator != " " or scheme.lower() != "bearer":
        return False
    return provided.isascii() and token.isascii() and compare_digest(provided, token)


def twilio_app_settings_from_env(
    *,
    stream_url: str | None = None,
    auth_token: str | None = None,
    require_auth_token: bool = False,
    environ: Mapping[str, str] | None = None,
) -> TwilioAppSettings:
    """Read the standard Twilio example/app environment variables."""
    env = os.environ if environ is None else environ
    resolved_stream_url = _settings_value(stream_url) or _settings_value(
        env.get("TWILIO_STREAM_URL")
    )
    if not resolved_stream_url:
        raise RuntimeError(
            "TWILIO_STREAM_URL is required. Set it to the public wss:// URL Twilio should "
            "connect to."
        )
    if not resolved_stream_url.lower().startswith("wss://"):
        raise RuntimeError(f"TWILIO_STREAM_URL must use wss:// (got {resolved_stream_url!r})")
    resolved_auth_token = _settings_value(auth_token) or _settings_value(
        env.get("TWILIO_AUTH_TOKEN")
    )
    if require_auth_token and not resolved_auth_token:
        raise RuntimeError(
            "TWILIO_AUTH_TOKEN is required to authenticate Twilio webhooks and "
            "the media WebSocket handshake."
        )

    max_sessions = _positive_int_setting(env, "TWILIO_MAX_SESSIONS", default=64)
    start_timeout_s = _positive_float_setting(env, "TWILIO_START_TIMEOUT_S", default=10.0)

    return TwilioAppSettings(
        stream_url=resolved_stream_url,
        account_sid=_settings_value(env.get("TWILIO_ACCOUNT_SID")),
        auth_token=resolved_auth_token,
        voice_from=_settings_value(env.get("TWILIO_VOICE_FROM")),
        twiml_url=_settings_value(env.get("TWILIO_TWIML_URL")),
        status_callback_url=_settings_value(env.get("TWILIO_STATUS_CALLBACK_URL")),
        call_api_token=_settings_value(env.get("TWILIO_CALL_API_TOKEN")),
        sms_from=_settings_value(env.get("TWILIO_SMS_FROM")),
        stream_token_secret=_settings_value(env.get("TWILIO_STREAM_TOKEN_SECRET")),
        max_sessions=max_sessions,
        start_timeout_s=start_timeout_s,
        public_twiml_url=_settings_value(env.get("TWILIO_PUBLIC_TWIML_URL")),
        drain_timeout_s=_non_negative_float_setting(
            env,
            "TWILIO_DRAIN_TIMEOUT_S",
            default=SERVER_DRAIN_TIMEOUT_S,
        ),
        force_shutdown_timeout_s=_non_negative_float_setting(
            env,
            "TWILIO_FORCE_SHUTDOWN_TIMEOUT_S",
            default=SERVER_FORCE_SHUTDOWN_TIMEOUT_S,
        ),
    )


__all__ = [
    "TwilioAppSettings",
    "TwilioCallSessionIndex",
    "bearer_token_matches",
    "twilio_app_settings_from_env",
]
