"""Tests for the server → browser event channel (transports/_browser_events.py)."""

from __future__ import annotations

from typing import Any

import pytest

from easycat.events import (
    AgentDelta,
    AgentFinal,
    BotStartedSpeaking,
    EventBus,
    Interruption,
    STTFinal,
    STTPartial,
    TurnStarted,
)
from easycat.transports._browser_events import (
    BROWSER_EVENT_SCHEMA_VERSION,
    BROWSER_EVENT_TYPES,
    BrowserEventForwarder,
)


class _Sink:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []
        self.fail = False

    async def send(self, payload: dict[str, Any]) -> None:
        if self.fail:
            raise ConnectionError("client gone")
        self.payloads.append(payload)

    def of_type(self, message_type: str) -> list[dict[str, Any]]:
        return [p for p in self.payloads if p["type"] == message_type]


@pytest.fixture
def bus() -> EventBus:
    return EventBus(handler_error_policy="raise")


@pytest.fixture
def sink() -> _Sink:
    return _Sink()


class TestBrowserEventForwarder:
    async def test_forwards_transcript_and_lifecycle_events(self, bus: EventBus, sink: _Sink):
        forwarder = BrowserEventForwarder(bus, sink.send)

        await bus.emit(TurnStarted(turn_id="t1"))
        await bus.emit(STTPartial(text="hel", turn_id="t1"))
        await bus.emit(STTFinal(text="hello there", turn_id="t1"))
        await bus.emit(AgentDelta(text="Hi! ", turn_id="t1"))
        await bus.emit(AgentFinal(text="Hi! How can I help?", turn_id="t1"))
        await bus.emit(Interruption(turn_id="t1"))

        types = [p["type"] for p in sink.payloads]
        assert types == [
            "turn_started",
            "stt_partial",
            "stt_final",
            "agent_delta",
            "agent_final",
            "interruption",
        ]
        assert sink.of_type("stt_partial")[0] == {
            "type": "stt_partial",
            "text": "hel",
            "turn_id": "t1",
        }
        assert sink.of_type("agent_final")[0]["text"] == "Hi! How can I help?"
        assert set(types) <= set(BROWSER_EVENT_TYPES)
        forwarder.close()

    async def test_turn_latency_measures_stt_final_to_bot_audio(self, bus: EventBus, sink: _Sink):
        forwarder = BrowserEventForwarder(bus, sink.send)

        await bus.emit(STTFinal(text="hi", turn_id="t1", timestamp=100.0))
        await bus.emit(BotStartedSpeaking(turn_id="t1", timestamp=100.42))

        (latency,) = sink.of_type("turn_latency")
        assert latency["turn_id"] == "t1"
        assert latency["ms"] == pytest.approx(420.0, abs=0.5)
        forwarder.close()

    async def test_turn_latency_falls_back_to_oldest_pending_turn(
        self, bus: EventBus, sink: _Sink
    ):
        forwarder = BrowserEventForwarder(bus, sink.send)

        await bus.emit(STTFinal(text="hi", turn_id="t1", timestamp=10.0))
        # Correlation ids can differ across hops; the readout should still fire.
        await bus.emit(BotStartedSpeaking(turn_id="other", timestamp=10.25))

        (latency,) = sink.of_type("turn_latency")
        assert latency["ms"] == pytest.approx(250.0, abs=0.5)
        forwarder.close()

    async def test_no_latency_without_pending_user_turn(self, bus: EventBus, sink: _Sink):
        forwarder = BrowserEventForwarder(bus, sink.send)

        await bus.emit(BotStartedSpeaking(turn_id="greeting", timestamp=5.0))

        assert sink.of_type("turn_latency") == []
        forwarder.close()

    async def test_latency_not_repeated_for_same_turn(self, bus: EventBus, sink: _Sink):
        forwarder = BrowserEventForwarder(bus, sink.send)

        await bus.emit(STTFinal(text="hi", turn_id="t1", timestamp=1.0))
        await bus.emit(BotStartedSpeaking(turn_id="t1", timestamp=1.2))
        await bus.emit(BotStartedSpeaking(turn_id="t1", timestamp=1.9))

        assert len(sink.of_type("turn_latency")) == 1
        forwarder.close()

    async def test_send_failures_are_swallowed(self, bus: EventBus, sink: _Sink):
        forwarder = BrowserEventForwarder(bus, sink.send)
        sink.fail = True

        # Must not raise even with handler_error_policy="raise".
        await bus.emit(STTPartial(text="hi", turn_id="t1"))

        assert sink.payloads == []
        forwarder.close()

    async def test_close_unsubscribes_and_is_idempotent(self, bus: EventBus, sink: _Sink):
        forwarder = BrowserEventForwarder(bus, sink.send)
        forwarder.close()
        forwarder.close()

        await bus.emit(STTPartial(text="hi", turn_id="t1"))

        assert sink.payloads == []

    def test_schema_version_is_stable(self):
        assert BROWSER_EVENT_SCHEMA_VERSION == 1
