"""Twilio app settings helpers shared by examples and small FastAPI apps."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from easycat.events import EventBus

if TYPE_CHECKING:
    from easycat.telephony.outbound import OutboundCallManager
    from easycat.telephony.session_actions import TwilioSessionActionConfig


def _settings_value(value: str | None) -> str:
    """Normalize env/settings values so blank secrets do not count as configured."""
    return (value or "").strip()


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


def twilio_app_settings_from_env(
    *,
    stream_url: str | None = None,
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

    return TwilioAppSettings(
        stream_url=resolved_stream_url,
        account_sid=_settings_value(env.get("TWILIO_ACCOUNT_SID")),
        auth_token=_settings_value(env.get("TWILIO_AUTH_TOKEN")),
        voice_from=_settings_value(env.get("TWILIO_VOICE_FROM")),
        twiml_url=_settings_value(env.get("TWILIO_TWIML_URL")),
        status_callback_url=_settings_value(env.get("TWILIO_STATUS_CALLBACK_URL")),
        call_api_token=_settings_value(env.get("TWILIO_CALL_API_TOKEN")),
        sms_from=_settings_value(env.get("TWILIO_SMS_FROM")),
        stream_token_secret=_settings_value(env.get("TWILIO_STREAM_TOKEN_SECRET")),
    )


__all__ = ["TwilioAppSettings", "twilio_app_settings_from_env"]
