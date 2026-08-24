"""Tests for Telnyx-backed session action executors (offline, fakes only)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from easycat.session.actions import (
    MAX_DTMF_INTER_DIGIT_DELAY_MS,
    AddToDNCAction,
    EndCallAction,
    SendDTMFAction,
    SendSMSAction,
    TransferCallAction,
    TransferPlan,
)
from easycat.telephony.session_actions import (
    TelnyxSessionActionConfig,
    TelnyxSessionActionExecutor,
)


class _FakeTelnyxControlClient:
    def __init__(self) -> None:
        self.transfers: list[tuple[str, str, str | None]] = []
        self.dtmfs: list[tuple[str, str]] = []
        self.hangups: list[str] = []
        self.sms: list[dict[str, Any]] = []

    async def transfer(
        self,
        call_control_id: str,
        to: str,
        *,
        from_: str | None = None,
    ) -> dict[str, Any]:
        self.transfers.append((call_control_id, to, from_))
        return {"data": {}}

    async def send_dtmf(self, call_control_id: str, digits: str) -> dict[str, Any]:
        self.dtmfs.append((call_control_id, digits))
        return {"data": {}}

    async def hangup(self, call_control_id: str) -> dict[str, Any]:
        self.hangups.append(call_control_id)
        return {"data": {}}

    async def send_sms(
        self,
        *,
        to: str,
        from_: str,
        text: str,
        connection_id: str | None = None,
    ) -> dict[str, Any]:
        self.sms.append(
            {
                "to": to,
                "from_": from_,
                "text": text,
                "connection_id": connection_id,
            }
        )
        return {"data": {"id": "SMS-MSG-1"}}


@dataclass
class _FakeTransport:
    call_control_id: str | None = "CC123"
    call_sid: str | None = None


@dataclass
class _FakeSession:
    transport: Any = field(default_factory=_FakeTransport)


def _executor(
    client: _FakeTelnyxControlClient,
    **config_kwargs: Any,
) -> TelnyxSessionActionExecutor:
    return TelnyxSessionActionExecutor(TelnyxSessionActionConfig(client=client, **config_kwargs))


class TestTransferAction:
    @pytest.mark.asyncio
    async def test_transfer_hits_call_control_command(self) -> None:
        client = _FakeTelnyxControlClient()
        executor = _executor(client)
        action = TransferCallAction(
            target="+15551234567",
            plan=TransferPlan(caller_id="+15550009999"),
        )

        result = await executor.execute(_FakeSession(), action)

        assert result.stop_session is True
        assert client.transfers == [("CC123", "+15551234567", "+15550009999")]
        assert result.metadata == {"call_control_id": "CC123", "target": "+15551234567"}

    @pytest.mark.asyncio
    async def test_transfer_without_caller_id_passes_none(self) -> None:
        client = _FakeTelnyxControlClient()

        await _executor(client).execute(_FakeSession(), TransferCallAction(target="+15551234567"))

        assert client.transfers == [("CC123", "+15551234567", None)]


class TestDTMFAction:
    @pytest.mark.asyncio
    async def test_send_dtmf_hits_send_dtmf_command(self) -> None:
        client = _FakeTelnyxControlClient()

        result = await _executor(client).execute(_FakeSession(), SendDTMFAction(digits="9"))

        assert client.dtmfs == [("CC123", "9")]
        assert result.metadata == {"call_control_id": "CC123", "digits": "9"}
        assert result.stop_session is False

    @pytest.mark.asyncio
    async def test_send_dtmf_inserts_inter_digit_delays(self) -> None:
        client = _FakeTelnyxControlClient()

        result = await _executor(client).execute(
            _FakeSession(), SendDTMFAction(digits="123", inter_digit_delay_ms=1000)
        )

        assert result.metadata["digits"] == "1W2W3"
        assert client.dtmfs == [("CC123", "1W2W3")]

    @pytest.mark.asyncio
    async def test_send_dtmf_delay_clamped_to_max(self) -> None:
        with pytest.raises(ValueError):
            SendDTMFAction(digits="12", inter_digit_delay_ms=MAX_DTMF_INTER_DIGIT_DELAY_MS + 1)


class TestEndCallAction:
    @pytest.mark.asyncio
    async def test_end_call_hits_hangup_command(self) -> None:
        client = _FakeTelnyxControlClient()

        result = await _executor(client).execute(_FakeSession(), EndCallAction())

        assert client.hangups == ["CC123"]
        assert result.stop_session is True
        assert result.metadata == {"call_control_id": "CC123"}


class TestSMSAction:
    @pytest.mark.asyncio
    async def test_sms_posts_to_messages_endpoint_payload(self) -> None:
        client = _FakeTelnyxControlClient()
        executor = _executor(client, sms_from_number="+15550001111", connection_id="conn-1")

        result = await executor.execute(
            _FakeSession(), SendSMSAction(to="+15551112222", body="Here is your link.")
        )

        assert result.metadata["message_id"] == "SMS-MSG-1"
        assert result.metadata["to"] == "+15551112222"
        assert client.sms == [
            {
                "to": "+15551112222",
                "from_": "+15550001111",
                "text": "Here is your link.",
                "connection_id": "conn-1",
            }
        ]

    @pytest.mark.asyncio
    async def test_sms_requires_configured_from_number(self) -> None:
        executor = _executor(_FakeTelnyxControlClient())

        with pytest.raises(RuntimeError, match="sms_from_number"):
            await executor.execute(_FakeSession(), SendSMSAction(to="+15551112222", body="hi"))


class TestErrorParity:
    @pytest.mark.asyncio
    async def test_missing_call_identity_raises_runtime_error(self) -> None:
        executor = _executor(_FakeTelnyxControlClient())

        with pytest.raises(RuntimeError, match="call_control_id"):
            await executor.execute(_FakeSession(transport=_FakeTransport(None)), EndCallAction())

    @pytest.mark.asyncio
    async def test_falls_back_to_transport_call_sid(self) -> None:
        client = _FakeTelnyxControlClient()
        transport = _FakeTransport(None)
        transport.call_sid = "CA-FALLBACK"

        await _executor(client).execute(_FakeSession(transport=transport), EndCallAction())

        assert client.hangups == ["CA-FALLBACK"]

    def test_missing_api_key_and_client_raises_runtime_error(self) -> None:
        executor = TelnyxSessionActionExecutor(TelnyxSessionActionConfig(api_key=""))

        with pytest.raises(RuntimeError, match="api_key"):
            executor._get_client()

    @pytest.mark.asyncio
    async def test_unsupported_action_raises_runtime_error(self) -> None:
        executor = _executor(_FakeTelnyxControlClient())

        with pytest.raises(RuntimeError, match="Unsupported Telnyx session action"):
            await executor.execute(_FakeSession(), AddToDNCAction(number="+15550003333"))

    def test_supports_matches_twilio_executor_surface(self) -> None:
        executor = _executor(_FakeTelnyxControlClient())

        assert executor.supports(EndCallAction())
        assert executor.supports(TransferCallAction())
        assert executor.supports(SendDTMFAction(digits="1"))
        assert executor.supports(SendSMSAction(to="+1", body="x"))
        assert not executor.supports(AddToDNCAction(number="+15550003333"))

    @pytest.mark.asyncio
    async def test_close_releases_created_client_once(self) -> None:
        client = _FakeTelnyxControlClient()
        closes = 0

        async def _close() -> None:
            nonlocal closes
            closes += 1

        client.close = _close  # type: ignore[method-assign]
        executor = _executor(client)

        await executor.close()
        await executor.close()

        assert closes == 1
