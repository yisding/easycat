"""Tests for outbound call lifecycle events."""

from __future__ import annotations

import pytest

from easycat.events import (
    CallAnswered,
    CallEnded,
    CallFailed,
    CallInitiated,
    CallRinging,
    CallScreening,
    Event,
    EventBus,
)


class TestCallLifecycleEvents:
    def test_call_initiated_fields(self) -> None:
        ev = CallInitiated(call_sid="CA123", to="+155512345", from_="+155598765")
        assert ev.call_sid == "CA123"
        assert ev.to == "+155512345"
        assert ev.from_ == "+155598765"
        assert ev.session_id is None
        assert ev.turn_id is None
        assert isinstance(ev.timestamp, float)

    def test_call_ringing_fields(self) -> None:
        ev = CallRinging(call_sid="CA123")
        assert ev.call_sid == "CA123"
        assert isinstance(ev.timestamp, float)

    def test_call_answered_fields(self) -> None:
        ev = CallAnswered(call_sid="CA123", answered_by="human")
        assert ev.call_sid == "CA123"
        assert ev.answered_by == "human"

    def test_call_screening_fields(self) -> None:
        ev = CallScreening(call_sid="CA123", platform="ios")
        assert ev.call_sid == "CA123"
        assert ev.platform == "ios"

    def test_call_failed_fields(self) -> None:
        ev = CallFailed(call_sid="CA123", reason="busy")
        assert ev.call_sid == "CA123"
        assert ev.reason == "busy"
        assert ev.sip_code is None

    def test_call_failed_with_sip_code(self) -> None:
        ev = CallFailed(call_sid="CA123", reason="blocked_unwanted", sip_code=607)
        assert ev.sip_code == 607

    def test_call_ended_fields(self) -> None:
        ev = CallEnded(call_sid="CA123", duration_s=45.2, disposition="completed")
        assert ev.call_sid == "CA123"
        assert ev.duration_s == 45.2
        assert ev.disposition == "completed"

    def test_events_are_event_subclasses(self) -> None:
        for cls in (
            CallInitiated,
            CallRinging,
            CallAnswered,
            CallScreening,
            CallFailed,
            CallEnded,
        ):
            assert issubclass(cls, Event), f"{cls.__name__} not a subclass of Event"

    @pytest.mark.asyncio
    async def test_events_emittable_on_bus(self) -> None:
        bus = EventBus()
        events_to_test = [
            CallInitiated(call_sid="CA1", to="+1", from_="+2"),
            CallRinging(call_sid="CA1"),
            CallAnswered(call_sid="CA1"),
            CallScreening(call_sid="CA1", platform="ios"),
            CallFailed(call_sid="CA1", reason="busy"),
            CallEnded(call_sid="CA1", duration_s=10.0),
        ]
        for ev in events_to_test:
            received: list[Event] = []
            bus.subscribe(type(ev), received.append)
            await bus.emit(ev)
            assert len(received) == 1
            assert received[0] is ev
