"""Outbound call management: Twilio status callback parsing and call placement."""

from __future__ import annotations

__all__ = [
    "OutboundCallManager",
    "OutboundCallManagerState",
    "emit_call_status",
    "parse_call_status_callback",
]

import asyncio
import logging
import math
from collections.abc import Callable
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
from easycat.telephony.voicemail import TWILIO_AMD_MAP

logger = logging.getLogger(__name__)

# SIP response codes indicating call blocking (FCC March 2026 mandate).
_SIP_BLOCK_REASONS: dict[int, str] = {
    603: "declined",
    607: "blocked_unwanted",
    608: "blocked_rejected",
}
_CALL_STATUSES: frozenset[str] = frozenset(
    {
        "initiated",
        "ringing",
        "in-progress",
        "completed",
        "busy",
        "no-answer",
        "failed",
        "canceled",
    }
)

# Reason strings that indicate carrier/callee blocking (for number_health).
BLOCK_REASONS: frozenset[str] = frozenset({"blocked_unwanted", "blocked_rejected"})


def _get_call_sid(params: dict[str, Any]) -> str | None:
    call_sid = params.get("CallSid")
    if not isinstance(call_sid, str) or not call_sid:
        return None
    return call_sid


def _parse_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed < 0:
        return None
    return parsed


def _parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _voicemail_detected_event(result: str, call_sid: str) -> VoicemailDetected:
    """Build the AMD classification event correlated to a Twilio call SID."""
    return VoicemailDetected(result=result, call_sid=call_sid)


def parse_call_status_callback(
    params: dict[str, Any],
) -> CallInitiated | CallRinging | CallAnswered | CallEnded | CallFailed | None:
    """Parse a Twilio status callback into a call lifecycle event.

    Returns ``None`` for missing/unknown statuses.
    """
    status = params.get("CallStatus")
    if status is None:
        return None
    if status not in _CALL_STATUSES:
        return None

    call_sid = _get_call_sid(params)
    if call_sid is None:
        logger.warning("Ignoring Twilio status callback without CallSid: status=%r", status)
        return None

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
        duration_s = _parse_float(params.get("Duration"))
        return CallEnded(
            call_sid=call_sid,
            duration_s=duration_s,
            disposition="completed",
            number=params.get("To", ""),
        )

    if status in {"busy", "no-answer", "failed", "canceled"}:
        sip_code = _parse_int(params.get("SipResponseCode"))
        reason = _SIP_BLOCK_REASONS.get(sip_code, status) if sip_code else status
        return CallFailed(
            call_sid=call_sid,
            reason=reason,
            sip_code=sip_code,
            number=params.get("To", ""),
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
            amd_result = TWILIO_AMD_MAP.get(event.answered_by.lower())
            if amd_result is not None:
                await event_bus.emit(_voicemail_detected_event(amd_result, event.call_sid))
    return event


class OutboundCallManagerState(Enum):
    IDLE = "idle"
    ACTIVE = "active"


class OutboundCallManager:
    """Orchestrates placing outbound calls via the Twilio REST API.

    Requires the ``twilio`` Python package (``pip install easycat[telephony]``).

    Optional pre-call gates (plugged in after construction by
    ``_create_outbound_helpers`` or the caller directly):

    - ``dnc_list`` — a :class:`~easycat.telephony.compliance.DNCList`
      that rejects numbers on the internal Do Not Call list.
    - ``compliance_check`` — a callable ``(to) -> bool`` that returns
      ``False`` when the call should be blocked (e.g. calling hours).
    - ``retry_strategy`` — a :class:`~easycat.telephony.retry.RetryStrategy`
      used by app code to decide whether to retry after a
      :class:`CallFailed`.
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
                "Install it with: pip install easycat[telephony]"
            ) from None

        if not twilio_account_sid or not twilio_auth_token:
            raise ValueError(
                "twilio_account_sid and twilio_auth_token are required for OutboundCallManager"
            )

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
        self._active_call_sid: str | None = None
        self._started = False

        # Optional plug-ins assigned after construction (see docstring).
        self.dnc_list: Any | None = None
        self.compliance_check: Callable[[str], bool] | None = None
        self.retry_strategy: Any | None = None

    @property
    def state(self) -> OutboundCallManagerState:
        return self._state

    @property
    def active_call_sid(self) -> str | None:
        return self._active_call_sid

    def start(self) -> None:
        if self._started:
            return
        self._event_bus.subscribe(CallRinging, self._on_call_ringing)
        self._event_bus.subscribe(CallAnswered, self._on_call_answered)
        self._event_bus.subscribe(CallEnded, self._on_call_ended)
        self._event_bus.subscribe(CallFailed, self._on_call_failed)
        self._started = True
        self._state = OutboundCallManagerState.IDLE
        self._active_call_sid = None

    def stop(self) -> None:
        if self._started:
            self._event_bus.unsubscribe(CallRinging, self._on_call_ringing)
            self._event_bus.unsubscribe(CallAnswered, self._on_call_answered)
            self._event_bus.unsubscribe(CallEnded, self._on_call_ended)
            self._event_bus.unsubscribe(CallFailed, self._on_call_failed)
        self._state = OutboundCallManagerState.IDLE
        self._active_call_sid = None
        self._started = False

    async def hangup_call(self, call_sid: str | None = None) -> None:
        """Hang up an active Twilio call without making ``stop()`` block on REST I/O."""
        target_call_sid = call_sid or self._active_call_sid
        if not target_call_sid:
            return
        await asyncio.to_thread(self._client.calls(target_call_sid).update, status="completed")
        self._clear_active_call(target_call_sid)

    async def place_call(self, to: str) -> str:
        """Place an outbound call and return the call SID.

        Pre-call gates:

        - Numbers on :attr:`dnc_list` raise ``ValueError`` immediately.
        - ``compliance_check(to) is False`` raises ``ValueError``.  Use
          this to wire :func:`~easycat.telephony.compliance.check_calling_hours`
          or your own timezone / DNC-vendor lookup.
        """
        if not self._started:
            raise RuntimeError("OutboundCallManager must be started before placing calls")
        if self._state is not OutboundCallManagerState.IDLE or self._active_call_sid is not None:
            raise RuntimeError("OutboundCallManager already has an active call")
        if self.dnc_list is not None and self.dnc_list.is_on_dnc(to):
            raise ValueError(f"Refusing to call {to!r}: on DNC list")
        if self.compliance_check is not None and not self.compliance_check(to):
            raise ValueError(
                f"Refusing to call {to!r}: blocked by compliance_check "
                "(e.g. outside allowed calling hours)"
            )
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
            self._set_active_call(call_sid)
            await self._event_bus.emit(
                CallInitiated(call_sid=call_sid, to=to, from_=self._from_number)
            )
            return call_sid
        except Exception as exc:
            await self._event_bus.emit(CallFailed(call_sid="", reason=str(exc)))
            raise

    def _set_active_call(self, call_sid: str) -> None:
        if not call_sid:
            return
        if self._active_call_sid not in (None, call_sid):
            return
        self._active_call_sid = call_sid
        self._state = OutboundCallManagerState.ACTIVE

    def _clear_active_call(self, call_sid: str) -> None:
        if self._active_call_sid is not None and call_sid != self._active_call_sid:
            return
        self._active_call_sid = None
        self._state = OutboundCallManagerState.IDLE

    async def _on_call_ringing(self, event: CallRinging) -> None:
        self._set_active_call(event.call_sid)

    async def _on_call_answered(self, event: CallAnswered) -> None:
        self._set_active_call(event.call_sid)

    async def _on_call_ended(self, event: CallEnded) -> None:
        self._clear_active_call(event.call_sid)

    async def _on_call_failed(self, event: CallFailed) -> None:
        self._clear_active_call(event.call_sid)
