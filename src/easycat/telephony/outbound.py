"""Outbound call management: Twilio status callback parsing and call placement."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from easycat.events import (
    CallAnswered,
    CallEnded,
    CallFailed,
    CallInitiated,
    CallRinging,
    EventBus,
    VoicemailDetected,
)

logger = logging.getLogger(__name__)

# Twilio AnsweredBy → VoicemailDetected result mapping (mirrors voicemail._TWILIO_AMD_MAP).
_ANSWERED_BY_MAP: dict[str, str] = {
    "human": "human",
    "machine_start": "machine",
    "machine_end_beep": "machine",
    "machine_end_silence": "machine",
    "machine_end_other": "machine",
    "fax": "machine",
    "unknown": "unknown",
}

# SIP response codes indicating call blocking (FCC March 2026 mandate).
_SIP_BLOCK_REASONS: dict[int, str] = {
    603: "declined",
    607: "blocked_unwanted",
    608: "blocked_rejected",
}


def parse_call_status_callback(
    params: dict[str, Any],
) -> CallInitiated | CallRinging | CallAnswered | CallEnded | CallFailed | None:
    """Parse a Twilio status callback into a call lifecycle event.

    Returns ``None`` for missing/unknown statuses.
    """
    status = params.get("CallStatus")
    if status is None:
        return None

    call_sid = params.get("CallSid", "")

    if status == "initiated":
        return CallInitiated(
            call_sid=call_sid,
            to=params.get("To", ""),
            from_=params.get("From", ""),
        )

    if status == "ringing":
        return CallRinging(call_sid=call_sid)

    if status == "in-progress":
        return CallAnswered(call_sid=call_sid, answered_by=params.get("AnsweredBy"))

    if status == "completed":
        duration_raw = params.get("Duration")
        duration_s = float(duration_raw) if duration_raw not in (None, "") else None
        return CallEnded(
            call_sid=call_sid,
            duration_s=duration_s,
            disposition="completed",
            number=params.get("From", ""),
        )

    if status in {"busy", "no-answer", "failed", "canceled"}:
        sip_code_raw = params.get("SipResponseCode")
        sip_code = int(sip_code_raw) if sip_code_raw not in (None, "") else None
        reason = _SIP_BLOCK_REASONS.get(sip_code, status) if sip_code else status
        return CallFailed(
            call_sid=call_sid,
            reason=reason,
            sip_code=sip_code,
            number=params.get("From", ""),
        )

    return None


async def emit_call_status(
    params: dict[str, Any], event_bus: EventBus
) -> CallInitiated | CallRinging | CallAnswered | CallEnded | CallFailed | None:
    """Parse a Twilio status callback and emit the resulting event.

    When the callback is an ``in-progress`` status that includes an
    ``AnsweredBy`` field (Twilio async AMD), a ``VoicemailDetected`` event
    is also emitted so the call state machine can classify the call without
    requiring a separate ``emit_twilio_amd()`` call.
    """
    event = parse_call_status_callback(params)
    if event is not None:
        await event_bus.emit(event)
        # Emit AMD classification when AnsweredBy is present on the answered callback.
        if isinstance(event, CallAnswered) and event.answered_by:
            amd_result = _ANSWERED_BY_MAP.get(event.answered_by.lower())
            if amd_result is not None:
                await event_bus.emit(VoicemailDetected(result=amd_result))
    return event


class OutboundCallManagerState(Enum):
    IDLE = "idle"
    ACTIVE = "active"


@dataclass
class _PlaceCallResult:
    call_sid: str


class OutboundCallManager:
    """Orchestrates placing outbound calls via the Twilio REST API.

    Requires the ``twilio`` Python package (``pip install easycat[twilio]``).
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        from_number: str,
        amd_mode: str = "DetectMessageEnd",
        async_amd: bool = True,
        amd_timeout: int = 30,
        speech_threshold: int = 2400,
        speech_end_threshold: int = 1200,
        silence_timeout: int = 5000,
        enable_realtime_transcription: bool = True,
        twilio_account_sid: str = "",
        twilio_auth_token: str = "",
        status_callback_url: str = "",
        twiml_url: str = "",
    ) -> None:
        # Lazy-import twilio at instantiation time.
        try:
            from twilio.rest import Client as TwilioClient  # noqa: F401
        except ImportError:
            raise ImportError(
                "The 'twilio' package is required for OutboundCallManager. "
                "Install it with: pip install easycat[twilio]"
            ) from None

        self._event_bus = event_bus
        self._from_number = from_number
        self._amd_mode = amd_mode
        self._async_amd = async_amd
        self._amd_timeout = amd_timeout
        self._speech_threshold = speech_threshold
        self._speech_end_threshold = speech_end_threshold
        self._silence_timeout = silence_timeout
        self._enable_realtime_transcription = enable_realtime_transcription
        self._status_callback_url = status_callback_url
        self._twiml_url = twiml_url
        self._client: TwilioClient = TwilioClient(twilio_account_sid, twilio_auth_token)
        self._state = OutboundCallManagerState.IDLE
        self._started = False

    @property
    def state(self) -> OutboundCallManagerState:
        return self._state

    def start(self) -> None:
        self._started = True
        self._state = OutboundCallManagerState.IDLE

    def stop(self) -> None:
        self._state = OutboundCallManagerState.IDLE
        self._started = False

    async def place_call(self, to: str) -> str:
        """Place an outbound call and return the call SID."""
        create_kwargs: dict[str, Any] = {
            "to": to,
            "from_": self._from_number,
            "url": self._twiml_url,
            "machine_detection": self._amd_mode,
            "async_amd": str(self._async_amd).lower(),
            "machine_detection_timeout": self._amd_timeout,
            "machine_detection_speech_threshold": self._speech_threshold,
            "machine_detection_speech_end_threshold": self._speech_end_threshold,
            "machine_detection_silence_timeout": self._silence_timeout,
        }
        if self._enable_realtime_transcription:
            create_kwargs["transcription"] = True
            create_kwargs["transcription_track"] = "inbound_track"

        if self._status_callback_url:
            create_kwargs["status_callback"] = self._status_callback_url
            create_kwargs["status_callback_event"] = [
                "initiated",
                "ringing",
                "answered",
                "completed",
            ]

        try:
            call = await asyncio.to_thread(self._client.calls.create, **create_kwargs)
            call_sid: str = call.sid
            self._state = OutboundCallManagerState.ACTIVE
            await self._event_bus.emit(
                CallInitiated(call_sid=call_sid, to=to, from_=self._from_number)
            )
            return call_sid
        except Exception as exc:
            await self._event_bus.emit(CallFailed(call_sid="", reason=str(exc)))
            raise
