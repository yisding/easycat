"""Chapter 10 — Exercise EasyCat's Twilio boundaries without live credentials.

Dependencies:
    uv sync --group dev

Run:
    uv run python docs/using-easycat/10-telephony/main.py
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from easycat.events import CallAnswered, CallEnded
from easycat.session.actions import (
    EndCallAction,
    SendDTMFAction,
    TransferCallAction,
    TransferPlan,
)
from easycat.telephony import (
    TwilioSessionActionConfig,
    TwilioSessionActionExecutor,
    compute_twilio_webhook_signature,
    parse_call_status_callback,
    parse_gather_webhook,
    twilio_stream_parameters_from_form,
    twiml_gather,
    validate_twilio_webhook_signature,
)
from easycat.telephony.ivr import classify_ivr_prompt
from easycat.telephony.screening import match_screening_platform
from easycat.transports import TwilioStreamTokenStore
from easycat.transports.twilio_media import twiml_connect_stream

AUTH_TOKEN = "chapter-10-test-auth-token"
CALL_SID = "CA_CHAPTER_10"


class FakeCallUpdater:
    def __init__(self) -> None:
        self.updates: list[dict[str, str]] = []

    def update(self, **kwargs: str) -> None:
        self.updates.append(kwargs)


class FakeCalls:
    def __init__(self) -> None:
        self.last_sid: str | None = None
        self.updater = FakeCallUpdater()

    def __call__(self, call_sid: str) -> FakeCallUpdater:
        self.last_sid = call_sid
        return self.updater


class FakeTwilioClient:
    def __init__(self) -> None:
        self.calls = FakeCalls()


class FakeTransport:
    call_sid = CALL_SID


class FakeSession:
    transport = FakeTransport()


def check_signed_media_handoff() -> None:
    url = "https://voice.example.test/twiml"
    form = {
        "CallSid": CALL_SID,
        "From": "+15551234567",
        "To": "+15557654321",
    }
    signature = compute_twilio_webhook_signature(
        auth_token=AUTH_TOKEN,
        url=url,
        params=form,
    )
    assert validate_twilio_webhook_signature(
        auth_token=AUTH_TOKEN,
        url=url,
        params=form,
        signature=signature,
    )
    assert not validate_twilio_webhook_signature(
        auth_token=AUTH_TOKEN,
        url=url,
        params=form,
        signature="wrong",
    )

    token_store = TwilioStreamTokenStore("chapter-10-stream-secret")
    stream_token = token_store.issue()
    parameters = twilio_stream_parameters_from_form(form)
    xml = twiml_connect_stream(
        "wss://voice.example.test/media",
        parameters=parameters,
        stream_token=stream_token,
        forward_caller_id=True,
    )
    assert "<Connect>" in xml and "EasyCatStreamToken" in xml
    assert parameters["Direction"] == "inbound"
    assert token_store.consume(stream_token) is True
    assert token_store.consume(stream_token) is False
    print("PASS handoff: signed webhook minted one-use media authorization")


def check_callbacks_and_classifiers() -> None:
    gather_xml = twiml_gather(
        action_url="https://voice.example.test/gather",
        num_digits=4,
        say_text="Enter your four digit account code.",
    )
    digits = [event.digit for event in parse_gather_webhook({"Digits": "12#X"})]
    assert "<Gather" in gather_xml and digits == ["1", "2", "#"]

    answered = parse_call_status_callback(
        {"CallStatus": "in-progress", "CallSid": CALL_SID, "AnsweredBy": "human"}
    )
    ended = parse_call_status_callback(
        {"CallStatus": "completed", "CallSid": CALL_SID, "CallDuration": "42"}
    )
    assert isinstance(answered, CallAnswered) and answered.call_sid == CALL_SID
    assert isinstance(ended, CallEnded) and ended.duration_s == 42.0

    screening = match_screening_platform("Please record your name and reason for calling")
    is_ivr = classify_ivr_prompt("Press 1 for sales, 2 for support")
    assert screening == "ios" and is_ivr is True
    print("PASS callbacks: DTMF, status, screening, and IVR inputs classified")


async def check_call_control() -> None:
    client = FakeTwilioClient()
    executor = TwilioSessionActionExecutor(TwilioSessionActionConfig(client=client))
    session = FakeSession()

    async def run_fake_inline(function: Any, /, *args: Any, **kwargs: Any) -> Any:
        return function(*args, **kwargs)

    # Production dispatches the blocking Twilio SDK through asyncio.to_thread.
    # This fake is synchronous and intentionally runs inline so the checkpoint
    # also works in sandboxes that forbid worker-thread creation.
    with patch.object(asyncio, "to_thread", run_fake_inline):
        dtmf_result = await executor.execute(
            session,
            SendDTMFAction(digits="12#", inter_digit_delay_ms=1000),
        )
        transfer_result = await executor.execute(
            session,
            TransferCallAction(
                target="+15559876543",
                plan=TransferPlan(
                    client_message="Connecting you now.",
                    post_dial_digits="ww42#",
                ),
            ),
        )
        end_result = await executor.execute(session, EndCallAction(reason="demo complete"))

    updates: list[dict[str, Any]] = client.calls.updater.updates
    assert client.calls.last_sid == CALL_SID
    assert 'digits="1W2W#"' in updates[0]["twiml"]
    assert "Connecting you now." in updates[1]["twiml"]
    assert updates[2] == {"status": "completed"}
    assert dtmf_result.stop_session is False
    assert transfer_result.stop_session is end_result.stop_session is True
    print("PASS actions: DTMF, transfer, and hangup mapped to Twilio updates")


async def checkpoint() -> None:
    check_signed_media_handoff()
    check_callbacks_and_classifiers()
    await check_call_control()


def main() -> None:
    asyncio.run(checkpoint())


if __name__ == "__main__":
    main()
