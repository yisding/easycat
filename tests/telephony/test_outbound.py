"""Tests for outbound call manager: status callback parsing, emission, and manager lifecycle."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from easycat.events import (
    CallAnswered,
    CallEnded,
    CallFailed,
    CallInitiated,
    CallRinging,
    EventBus,
    VoicemailDetected,
)
from easycat.telephony.outbound import (
    OutboundCallManager,
    OutboundCallManagerState,
    emit_call_status,
    parse_call_status_callback,
)


class TestParseCallStatusCallback:
    def test_initiated_status(self) -> None:
        result = parse_call_status_callback(
            {"CallStatus": "initiated", "CallSid": "CA123", "To": "+1555", "From": "+1999"}
        )
        assert isinstance(result, CallInitiated)
        assert result.call_sid == "CA123"
        assert result.to == "+1555"
        assert result.from_ == "+1999"

    def test_ringing_status(self) -> None:
        result = parse_call_status_callback({"CallStatus": "ringing", "CallSid": "CA123"})
        assert isinstance(result, CallRinging)
        assert result.call_sid == "CA123"

    def test_answered_status(self) -> None:
        result = parse_call_status_callback({"CallStatus": "in-progress", "CallSid": "CA123"})
        assert isinstance(result, CallAnswered)
        assert result.call_sid == "CA123"

    def test_completed_status(self) -> None:
        result = parse_call_status_callback(
            {
                "CallStatus": "completed",
                "CallSid": "CA123",
                "Duration": "45",
                "To": "+1555",
                "From": "+1999",
            }
        )
        assert isinstance(result, CallEnded)
        assert result.duration_s == 45.0
        assert result.disposition == "completed"
        assert result.number == "+1999"

    def test_completed_status_ignores_malformed_duration(self) -> None:
        result = parse_call_status_callback(
            {"CallStatus": "completed", "CallSid": "CA123", "Duration": "not-a-number"}
        )
        assert isinstance(result, CallEnded)
        assert result.duration_s is None

    def test_busy_status(self) -> None:
        result = parse_call_status_callback(
            {"CallStatus": "busy", "CallSid": "CA123", "To": "+1555", "From": "+1999"}
        )
        assert isinstance(result, CallFailed)
        assert result.reason == "busy"
        assert result.number == "+1999"

    def test_no_answer_status(self) -> None:
        result = parse_call_status_callback({"CallStatus": "no-answer", "CallSid": "CA123"})
        assert isinstance(result, CallFailed)
        assert result.reason == "no-answer"

    def test_failed_status(self) -> None:
        result = parse_call_status_callback({"CallStatus": "failed", "CallSid": "CA123"})
        assert isinstance(result, CallFailed)
        assert result.reason == "failed"

    def test_canceled_status(self) -> None:
        result = parse_call_status_callback({"CallStatus": "canceled", "CallSid": "CA123"})
        assert isinstance(result, CallFailed)
        assert result.reason == "canceled"

    def test_missing_call_status(self) -> None:
        result = parse_call_status_callback({"CallSid": "CA123"})
        assert result is None

    def test_missing_call_sid(self) -> None:
        result = parse_call_status_callback({"CallStatus": "ringing"})
        assert result is None

    def test_unknown_status(self) -> None:
        result = parse_call_status_callback({"CallStatus": "something_new", "CallSid": "CA123"})
        assert result is None

    def test_sip_response_code_607_blocked(self) -> None:
        result = parse_call_status_callback(
            {"CallStatus": "failed", "CallSid": "CA123", "SipResponseCode": "607"}
        )
        assert isinstance(result, CallFailed)
        assert result.reason == "blocked_unwanted"
        assert result.sip_code == 607

    def test_sip_response_code_608_rejected(self) -> None:
        result = parse_call_status_callback(
            {"CallStatus": "failed", "CallSid": "CA123", "SipResponseCode": "608"}
        )
        assert isinstance(result, CallFailed)
        assert result.reason == "blocked_rejected"
        assert result.sip_code == 608

    def test_sip_response_code_603_declined(self) -> None:
        result = parse_call_status_callback(
            {"CallStatus": "failed", "CallSid": "CA123", "SipResponseCode": "603"}
        )
        assert isinstance(result, CallFailed)
        assert result.reason == "declined"
        assert result.sip_code == 603

    def test_malformed_sip_response_code_falls_back_to_status(self) -> None:
        result = parse_call_status_callback(
            {"CallStatus": "failed", "CallSid": "CA123", "SipResponseCode": "nope"}
        )
        assert isinstance(result, CallFailed)
        assert result.reason == "failed"
        assert result.sip_code is None


class TestEmitCallStatus:
    @pytest.mark.asyncio
    async def test_emits_to_bus(self) -> None:
        bus = EventBus()
        received: list[CallRinging] = []
        bus.subscribe(CallRinging, received.append)
        result = await emit_call_status({"CallStatus": "ringing", "CallSid": "CA1"}, bus)
        assert isinstance(result, CallRinging)
        assert len(received) == 1
        assert received[0].call_sid == "CA1"

    @pytest.mark.asyncio
    async def test_skips_unparseable(self) -> None:
        bus = EventBus()
        received: list[object] = []
        bus.subscribe_all(received.append)
        result = await emit_call_status({"invalid": "data"}, bus)
        assert result is None
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_emits_amd_voicemail_detected(self) -> None:
        bus = EventBus()
        received: list[VoicemailDetected] = []
        bus.subscribe(VoicemailDetected, received.append)
        result = await emit_call_status(
            {"CallStatus": "in-progress", "CallSid": "CA1", "AnsweredBy": "machine_start"},
            bus,
        )
        assert isinstance(result, CallAnswered)
        assert len(received) == 1
        assert received[0].result == "machine"
        assert received[0].call_sid == "CA1"

    @pytest.mark.asyncio
    async def test_terminal_callback_for_sid_evicted_call_does_not_double_release(self) -> None:
        """An outbound terminal callback for an SID-evicted call must not double-release.

        The ``completed``/``busy`` callbacks now parse ``number=From``. When a
        :class:`NumberHealthMonitor` has already evicted (and decremented) an SID
        because tracking exceeded its cap, replaying a late terminal callback for
        that SID through ``emit_call_status`` must not decrement the caller bucket
        below the true live count — the eviction tombstone short-circuits it.
        """
        from easycat.telephony.number_health import NumberHealthMonitor

        bus = EventBus()
        monitor = NumberHealthMonitor(
            bus,
            max_concurrent_per_number=100,
            max_calls_per_minute=100,
            min_inter_call_delay_s=0.0,
        )
        monitor._max_sid_tracking = 4
        from_number = "+1999"
        to_number = "+1555"
        monitor.start()
        try:
            # Five initiated callbacks on the same caller ID drive SID tracking
            # over the cap (5 > 4); the 2 oldest SIDs are evicted, leaving the
            # bucket at the true live count of 3 (CA2, CA3, CA4).
            for i in range(5):
                await emit_call_status(
                    {
                        "CallStatus": "initiated",
                        "CallSid": f"CA{i}",
                        "To": to_number,
                        "From": from_number,
                    },
                    bus,
                )
            assert monitor._concurrent[from_number] == 3

            # Late completed callback (carrying number=From) for evicted CA0 must
            # not drop the bucket below the live count.
            await emit_call_status(
                {
                    "CallStatus": "completed",
                    "CallSid": "CA0",
                    "Duration": "5",
                    "To": to_number,
                    "From": from_number,
                },
                bus,
            )
            assert monitor._concurrent[from_number] == 3
        finally:
            monitor.stop()


class TestOutboundCallManager:
    def test_twilio_sdk_import_error(self) -> None:
        bus = EventBus()
        with patch.dict("sys.modules", {"twilio": None, "twilio.rest": None}):
            with pytest.raises(ImportError, match="easycat\\[telephony\\]"):
                OutboundCallManager(bus, from_number="+1555")

    @patch("easycat.telephony.outbound.OutboundCallManager.__init__", return_value=None)
    def test_init_stores_config(self, mock_init: MagicMock) -> None:
        manager = OutboundCallManager.__new__(OutboundCallManager)
        manager._state = OutboundCallManagerState.IDLE
        manager._active_call_sid = None
        manager._started = False
        assert manager.state == OutboundCallManagerState.IDLE
        assert manager.active_call_sid is None

    @patch("easycat.telephony.outbound.OutboundCallManager.__init__", return_value=None)
    def test_start_stop_idempotent(self, mock_init: MagicMock) -> None:
        manager = OutboundCallManager.__new__(OutboundCallManager)
        manager._event_bus = EventBus()
        manager._state = OutboundCallManagerState.IDLE
        manager._active_call_sid = None
        manager._started = False
        manager.start()
        manager.start()
        assert manager._started is True
        manager.stop()
        manager.stop()
        assert manager._started is False

    @patch("easycat.telephony.outbound.OutboundCallManager.__init__", return_value=None)
    def test_stop_resets_state(self, mock_init: MagicMock) -> None:
        manager = OutboundCallManager.__new__(OutboundCallManager)
        manager._event_bus = EventBus()
        manager._state = OutboundCallManagerState.ACTIVE
        manager._active_call_sid = "CA1"
        manager._started = True
        manager.stop()
        assert manager.state == OutboundCallManagerState.IDLE
        assert manager.active_call_sid is None
        assert manager._started is False

    @patch("easycat.telephony.outbound.OutboundCallManager.__init__", return_value=None)
    def test_active_call_sid_is_read_only(self, mock_init: MagicMock) -> None:
        manager = OutboundCallManager.__new__(OutboundCallManager)
        manager._active_call_sid = None
        with pytest.raises(AttributeError):
            manager.active_call_sid = "CA1"  # type: ignore[misc]


class TestOutboundCallManagerPlaceCall:
    def _make_manager(self, bus: EventBus) -> OutboundCallManager:
        manager = OutboundCallManager.__new__(OutboundCallManager)
        manager._event_bus = bus
        manager._from_number = "+15559876543"
        manager._amd_mode = "DetectMessageEnd"
        manager._async_amd = True
        manager._amd_timeout = 30
        manager._speech_threshold = 2400
        manager._speech_end_threshold = 1200
        manager._silence_timeout = 5000
        manager._enable_realtime_transcription = True
        manager._status_callback_url = "https://example.com/status"
        manager._twiml_url = "https://example.com/twiml"
        manager._client = MagicMock()
        manager._state = OutboundCallManagerState.IDLE
        manager._active_call_sid = None
        manager._started = True
        manager.dnc_list = None
        manager.compliance_check = None
        manager.retry_strategy = None
        return manager

    @pytest.mark.asyncio
    async def test_place_call_emits_initiated(self) -> None:
        bus = EventBus()
        received: list[CallInitiated] = []
        bus.subscribe(CallInitiated, received.append)
        manager = self._make_manager(bus)
        mock_call = MagicMock()
        mock_call.sid = "CA999"
        manager._client.calls.create.return_value = mock_call
        await manager.place_call("+15551234567")
        assert len(received) == 1
        assert received[0].call_sid == "CA999"
        assert received[0].to == "+15551234567"
        assert received[0].from_ == "+15559876543"
        assert manager.state == OutboundCallManagerState.ACTIVE
        assert manager.active_call_sid == "CA999"

    @pytest.mark.asyncio
    async def test_place_call_configures_amd(self) -> None:
        bus = EventBus()
        manager = self._make_manager(bus)
        mock_call = MagicMock()
        mock_call.sid = "CA999"
        manager._client.calls.create.return_value = mock_call
        await manager.place_call("+15551234567")
        kwargs = manager._client.calls.create.call_args.kwargs
        assert kwargs["machine_detection"] == "DetectMessageEnd"
        assert kwargs["async_amd"] == "true"

    @pytest.mark.asyncio
    async def test_place_call_configures_transcription(self) -> None:
        bus = EventBus()
        manager = self._make_manager(bus)
        mock_call = MagicMock()
        mock_call.sid = "CA999"
        manager._client.calls.create.return_value = mock_call
        await manager.place_call("+15551234567")
        kwargs = manager._client.calls.create.call_args.kwargs
        assert kwargs["transcription"] is True
        assert kwargs["transcription_track"] == "inbound_track"

    @pytest.mark.asyncio
    async def test_place_call_uses_from_number(self) -> None:
        bus = EventBus()
        manager = self._make_manager(bus)
        mock_call = MagicMock()
        mock_call.sid = "CA999"
        manager._client.calls.create.return_value = mock_call
        await manager.place_call("+15551234567")
        kwargs = manager._client.calls.create.call_args.kwargs
        assert kwargs["from_"] == "+15559876543"

    @pytest.mark.asyncio
    async def test_place_call_returns_call_sid(self) -> None:
        bus = EventBus()
        manager = self._make_manager(bus)
        mock_call = MagicMock()
        mock_call.sid = "CA999"
        manager._client.calls.create.return_value = mock_call
        result = await manager.place_call("+15551234567")
        assert result == "CA999"

    @pytest.mark.asyncio
    async def test_place_call_failure_emits_call_failed(self) -> None:
        bus = EventBus()
        received: list[CallFailed] = []
        bus.subscribe(CallFailed, received.append)
        manager = self._make_manager(bus)
        manager._client.calls.create.side_effect = RuntimeError("network error")
        with pytest.raises(RuntimeError, match="network error"):
            await manager.place_call("+15551234567")
        assert len(received) == 1
        assert "network error" in received[0].reason

    @pytest.mark.asyncio
    async def test_place_call_blocks_dnc_numbers(self) -> None:
        from easycat.telephony.compliance import DNCList

        bus = EventBus()
        manager = self._make_manager(bus)
        dnc = DNCList()
        dnc.add("+15551234567")
        manager.dnc_list = dnc
        with pytest.raises(ValueError, match="DNC"):
            await manager.place_call("+15551234567")
        manager._client.calls.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_place_call_honors_compliance_check(self) -> None:
        bus = EventBus()
        manager = self._make_manager(bus)
        manager.compliance_check = lambda _to: False
        with pytest.raises(ValueError, match="compliance_check"):
            await manager.place_call("+15551234567")
        manager._client.calls.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_place_call_requires_start(self) -> None:
        bus = EventBus()
        manager = self._make_manager(bus)
        manager._started = False
        with pytest.raises(RuntimeError, match="started"):
            await manager.place_call("+15551234567")
        manager._client.calls.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_place_call_requires_idle(self) -> None:
        bus = EventBus()
        manager = self._make_manager(bus)
        manager._state = OutboundCallManagerState.ACTIVE
        manager._active_call_sid = "CA999"
        with pytest.raises(RuntimeError, match="active call"):
            await manager.place_call("+15551234567")
        manager._client.calls.create.assert_not_called()


class TestOutboundCallManagerStatusTracking:
    def _make_manager(self, bus: EventBus) -> OutboundCallManager:
        manager = OutboundCallManager.__new__(OutboundCallManager)
        manager._event_bus = bus
        manager._client = MagicMock()
        manager._state = OutboundCallManagerState.IDLE
        manager._active_call_sid = None
        manager._started = False
        return manager

    @pytest.mark.asyncio
    async def test_status_events_track_active_call(self) -> None:
        bus = EventBus()
        manager = self._make_manager(bus)
        manager.start()
        await bus.emit(CallRinging(call_sid="CA1"))
        assert manager.state == OutboundCallManagerState.ACTIVE
        assert manager.active_call_sid == "CA1"
        await bus.emit(CallAnswered(call_sid="CA1"))
        assert manager.state == OutboundCallManagerState.ACTIVE
        assert manager.active_call_sid == "CA1"
        await bus.emit(CallEnded(call_sid="CA1"))
        assert manager.state == OutboundCallManagerState.IDLE
        assert manager.active_call_sid is None

    @pytest.mark.asyncio
    async def test_failed_event_clears_active_call(self) -> None:
        bus = EventBus()
        manager = self._make_manager(bus)
        manager.start()
        await bus.emit(CallRinging(call_sid="CA1"))
        await bus.emit(CallFailed(call_sid="CA1", reason="busy"))
        assert manager.state == OutboundCallManagerState.IDLE
        assert manager.active_call_sid is None

    @pytest.mark.asyncio
    async def test_ignores_terminal_event_for_different_call(self) -> None:
        bus = EventBus()
        manager = self._make_manager(bus)
        manager.start()
        await bus.emit(CallRinging(call_sid="CA1"))
        await bus.emit(CallEnded(call_sid="CA2"))
        assert manager.state == OutboundCallManagerState.ACTIVE
        assert manager.active_call_sid == "CA1"

    @pytest.mark.asyncio
    async def test_stop_unsubscribes_from_status_events(self) -> None:
        bus = EventBus()
        manager = self._make_manager(bus)
        manager.start()
        manager.stop()
        await bus.emit(CallRinging(call_sid="CA1"))
        assert manager.state == OutboundCallManagerState.IDLE
        assert manager.active_call_sid is None

    def test_stop_does_not_call_twilio_rest(self) -> None:
        bus = EventBus()
        manager = self._make_manager(bus)
        manager._active_call_sid = "CA1"
        manager._state = OutboundCallManagerState.ACTIVE
        manager.start()
        manager._active_call_sid = "CA1"
        manager._state = OutboundCallManagerState.ACTIVE
        manager.stop()
        manager._client.calls.assert_not_called()

    @pytest.mark.asyncio
    async def test_hangup_call_uses_twilio_rest_async_api(self) -> None:
        bus = EventBus()
        manager = self._make_manager(bus)
        manager._active_call_sid = "CA1"
        manager._state = OutboundCallManagerState.ACTIVE
        call_resource = MagicMock()
        manager._client.calls.return_value = call_resource
        await manager.hangup_call()
        manager._client.calls.assert_called_once_with("CA1")
        call_resource.update.assert_called_once_with(status="completed")
        assert manager.state == OutboundCallManagerState.IDLE
        assert manager.active_call_sid is None
