"""Tests for number health monitoring and call disposition tracking."""

from __future__ import annotations

import pytest

from easycat.events import CallEnded, CallFailed, CallInitiated, EventBus
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

    @pytest.mark.asyncio
    async def test_duplicate_terminal_events_are_recorded_once(self) -> None:
        bus = EventBus()
        monitor = NumberHealthMonitor(bus)
        monitor.start()
        try:
            await bus.emit(CallInitiated(call_sid="CA1", to="+15551234567", from_="+15557654321"))
            await bus.emit(CallEnded(call_sid="CA1", duration_s=10.0, number="+15551234567"))
            await bus.emit(CallEnded(call_sid="CA1", duration_s=11.0, number="+15551234567"))
            await bus.emit(CallFailed(call_sid="CA1", reason="busy", number="+15551234567"))
            assert len(monitor._records["+15551234567"]) == 1
            assert monitor._records["+15551234567"][0].answered is True
        finally:
            monitor.stop()

    @pytest.mark.asyncio
    async def test_placement_failure_does_not_create_phantom_buckets(self) -> None:
        """A CallFailed with an empty SID and no number records nothing.

        ``place_call`` emits ``CallFailed(call_sid="", reason=...)`` when
        ``calls.create`` fails. Such an event has no resolvable from-number, so
        the monitor must not file a phantom record under an empty key.
        """
        bus = EventBus()
        monitor = NumberHealthMonitor(bus)
        monitor.start()
        try:
            await bus.emit(CallFailed(call_sid="", reason="error placing call"))
            assert "" not in monitor._records
            assert dict(monitor._records) == {}
        finally:
            monitor.stop()

    @pytest.mark.asyncio
    async def test_untracked_sid_failure_does_not_masquerade_as_number(self) -> None:
        """A failure for an untracked SID with no number is skipped, not keyed by SID."""
        bus = EventBus()
        monitor = NumberHealthMonitor(bus)
        monitor.start()
        try:
            await bus.emit(CallFailed(call_sid="CA-untracked", reason="busy"))
            assert "CA-untracked" not in monitor._records
            assert dict(monitor._records) == {}
        finally:
            monitor.stop()

    @pytest.mark.asyncio
    async def test_tracked_sid_failure_without_number_uses_from_number(self) -> None:
        """When the SID is tracked, the failure is recorded under the from-number."""
        bus = EventBus()
        monitor = NumberHealthMonitor(bus)
        monitor.start()
        try:
            await bus.emit(CallInitiated(call_sid="CA2", to="+15551234567", from_="+15557654321"))
            await bus.emit(CallFailed(call_sid="CA2", reason="busy"))
            assert len(monitor._records["+15557654321"]) == 1
            assert monitor._records["+15557654321"][0].answered is False
        finally:
            monitor.stop()

    @pytest.mark.asyncio
    async def test_failed_callbacks_are_bounded_by_number_cardinality(self, monkeypatch) -> None:
        bus = EventBus()
        monkeypatch.setattr(NumberHealthMonitor, "_MAX_TRACKED_NUMBERS", 3)
        monitor = NumberHealthMonitor(bus)
        monitor.start()
        try:
            for i in range(8):
                await bus.emit(
                    CallFailed(
                        call_sid=f"CA{i}",
                        reason="busy",
                        number=f"+1555000{i}",
                    )
                )

            assert len(monitor._records) <= 3
            assert len(monitor._last_call_time) <= 3
            assert monitor._concurrent == {}
        finally:
            monitor.stop()

    @pytest.mark.asyncio
    async def test_all_active_capacity_fallback_drops_oldest(self, monkeypatch) -> None:
        """When every tracked number is in-flight, the oldest bucket is dropped anyway."""
        bus = EventBus()
        monkeypatch.setattr(NumberHealthMonitor, "_MAX_TRACKED_NUMBERS", 3)
        monitor = NumberHealthMonitor(bus)
        monitor.start()
        try:
            # Fill to cap with in-flight (concurrent > 0) numbers; none are inactive.
            for i in range(3):
                await bus.emit(
                    CallInitiated(
                        call_sid=f"CA{i}",
                        to=f"+1555100{i}",
                        from_=f"+1555000{i}",
                    )
                )
            assert len(monitor._last_call_time) == 3
            assert all(monitor._concurrent[f"+1555000{i}"] == 1 for i in range(3))

            # One more in-flight number forces the all-active fallback branch:
            # the oldest tracked number (+15550000) is evicted despite being active.
            await bus.emit(CallInitiated(call_sid="CA3", to="+15551003", from_="+15550003"))

            assert len(monitor._last_call_time) <= 3
            assert "+15550003" in monitor._last_call_time
            # The force-dropped oldest number's SID mapping is purged too, so a
            # later terminal event short-circuits instead of touching concurrency.
            assert "+15550000" not in monitor._last_call_time
            assert "CA0" not in monitor._call_sid_to_number
        finally:
            monitor.stop()

    @pytest.mark.asyncio
    async def test_force_dropped_number_terminal_event_does_not_decrement_phantom(
        self, monkeypatch
    ) -> None:
        """A terminal event for a force-dropped number resolves to None, no phantom decrement."""
        bus = EventBus()
        monkeypatch.setattr(NumberHealthMonitor, "_MAX_TRACKED_NUMBERS", 2)
        monitor = NumberHealthMonitor(bus)
        monitor.start()
        try:
            for i in range(2):
                await bus.emit(
                    CallInitiated(
                        call_sid=f"CA{i}",
                        to=f"+1555100{i}",
                        from_=f"+1555000{i}",
                    )
                )
            # Force-drop the oldest active number (+15550000) by initiating a third.
            await bus.emit(CallInitiated(call_sid="CA2", to="+15551002", from_="+15550002"))
            assert "+15550000" not in monitor._last_call_time
            assert "CA0" not in monitor._call_sid_to_number

            # Terminal event for the dropped call: number unresolvable -> skipped.
            await bus.emit(CallEnded(call_sid="CA0", duration_s=5.0))
            assert "+15550000" not in monitor._concurrent
            assert "+15550000" not in monitor._records
        finally:
            monitor.stop()


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

    @pytest.mark.asyncio
    async def test_failed_call_reasons_are_bounded_without_state_changes(
        self, monkeypatch
    ) -> None:
        bus = EventBus()
        monkeypatch.setattr(CallDispositionTracker, "_MAX_CALL_TRACKING", 4)
        tracker = CallDispositionTracker(bus)
        tracker.start()
        try:
            for i in range(9):
                await bus.emit(CallFailed(call_sid=f"CA{i}", reason="busy"))

            assert len(tracker._failure_reasons) <= 4
            assert len(tracker._call_dispositions) == 0
        finally:
            tracker.stop()

    @pytest.mark.asyncio
    async def test_failure_overflow_does_not_drop_under_cap_dispositions(
        self, monkeypatch
    ) -> None:
        """A failure-only flood must not evict completed-call dispositions under their own cap."""
        bus = EventBus()
        monkeypatch.setattr(CallDispositionTracker, "_MAX_CALL_TRACKING", 4)
        tracker = CallDispositionTracker(bus)
        tracker.start()
        try:
            # Two completed-call dispositions, well under the cap of 4. These SIDs
            # are the dedupe/reclassification guard used by _on_state_changed.
            for i in range(2):
                await bus.emit(
                    CallStateChanged(
                        old=OutboundCallState.CLASSIFYING,
                        new=OutboundCallState.HUMAN,
                        call_sid=f"DISP{i}",
                    )
                )
            assert len(tracker._call_dispositions) == 2

            # Flood failure-only callbacks (distinct SIDs) to overflow
            # _failure_reasons past the cap without involving the dispositions.
            for i in range(9):
                await bus.emit(CallFailed(call_sid=f"FAIL{i}", reason="busy"))

            # _failure_reasons was trimmed, but the under-cap dispositions survive.
            assert len(tracker._failure_reasons) <= 4
            assert tracker._call_dispositions == {"DISP0": "human", "DISP1": "human"}
        finally:
            tracker.stop()

    @pytest.mark.asyncio
    async def test_recent_failure_reason_is_preserved_for_terminal_state(self) -> None:
        bus = EventBus()
        tracker = CallDispositionTracker(bus)
        tracker.start()
        try:
            await bus.emit(CallFailed(call_sid="CA-recent", reason="no-answer"))
            await bus.emit(
                CallStateChanged(
                    old=OutboundCallState.CLASSIFYING,
                    new=OutboundCallState.ENDED,
                    call_sid="CA-recent",
                )
            )

            assert tracker._call_dispositions["CA-recent"] == "no-answer"
        finally:
            tracker.stop()
