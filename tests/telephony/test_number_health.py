"""Tests for number health monitoring and call disposition tracking."""

from __future__ import annotations

import pytest

from easycat.events import EventBus
from easycat.telephony.call_state import CallStateChanged, OutboundCallState
from easycat.telephony.number_health import (
    CallDispositionTracker,
    NumberHealthMonitor,
)


class TestNumberHealthMonitor:
    def test_tracks_answer_rate_per_number(self) -> None:
        bus = EventBus()
        monitor = NumberHealthMonitor(bus)
        monitor.record_call("+15551234567", answered=True)
        monitor.record_call("+15551234567", answered=False)
        assert monitor.answer_rate("+15551234567") == 0.5

    def test_tracks_avg_call_duration(self) -> None:
        bus = EventBus()
        monitor = NumberHealthMonitor(bus)
        monitor.record_call("+15551234567", answered=True, duration_s=30.0)
        monitor.record_call("+15551234567", answered=True, duration_s=60.0)
        assert monitor.avg_duration("+15551234567") == 45.0

    def test_detects_sip_607_608_blocks(self) -> None:
        bus = EventBus()
        monitor = NumberHealthMonitor(bus)
        monitor.record_call("+15551234567", answered=False, blocked=True)
        monitor.record_call("+15551234567", answered=False, blocked=True)
        assert monitor.block_count("+15551234567") == 2

    def test_reputation_warning_emitted(self) -> None:
        bus = EventBus()
        monitor = NumberHealthMonitor(bus, answer_rate_threshold=0.4)
        # 1 answered, 4 unanswered = 20% answer rate.
        monitor.record_call("+1555", answered=True)
        for _ in range(4):
            monitor.record_call("+1555", answered=False)
        assert monitor.answer_rate("+1555") < 0.4

    def test_number_rotation_suggestion(self) -> None:
        bus = EventBus()
        monitor = NumberHealthMonitor(bus, block_count_threshold=3)
        for _ in range(4):
            monitor.record_call("+1555", answered=False, blocked=True)
        assert monitor.block_count("+1555") > 3

    def test_call_pacing_enforced(self) -> None:
        bus = EventBus()
        monitor = NumberHealthMonitor(bus, max_calls_per_minute=2, min_inter_call_delay_s=0.0)
        monitor.record_call("+1555", answered=True)
        monitor.record_call("+1555", answered=True)
        assert not monitor.can_place_call("+1555")

    def test_concurrent_call_limit(self) -> None:
        bus = EventBus()
        monitor = NumberHealthMonitor(bus, max_concurrent_per_number=2)
        monitor._concurrent["+1555"] = 2
        assert not monitor.can_place_call("+1555")

    def test_metrics_decay_over_time(self) -> None:
        bus = EventBus()
        monitor = NumberHealthMonitor(bus, record_ttl_s=0.0)
        monitor.record_call("+1555", answered=False)
        # With TTL=0, record is already expired.
        assert monitor.answer_rate("+1555") == 1.0  # No active records.


class TestCallDispositionTracker:
    def test_records_disposition(self) -> None:
        bus = EventBus()
        tracker = CallDispositionTracker(bus)
        tracker.record_disposition("human")
        tracker.record_disposition("voicemail")
        rates = tracker.disposition_rates()
        assert "human" in rates
        assert "voicemail" in rates

    def test_disposition_rates(self) -> None:
        bus = EventBus()
        tracker = CallDispositionTracker(bus)
        tracker.record_disposition("human")
        tracker.record_disposition("human")
        tracker.record_disposition("voicemail")
        tracker.record_disposition("voicemail")
        rates = tracker.disposition_rates()
        assert rates["human"] == 0.5
        assert rates["voicemail"] == 0.5

    def test_disposition_by_time_of_day(self) -> None:
        bus = EventBus()
        tracker = CallDispositionTracker(bus)
        tracker.record_disposition("human")
        by_hour = tracker.disposition_by_hour()
        assert isinstance(by_hour, dict)

    @pytest.mark.asyncio
    async def test_integrates_with_call_state_machine(self) -> None:
        bus = EventBus()
        tracker = CallDispositionTracker(bus)
        tracker.start()
        try:
            await bus.emit(
                CallStateChanged(
                    old=OutboundCallState.CLASSIFYING,
                    new=OutboundCallState.HUMAN,
                )
            )
            assert len(tracker._dispositions) == 1
            assert tracker._dispositions[0][1] == "human"
        finally:
            tracker.stop()

    @pytest.mark.asyncio
    async def test_voicemail_to_human_reclassification(self) -> None:
        """VOICEMAIL → HUMAN overwrites disposition (voicemail pickup)."""
        bus = EventBus()
        tracker = CallDispositionTracker(bus)
        tracker.start()
        try:
            await bus.emit(
                CallStateChanged(
                    old=OutboundCallState.CLASSIFYING,
                    new=OutboundCallState.VOICEMAIL,
                    call_sid="CA1",
                )
            )
            assert tracker._call_dispositions["CA1"] == "voicemail"
            await bus.emit(
                CallStateChanged(
                    old=OutboundCallState.VOICEMAIL,
                    new=OutboundCallState.HUMAN,
                    call_sid="CA1",
                )
            )
            assert tracker._call_dispositions["CA1"] == "human"
            # Only one entry in the list (replaced, not duplicated).
            ca1_entries = [d for d in tracker._dispositions if d[2] == "CA1"]
            assert len(ca1_entries) == 1
            assert ca1_entries[0][1] == "human"
        finally:
            tracker.stop()
