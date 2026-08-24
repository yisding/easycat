"""Telephony-backed session action executors."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

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
from easycat.telephony.telnyx_client import TELNYX_API_BASE_URL, TelnyxCallControlClient
from easycat.telephony.twiml import twiml_dial_number, twiml_play_digits

logger = logging.getLogger(__name__)


@dataclass
class TwilioSessionActionConfig:
    """Configuration for Twilio-backed session action execution."""

    account_sid: str = ""
    auth_token: str = field(default="", repr=False)
    sms_from_number: str = ""
    client: Any = None


class TwilioSessionActionExecutor(SessionActionExecutor):
    """Execute session actions by updating the active Twilio call."""

    def __init__(self, config: TwilioSessionActionConfig) -> None:
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

        client = self._get_client()
        if isinstance(action, EndCallAction):
            await self._update_call(client, call_sid, status="completed")
            return SessionActionResult(stop_session=True, metadata={"call_sid": call_sid})

        if isinstance(action, TransferCallAction):
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
            digits = _apply_inter_digit_delay(action.digits, action.inter_digit_delay_ms)
            await self._update_call(client, call_sid, twiml=twiml_play_digits(digits))
            return SessionActionResult(metadata={"call_sid": call_sid, "digits": digits})

        if isinstance(action, SendSMSAction):
            if not self._config.sms_from_number:
                raise RuntimeError("Twilio SMS actions require sms_from_number")
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
