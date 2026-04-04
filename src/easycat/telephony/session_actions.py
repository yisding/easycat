"""Telephony-backed session action executors."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from easycat.session.actions import (
    DTMFTarget,
    EndCallAction,
    SendDTMFAction,
    SendSMSAction,
    SessionAction,
    SessionActionExecutor,
    SessionActionResult,
    TransferCallAction,
    TransferMode,
)
from easycat.telephony.twiml import (
    twiml_dial_number,
    twiml_play_digits,
    twiml_say_and_hangup,
)

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
            if action.farewell:
                await self._update_call(
                    client,
                    call_sid,
                    twiml=twiml_say_and_hangup(action.farewell),
                )
            else:
                await self._update_call(client, call_sid, status="completed")
            return SessionActionResult(stop_session=True, metadata={"call_sid": call_sid})

        if isinstance(action, TransferCallAction):
            self._validate_transfer_plan(action)
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
            if action.target_leg is not DTMFTarget.REMOTE:
                raise RuntimeError(
                    f"Twilio DTMF only supports target_leg={DTMFTarget.REMOTE.value}"
                )
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
                "The 'twilio' package is required for Twilio session actions"
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

    def _validate_transfer_plan(self, action: TransferCallAction) -> None:
        supported_modes = {TransferMode.BLIND, TransferMode.WARM_MESSAGE}
        if action.plan.mode not in supported_modes:
            raise RuntimeError(
                f"Twilio executor does not support transfer mode {action.plan.mode}"
            )
        if action.plan.operator_message:
            raise RuntimeError("Twilio executor does not support operator_message yet")
        if action.plan.summary_prompt:
            raise RuntimeError("Twilio executor does not support summary_prompt yet")
        if action.plan.hold_audio_url:
            raise RuntimeError("Twilio executor does not support hold_audio_url yet")
        if action.plan.fallback_message:
            raise RuntimeError("Twilio executor does not support fallback_message yet")
        if action.plan.sip_headers:
            raise RuntimeError("Twilio executor does not support sip_headers yet")


def _apply_inter_digit_delay(digits: str, inter_digit_delay_ms: int) -> str:
    if inter_digit_delay_ms <= 0 or len(digits) <= 1:
        return digits
    pauses = max(1, round(inter_digit_delay_ms / 1000))
    separator = "W" * pauses
    return separator.join(digits)
