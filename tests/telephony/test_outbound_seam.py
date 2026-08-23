"""Tests for the OutboundCallClient seam and provider adapters."""

from __future__ import annotations

import types
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from easycat.events import (
    CallAnswered,
    CallInitiated,
    EventBus,
)
from easycat.telephony.outbound import (
    OutboundCallClient,
    OutboundCallManager,
    OutboundCallManagerState,
    TelnyxOutboundClient,
    TwilioRestOutboundClient,
    emit_telnyx_call_event,
    telnyx_dial_payload_from_create_kwargs,
)


@contextmanager
def _stub_twilio_sdk():
    """Install a minimal ``twilio.rest`` stub so offline tests can construct clients."""

    class _StubClient:
        def __init__(self, account_sid: str, auth_token: str) -> None:
            self.account_sid = account_sid
            self.auth_token = auth_token
            self.calls = SimpleNamespace(create=lambda **kwargs: SimpleNamespace(sid="CA-STUB"))

    rest_module = types.ModuleType("twilio.rest")
    rest_module.Client = _StubClient  # type: ignore[attr-defined]
    twilio_module = types.ModuleType("twilio")
    twilio_module.rest = rest_module  # type: ignore[attr-defined]
    modules = {"twilio": twilio_module, "twilio.rest": rest_module}
    import unittest.mock

    with unittest.mock.patch.dict("sys.modules", modules):
        yield _StubClient


class _RecordingUpdater:
    def __init__(self, resource: _FakeCallsResource, call_sid: str) -> None:
        self._resource = resource
        self._call_sid = call_sid

    def update(self, **kwargs: Any) -> Any:
        self._resource.updates.append((self._call_sid, kwargs))
        return SimpleNamespace(sid=self._call_sid)


class _FakeCallsResource:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def create(self, **kwargs: Any) -> Any:
        self.created.append(kwargs)
        return SimpleNamespace(sid=f"CA{len(self.created)}")

    def __call__(self, call_sid: str) -> _RecordingUpdater:
        return _RecordingUpdater(self, call_sid)


class _FakeOutboundClient:
    def __init__(self) -> None:
        self.calls = _FakeCallsResource()


class TestManagerDefaultsToTwilioClient:
    def test_default_client_is_twilio_rest_outbound_client(self) -> None:
        with _stub_twilio_sdk():
            manager = OutboundCallManager(
                EventBus(),
                from_number="+1555",
                twilio_account_sid="AC123",
                twilio_auth_token="token",
            )

        assert type(manager._client) is TwilioRestOutboundClient
        assert isinstance(manager._client, TwilioRestOutboundClient)

    def test_blank_twilio_credentials_still_raise_value_error(self) -> None:
        with (
            _stub_twilio_sdk(),
            pytest.raises(ValueError, match="twilio_account_sid and twilio_auth_token"),
        ):
            OutboundCallManager(EventBus(), from_number="+1555")

    def test_injected_client_skips_credential_requirement(self) -> None:
        client = _FakeOutboundClient()
        manager = OutboundCallManager(EventBus(), from_number="+1555", client=client)

        assert manager._client is client


class TestInjectedClientPlacement:
    @pytest.mark.asyncio
    async def test_place_call_and_hangup_drive_the_injected_client(self) -> None:
        bus = EventBus()
        client = _FakeOutboundClient()
        manager = OutboundCallManager(bus, from_number="+1999", client=client)
        manager.start()
        try:
            call_sid = await manager.place_call("+15559876543")

            assert call_sid == "CA1"
            assert client.calls.created[0]["to"] == "+15559876543"

            await manager.hangup_call(call_sid)

            assert ("CA1", {"status": "completed"}) in client.calls.updates
            assert manager.state is OutboundCallManagerState.IDLE
        finally:
            manager.stop()

    def test_fake_satisfies_runtime_checkable_protocol(self) -> None:
        assert isinstance(_FakeOutboundClient(), OutboundCallClient)
        with _stub_twilio_sdk():
            assert isinstance(TwilioRestOutboundClient("AC123", "token"), OutboundCallClient)


class TestTelnyxOutboundClientRouting:
    def _make_owner_client(self, fake: _FakeTelnyxControl) -> TelnyxOutboundClient:
        return TelnyxOutboundClient(
            "key-123",
            connection_id="conn-1",
            webhook_url="https://example.test/telnyx",
            client_factory=lambda: fake,
        )

    @pytest.mark.asyncio
    async def test_dial_routes_to_control_client(self) -> None:
        fake = _FakeTelnyxControl()
        client = self._make_owner_client(fake)

        response = await client.dial({"to": "+15550001111"})

        assert fake.dials == [{"to": "+15550001111"}]
        assert response == {"data": {"call_control_id": "CC-DIAL"}}
        assert fake.closed is True

    @pytest.mark.asyncio
    async def test_hangup_routes_to_control_client(self) -> None:
        fake = _FakeTelnyxControl()
        client = self._make_owner_client(fake)

        await client.hangup("CC1")

        assert fake.hangups == ["CC1"]

    def test_calls_facade_creates_call_and_returns_sid(self) -> None:
        fake = _FakeTelnyxControl()
        client = self._make_owner_client(fake)

        created = client.calls.create(
            to="+15550001111",
            from_="+15550002222",
            machine_detection="DetectMessageEnd",
            status_callback="https://example.test/status",
        )

        assert created.sid == "CC-DIAL"
        assert fake.dials[0]["to"] == "+15550001111"
        assert fake.dials[0]["from"] == "+15550002222"
        assert fake.dials[0]["connection_id"] == "conn-1"
        assert fake.dials[0]["answering_machine_detection"] == "greeting_end"
        assert fake.dials[0]["webhook_url"] == "https://example.test/telnyx"

    def test_calls_facade_update_maps_to_hangup(self) -> None:
        fake = _FakeTelnyxControl()
        client = self._make_owner_client(fake)

        client.calls("CC7").update(status="completed")

        assert fake.hangups == ["CC7"]

    def test_update_with_unsupported_kwarg_raises_type_error(self) -> None:
        client = self._make_owner_client(_FakeTelnyxControl())

        with pytest.raises(TypeError, match="does not support.*twiml"):
            client.calls("CC7").update(twiml="<Response><Dial>+1555</Dial></Response>")

    def test_update_with_non_completed_status_raises_value_error(self) -> None:
        client = self._make_owner_client(_FakeTelnyxControl())

        with pytest.raises(ValueError, match="only supports status='completed'"):
            client.calls("CC7").update(status="canceled")

    def test_update_without_kwargs_defaults_to_hangup(self) -> None:
        fake = _FakeTelnyxControl()
        client = self._make_owner_client(fake)

        client.calls("CC7").update()

        assert fake.hangups == ["CC7"]

    @pytest.mark.asyncio
    async def test_sync_create_from_async_context_raises_clear_error(self) -> None:
        fake = _FakeTelnyxControl()
        client = self._make_owner_client(fake)

        with pytest.raises(RuntimeError, match="sync facade.*owner\\.dial"):
            client.calls.create(to="+1", from_="+2")

    @pytest.mark.asyncio
    async def test_sync_update_from_async_context_raises_clear_error(self) -> None:
        fake = _FakeTelnyxControl()
        client = self._make_owner_client(fake)

        with pytest.raises(RuntimeError, match="sync facade.*owner\\.hangup"):
            client.calls("CC7").update(status="completed")

    def test_isinstance_outbound_call_client_protocol(self) -> None:
        assert isinstance(TelnyxOutboundClient("key", connection_id="c"), OutboundCallClient)


class _FakeTelnyxControl:
    def __init__(self) -> None:
        self.dials: list[dict[str, Any]] = []
        self.hangups: list[str] = []
        self.closed = False

    async def dial(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.dials.append(payload)
        return {"data": {"call_control_id": "CC-DIAL"}}

    async def hangup(self, call_control_id: str) -> dict[str, Any]:
        self.hangups.append(call_control_id)
        return {"data": {}}

    async def close(self) -> None:
        self.closed = True


class TestDialPayloadTranslation:
    def test_translates_shared_subset(self) -> None:
        payload = telnyx_dial_payload_from_create_kwargs(
            {
                "to": "+15550001111",
                "from_": "+15550002222",
                "url": "https://example.test/twiml",
                "machine_detection": "Enable",
                "async_amd": "true",
                "transcription": True,
            },
            connection_id="conn-9",
        )

        assert payload["to"] == "+15550001111"
        assert payload["from"] == "+15550002222"
        assert payload["connection_id"] == "conn-9"
        assert payload["answering_machine_detection"] == "detect"
        assert "transcription" not in payload
        assert "url" not in payload

    def test_status_callback_falls_back_for_webhook_url(self) -> None:
        payload = telnyx_dial_payload_from_create_kwargs(
            {"to": "+1", "from_": "+2", "status_callback": "https://cb.example"},
            connection_id="conn",
        )

        assert payload["webhook_url"] == "https://cb.example"

    def test_stream_url_expands_stream_parameters(self) -> None:
        payload = telnyx_dial_payload_from_create_kwargs(
            {"to": "+1", "from_": "+2", "stream_url": "wss://media.example/stream"},
            connection_id="conn",
        )

        assert payload["stream_url"] == "wss://media.example/stream"
        assert payload["stream_bidirectional_codec"] == "L16"
        assert payload["stream_bidirectional_sampling_rate"] == 16000

    @pytest.mark.parametrize("amd_mode", ["DetectMessageEnd", "Enable", "Disabled"])
    def test_amd_mode_mapping(self, amd_mode: str) -> None:
        payload = telnyx_dial_payload_from_create_kwargs(
            {"to": "+1", "from_": "+2", "machine_detection": amd_mode},
            connection_id="conn",
        )

        expected = {"DetectMessageEnd": "greeting_end", "Enable": "detect", "Disabled": "disabled"}
        assert payload["answering_machine_detection"] == expected[amd_mode]

    def test_empty_connection_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="connection_id"):
            telnyx_dial_payload_from_create_kwargs({"to": "+1", "from_": "+2"}, connection_id="")

    @pytest.mark.parametrize(
        "native_mode",
        ["premium", "detect", "detect_beep", "detect_words", "greeting_end", "disabled"],
    )
    def test_native_telnyx_amd_modes_pass_through(self, native_mode: str) -> None:
        payload = telnyx_dial_payload_from_create_kwargs(
            {"to": "+1", "from_": "+2", "machine_detection": native_mode},
            connection_id="conn",
        )

        assert payload["answering_machine_detection"] == native_mode

    @pytest.mark.parametrize(
        "native_mode",
        ["Premium", "DETECT_BEEP", "Detect_Words"],
    )
    def test_native_telnyx_amd_modes_are_case_insensitive(self, native_mode: str) -> None:
        payload = telnyx_dial_payload_from_create_kwargs(
            {"to": "+1", "from_": "+2", "machine_detection": native_mode},
            connection_id="conn",
        )

        assert payload["answering_machine_detection"] == native_mode.lower()

    def test_unknown_amd_mode_is_omitted(self) -> None:
        payload = telnyx_dial_payload_from_create_kwargs(
            {"to": "+1", "from_": "+2", "machine_detection": "not_a_real_mode"},
            connection_id="conn",
        )

        assert "answering_machine_detection" not in payload


class TestEmitTelnyxCallEvent:
    @pytest.mark.asyncio
    async def test_answered_envelope_emits_neutral_event(self) -> None:
        bus = EventBus()
        emitted: list[Any] = []
        bus.subscribe(CallAnswered, emitted.append)

        event = await emit_telnyx_call_event(
            {"event_type": "call.answered", "payload": {"call_control_id": "CC9"}},
            bus,
            session_id="session-1",
        )

        assert isinstance(event, CallAnswered)
        assert event.call_sid == "CC9"
        assert event.session_id == "session-1"
        assert len(emitted) == 1

    @pytest.mark.asyncio
    async def test_initiated_envelope_carries_parties(self) -> None:
        bus = EventBus()
        emitted: list[Any] = []
        bus.subscribe(CallInitiated, emitted.append)

        event = await emit_telnyx_call_event(
            {
                "event_type": "call.initiated",
                "payload": {"call_control_id": "CC1", "to": "+1555", "from": "+1999"},
            },
            bus,
        )

        assert isinstance(event, CallInitiated)
        assert (event.to, event.from_) == ("+1555", "+1999")

    @pytest.mark.asyncio
    async def test_unsupported_event_emits_nothing(self) -> None:
        bus = EventBus()

        event = await emit_telnyx_call_event({"event_type": "other.thing"}, bus)

        assert event is None
