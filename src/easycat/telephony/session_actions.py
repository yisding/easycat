"""Telephony-backed session action executors."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Literal, get_args

from easycat._dtmf_tones import DTMFToneConfig
from easycat._numeric import is_finite_number
from easycat.session.actions import (
    MAX_DTMF_INTER_DIGIT_DELAY_MS,
    EndCallAction,
    SendDTMFAction,
    SendSMSAction,
    SessionAction,
    SessionActionExecutor,
    SessionActionResult,
    TransferCallAction,
)
from easycat.telephony._install import TELEPHONY_INSTALL_HINT
from easycat.telephony.twiml import twiml_dial_number, twiml_play_digits

logger = logging.getLogger(__name__)

TwilioDTMFMode = Literal["rest", "in_band", "auto"]
"""How :class:`TwilioSessionActionExecutor` delivers :class:`SendDTMFAction`.

* ``"rest"`` (default): a REST call update with ``<Play digits>``. The update
  replaces the call's ``<Connect><Stream>`` TwiML, so the media stream ends and
  the action requests a session stop.
* ``"in_band"``: ITU-T Q.23 tones on the live media stream through
  ``Session.play_dtmf_tones``; the session keeps running. Fails closed when the
  session cannot play tones.
* ``"auto"``: in-band when the session can play tones, else REST with a
  warning.
"""

TWILIO_DTMF_MODES: tuple[str, ...] = get_args(TwilioDTMFMode)


@dataclass
class TwilioSessionActionConfig:
    """Configuration for Twilio-backed session action execution.

    ``dtmf_mode`` selects how :class:`SendDTMFAction` reaches the far end (see
    :data:`TwilioDTMFMode`). The remaining ``dtmf_*`` fields apply only to the
    in-band path: ``dtmf_tones`` overrides the Q.23 tone timing and levels,
    ``dtmf_wait_for_playback`` makes the action wait until Twilio acknowledges
    that the tones played (bounded by the tone length plus
    ``dtmf_playback_timeout_margin_s``), and ``dtmf_fence_timeout_s`` bounds the
    wait for queued bot speech to finish before the tones start.
    """

    account_sid: str = ""
    auth_token: str = field(default="", repr=False)
    sms_from_number: str = ""
    client: Any = None
    dtmf_mode: TwilioDTMFMode = "rest"
    dtmf_tones: DTMFToneConfig | None = None
    dtmf_wait_for_playback: bool = True
    dtmf_playback_timeout_margin_s: float = 10.0
    dtmf_fence_timeout_s: float = 10.0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Revalidate the DTMF delivery policy (the dataclass is mutable)."""
        if self.dtmf_mode not in TWILIO_DTMF_MODES:
            raise ValueError(f"dtmf_mode must be one of {', '.join(TWILIO_DTMF_MODES)}")
        if self.dtmf_tones is not None and not isinstance(self.dtmf_tones, DTMFToneConfig):
            raise TypeError("dtmf_tones must be a DTMFToneConfig or None")
        if not isinstance(self.dtmf_wait_for_playback, bool):
            raise TypeError("dtmf_wait_for_playback must be a bool")
        for name in ("dtmf_playback_timeout_margin_s", "dtmf_fence_timeout_s"):
            value = getattr(self, name)
            if not is_finite_number(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")


class TwilioSessionActionExecutor(SessionActionExecutor):
    """Execute session actions by updating the active Twilio call.

    Hangup, transfer, SMS and REST-mode DTMF use the Twilio REST client, which
    is built lazily on first use. In-band DTMF (``dtmf_mode="in_band"`` or
    ``"auto"``) plays tones on the live media stream through the session and
    needs neither the Twilio SDK nor credentials.

    ``SendDTMFAction`` defaults to ``dtmf_mode="rest"``: the ``<Play digits>``
    call update replaces the call's ``<Connect><Stream>``, so the result
    requests ``stop_session=True`` with ``media_stream_replaced: true``. In-band
    results leave the session running (``stop_session=False``) and report
    ``delivery: "in_band"`` with the playback outcome.
    """

    def __init__(self, config: TwilioSessionActionConfig) -> None:
        config.validate()
        self._config = config
        self._client = config.client

    def supports(self, action: SessionAction) -> bool:
        return isinstance(
            action,
            (EndCallAction, TransferCallAction, SendDTMFAction, SendSMSAction),
        )

    async def execute(self, session: Any, action: SessionAction) -> SessionActionResult:
        call_sid = getattr(session.transport, "call_sid", None)
        if not call_sid:
            raise RuntimeError("Twilio session actions require an active call_sid")

        if isinstance(action, EndCallAction):
            client = self._get_client()
            await self._update_call(client, call_sid, status="completed")
            return SessionActionResult(stop_session=True, metadata={"call_sid": call_sid})

        if isinstance(action, TransferCallAction):
            client = self._get_client()
            twiml = twiml_dial_number(
                action.target,
                caller_id=action.plan.caller_id,
                send_digits=action.plan.post_dial_digits,
                preamble=action.plan.client_message or None,
            )
            await self._update_call(client, call_sid, twiml=twiml)
            return SessionActionResult(
                stop_session=True,
                metadata={"call_sid": call_sid, "target": action.target},
            )

        if isinstance(action, SendDTMFAction):
            return await self._send_dtmf(session, call_sid, action)

        if isinstance(action, SendSMSAction):
            if not self._config.sms_from_number:
                raise RuntimeError("Twilio SMS actions require sms_from_number")
            client = self._get_client()
            message = await asyncio.to_thread(
                client.messages.create,
                to=action.to,
                from_=self._config.sms_from_number,
                body=action.body,
            )
            return SessionActionResult(
                metadata={"message_sid": getattr(message, "sid", ""), "to": action.to}
            )

        raise RuntimeError(f"Unsupported Twilio session action: {action.type}")

    async def _send_dtmf(
        self,
        session: Any,
        call_sid: str,
        action: SendDTMFAction,
    ) -> SessionActionResult:
        self._config.validate()
        mode = self._config.dtmf_mode
        if mode == "rest":
            return await self._send_dtmf_rest(call_sid, action)
        play = getattr(session, "play_dtmf_tones", None)
        if callable(play):
            return await self._send_dtmf_in_band(play, call_sid, action)
        if mode == "in_band":
            raise RuntimeError(
                "in-band Twilio DTMF needs a running EasyCat Session; set "
                "dtmf_mode='rest' for the stream-ending call-update path"
            )
        logger.warning(
            "Twilio dtmf_mode='auto': the session cannot play in-band DTMF tones, "
            "falling back to REST <Play digits>; this ends the media stream and "
            "stops the session"
        )
        return await self._send_dtmf_rest(call_sid, action)

    async def _send_dtmf_rest(
        self,
        call_sid: str,
        action: SendDTMFAction,
    ) -> SessionActionResult:
        # Twilio cannot send outbound DTMF over a bidirectional Media Stream
        # as RFC 2833 events, so this path plays the tones as a REST call
        # update. That update *replaces* the call's executing TwiML, which ends
        # this session's ``<Connect><Stream>``; once ``<Play digits>`` finishes
        # the call has no further TwiML and Twilio hangs up. The session cannot
        # keep talking on a stream that no longer exists, so request a stop the
        # same way transfer does instead of leaving it on a dead transport.
        client = self._get_client()
        digits = _apply_inter_digit_delay(action.digits, action.inter_digit_delay_ms)
        await self._update_call(client, call_sid, twiml=twiml_play_digits(digits))
        return SessionActionResult(
            stop_session=True,
            metadata={
                "call_sid": call_sid,
                "digits": digits,
                "delivery": "rest",
                "media_stream_replaced": True,
            },
        )

    async def _send_dtmf_in_band(
        self,
        play: Any,
        call_sid: str,
        action: SendDTMFAction,
    ) -> SessionActionResult:
        config = self._config
        playback = await play(
            action.digits,
            inter_digit_delay_ms=action.inter_digit_delay_ms,
            tone_config=config.dtmf_tones,
            fence_timeout_s=config.dtmf_fence_timeout_s,
        )
        outcome = "sent"
        if config.dtmf_wait_for_playback:
            outcome = await playback.wait(
                playback.duration_ms / 1000 + config.dtmf_playback_timeout_margin_s
            )
            if outcome == "disconnected":
                raise RuntimeError(
                    "the Twilio media stream ended before the in-band DTMF tones played"
                )
            if outcome == "timeout":
                # The tones left EasyCat; Twilio only failed to confirm them in
                # time. Report completion so a retry never presses keys twice.
                logger.warning(
                    "Twilio did not confirm in-band DTMF playback in time; "
                    "treating the tones as sent"
                )
        return SessionActionResult(
            stop_session=False,
            metadata={
                "call_sid": call_sid,
                "digits": action.digits,
                "delivery": "in_band",
                "duration_ms": playback.duration_ms,
                "playback_mark": playback.mark_name,
                "playback": outcome,
                "inter_digit_delay_ms": action.inter_digit_delay_ms,
            },
        )

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._config.account_sid or not self._config.auth_token:
            raise RuntimeError("Twilio session actions require account_sid and auth_token")
        try:
            from twilio.rest import Client as TwilioClient
        except ImportError as exc:  # pragma: no cover - exercised via config tests
            raise RuntimeError(
                "The 'twilio' package is required for Twilio session actions. "
                + TELEPHONY_INSTALL_HINT
            ) from exc
        self._client = TwilioClient(self._config.account_sid, self._config.auth_token)
        return self._client

    async def _update_call(
        self,
        client: Any,
        call_sid: str,
        *,
        twiml: str | None = None,
        status: str | None = None,
    ) -> None:
        kwargs: dict[str, Any] = {}
        if twiml is not None:
            kwargs["twiml"] = twiml
        if status is not None:
            kwargs["status"] = status
        await asyncio.to_thread(client.calls(call_sid).update, **kwargs)

    async def close(self) -> None:
        """Release no shared resources; present for executor lifecycle parity."""
        return


def _apply_inter_digit_delay(digits: str, inter_digit_delay_ms: int) -> str:
    if inter_digit_delay_ms <= 0 or len(digits) <= 1:
        return digits
    bounded_delay_ms = min(inter_digit_delay_ms, MAX_DTMF_INTER_DIGIT_DELAY_MS)
    pauses = max(1, round(bounded_delay_ms / 1000))
    separator = "W" * pauses
    return separator.join(digits)


@dataclass
class TelnyxSessionActionConfig:
    """Configuration for Telnyx-backed session action execution."""

    api_key: str = field(default="", repr=False)
    sms_from_number: str = ""
    connection_id: str = ""
    client: Any = None


class TelnyxSessionActionExecutor(SessionActionExecutor):
    """Execute session actions via native Telnyx Call Control commands.

    Transfer/DTMF/hangup map to the ``/actions/*`` command endpoints and SMS
    to ``POST /v2/messages`` — no TwiML redirect round-trip.

    The lazily created client owns an aiohttp session; call :meth:`close`
    when the executor is no longer needed (e.g., on session teardown) to
    release the connector.
    """

    def __init__(self, config: TelnyxSessionActionConfig) -> None:
        self._config = config
        self._client = config.client

    def supports(self, action: SessionAction) -> bool:
        return isinstance(
            action,
            (EndCallAction, TransferCallAction, SendDTMFAction, SendSMSAction),
        )

    async def execute(self, session: Any, action: SessionAction) -> SessionActionResult:
        call_control_id = _call_control_id_from(session)
        if not call_control_id:
            raise RuntimeError("Telnyx session actions require an active call_control_id")

        client = self._get_client()
        if isinstance(action, EndCallAction):
            await client.hangup(call_control_id)
            return SessionActionResult(
                stop_session=True,
                metadata={"call_control_id": call_control_id},
            )

        if isinstance(action, TransferCallAction):
            await client.transfer(
                call_control_id,
                action.target,
                from_=action.plan.caller_id or None,
            )
            return SessionActionResult(
                stop_session=True,
                metadata={"call_control_id": call_control_id, "target": action.target},
            )

        if isinstance(action, SendDTMFAction):
            digits = _apply_inter_digit_delay(action.digits, action.inter_digit_delay_ms)
            await client.send_dtmf(call_control_id, digits)
            return SessionActionResult(
                metadata={"call_control_id": call_control_id, "digits": digits}
            )

        if isinstance(action, SendSMSAction):
            if not self._config.sms_from_number:
                raise RuntimeError("Telnyx SMS actions require sms_from_number")
            message = await client.send_sms(
                to=action.to,
                from_=self._config.sms_from_number,
                text=action.body,
                connection_id=self._config.connection_id or None,
            )
            return SessionActionResult(
                metadata={
                    "message_id": _sms_message_id(message),
                    "to": action.to,
                }
            )

        raise RuntimeError(f"Unsupported Telnyx session action: {action.type}")

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._config.api_key:
            raise RuntimeError("Telnyx session actions require api_key")
        try:
            from easycat.telephony.telnyx_client import (
                TELNYX_API_BASE_URL,
                TelnyxCallControlClient,
            )
        except ImportError as exc:  # pragma: no cover - exercised via config tests
            raise RuntimeError(
                "The 'aiohttp' package is required for Telnyx session actions. "
                + "Install with: uv add 'easycat[telnyx]'. From the EasyCat repo, "
                "use: uv sync --extra telnyx --group dev."
            ) from exc
        self._client = TelnyxCallControlClient(self._config.api_key, base_url=TELNYX_API_BASE_URL)
        return self._client

    async def close(self) -> None:
        """Release the underlying HTTP session, if one was created."""
        client = self._client
        self._client = None
        if client is not None and hasattr(client, "close"):
            await client.close()


def _call_control_id_from(session: Any) -> str | None:
    transport = getattr(session, "transport", None)
    for attribute in ("call_control_id", "call_sid"):
        value = getattr(transport, attribute, None)
        if isinstance(value, str) and value:
            return value
    return None


def _sms_message_id(message: Any) -> str:
    data = getattr(message, "data", None) if not isinstance(message, dict) else message
    if isinstance(data, dict):
        record = data.get("data")
        if isinstance(record, dict):
            identifier = record.get("id") or record.get("sms_id")
            if isinstance(identifier, str):
                return identifier
    for attribute in ("id", "sms_id"):
        value = getattr(message, attribute, None)
        if isinstance(value, str):
            return value
    return ""
