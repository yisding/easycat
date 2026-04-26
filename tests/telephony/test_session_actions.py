"""Tests for telephony-backed session action executors."""

from __future__ import annotations

import base64
import hashlib
import hmac

import pytest

from easycat.session.actions import (
    SendDTMFAction,
    SendSMSAction,
    TransferCallAction,
    TransferPlan,
)
from easycat.telephony.session_actions import (
    TwilioSessionActionConfig,
    TwilioSessionActionExecutor,
)
from easycat.telephony.twiml import (
    compute_twilio_webhook_signature,
    validate_twilio_webhook_signature,
)
from easycat.transports.twilio_media import TwilioConnectionTransport


class _FakeCallUpdater:
    def __init__(self) -> None:
        self.updates: list[dict[str, str]] = []

    def update(self, **kwargs: str) -> None:
        self.updates.append(kwargs)


class _FakeCalls:
    def __init__(self) -> None:
        self.last_sid: str | None = None
        self.updater = _FakeCallUpdater()

    def __call__(self, sid: str) -> _FakeCallUpdater:
        self.last_sid = sid
        return self.updater


class _FakeMessages:
    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []

    def create(self, *, to: str, from_: str, body: str):
        self.requests.append({"to": to, "from_": from_, "body": body})
        return type("Message", (), {"sid": "SM123"})()


class _FakeTwilioClient:
    def __init__(self) -> None:
        self.calls = _FakeCalls()
        self.messages = _FakeMessages()


class _FakeTransport:
    call_sid = "CA123"


class _FakeSession:
    transport = _FakeTransport()


class _FakeWebSocket:
    async def send(self, _message: str) -> None:
        return None

    async def close(self) -> None:
        return None


def test_twilio_webhook_signature_validation_groups_duplicate_form_values() -> None:
    url = "https://voice.example.com/twiml?tenant=acme"
    params = [("Digits", "2"), ("CallSid", "CA123"), ("Digits", "1")]
    signed = url + "CallSidCA123Digits1Digits2"
    expected = base64.b64encode(
        hmac.new(b"token", signed.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")

    assert (
        compute_twilio_webhook_signature(
            auth_token="token",
            url=url,
            params=params,
        )
        == expected
    )
    assert validate_twilio_webhook_signature(
        auth_token="token",
        url=url,
        params=params,
        signature=expected,
    )
    assert not validate_twilio_webhook_signature(
        auth_token="token",
        url=url,
        params=params,
        signature="bad",
    )


@pytest.mark.asyncio
async def test_twilio_transfer_action_updates_call_with_twiml() -> None:
    client = _FakeTwilioClient()
    executor = TwilioSessionActionExecutor(TwilioSessionActionConfig(client=client))
    action = TransferCallAction(
        target="+15551234567",
        plan=TransferPlan(
            client_message="Connecting you now.",
            post_dial_digits="ww1234",
        ),
    )

    result = await executor.execute(_FakeSession(), action)

    assert result.stop_session is True
    assert client.calls.last_sid == "CA123"
    twiml = client.calls.updater.updates[0]["twiml"]
    assert "Connecting you now." in twiml
    assert "+15551234567" in twiml
    assert "ww1234" in twiml


@pytest.mark.asyncio
async def test_twilio_dtmf_action_inserts_delays() -> None:
    client = _FakeTwilioClient()
    executor = TwilioSessionActionExecutor(TwilioSessionActionConfig(client=client))
    action = SendDTMFAction(digits="123", inter_digit_delay_ms=1000)

    result = await executor.execute(_FakeSession(), action)

    assert result.metadata["digits"] == "1W2W3"
    assert 'digits="1W2W3"' in client.calls.updater.updates[0]["twiml"]


@pytest.mark.asyncio
async def test_twilio_connection_transport_session_action_uses_start_call_sid() -> None:
    client = _FakeTwilioClient()
    transport = TwilioConnectionTransport(_FakeWebSocket())
    await transport._handle_start(
        {
            "streamSid": "MZ123",
            "start": {
                "streamSid": "MZ123",
                "callSid": "CA_FROM_START",
                "customParameters": {"From": "+15551234567"},
            },
        }
    )
    session = type("Session", (), {"transport": transport})()
    executor = TwilioSessionActionExecutor(TwilioSessionActionConfig(client=client))

    result = await executor.execute(session, SendDTMFAction(digits="9"))

    assert transport.stream_sid == "MZ123"
    assert transport.call_sid == "CA_FROM_START"
    assert client.calls.last_sid == "CA_FROM_START"
    assert result.metadata["call_sid"] == "CA_FROM_START"
    assert 'digits="9"' in client.calls.updater.updates[0]["twiml"]


@pytest.mark.asyncio
async def test_twilio_sms_action_uses_configured_from_number() -> None:
    client = _FakeTwilioClient()
    executor = TwilioSessionActionExecutor(
        TwilioSessionActionConfig(client=client, sms_from_number="+15550001111")
    )

    result = await executor.execute(
        _FakeSession(),
        SendSMSAction(to="+15551112222", body="Here is your link."),
    )

    assert result.metadata["message_sid"] == "SM123"
    assert client.messages.requests == [
        {"to": "+15551112222", "from_": "+15550001111", "body": "Here is your link."}
    ]
