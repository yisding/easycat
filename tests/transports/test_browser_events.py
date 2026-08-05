"""Tests for the server → browser event channel (transports/_browser_events.py)."""

from __future__ import annotations

import asyncio
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
from easycat.runtime.scope import RuntimeScope, RuntimeSupervisor
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


async def _drain(forwarder: BrowserEventForwarder) -> None:
    await asyncio.wait_for(forwarder._send_queue.join(), timeout=0.2)


@pytest.fixture
def bus() -> EventBus:
    return EventBus(handler_error_policy="raise")


@pytest.fixture
def sink() -> _Sink:
    return _Sink()


class TestBrowserEventForwarder:
    async def test_writer_and_send_tasks_attach_to_transport_scope(self, bus: EventBus):
        root = RuntimeScope.create_root(
            name="transport-runtime",
            root_id="test-root:browser-events",
            supervisor=RuntimeSupervisor(capacity=1),
            survivor_capacity=1,
        )
        entered = asyncio.Event()
        release = asyncio.Event()

        async def blocked_send(_payload: dict[str, Any]) -> None:
            entered.set()
            await release.wait()

        forwarder = BrowserEventForwarder(bus, blocked_send, runtime_scope=root)
        await bus.emit(STTPartial(text="hi", turn_id="t1"))
        await entered.wait()

        assert root.tasks("browser_event_writer")
        assert root.tasks("browser_event_send")

        closing = asyncio.create_task(root.close(phases=("transport-events",)))
        await asyncio.sleep(0)
        assert not closing.done()
        release.set()
        await closing

        assert not forwarder._send_tasks
        forwarder.close()

    async def test_closed_transport_scope_drops_and_accounts_queued_event(
        self, bus: EventBus, sink: _Sink
    ):
        root = RuntimeScope.create_root(
            name="transport-runtime",
            root_id="test-root:closed-browser-events",
            supervisor=RuntimeSupervisor(capacity=1),
            survivor_capacity=1,
        )
        await root.close()
        forwarder = BrowserEventForwarder(bus, sink.send, runtime_scope=root)

        await bus.emit(STTPartial(text="late", turn_id="t1"))
        await _drain(forwarder)

        assert sink.payloads == []
        forwarder.close()

    async def test_forwards_transcript_and_lifecycle_events(self, bus: EventBus, sink: _Sink):
        forwarder = BrowserEventForwarder(bus, sink.send)

        await bus.emit(TurnStarted(turn_id="t1"))
        await bus.emit(STTPartial(text="hel", turn_id="t1"))
        await bus.emit(STTFinal(text="hello there", turn_id="t1"))
        await bus.emit(AgentDelta(text="Hi! ", turn_id="t1"))
        await bus.emit(AgentFinal(text="Hi! How can I help?", turn_id="t1"))
        await bus.emit(Interruption(turn_id="t1"))
        await _drain(forwarder)

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
        await _drain(forwarder)

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
        await _drain(forwarder)

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
        await _drain(forwarder)

        assert len(sink.of_type("turn_latency")) == 1
        forwarder.close()

    async def test_send_failures_are_swallowed(self, bus: EventBus, sink: _Sink):
        forwarder = BrowserEventForwarder(bus, sink.send)
        sink.fail = True

        # Must not raise even with handler_error_policy="raise".
        await bus.emit(STTPartial(text="hi", turn_id="t1"))
        await _drain(forwarder)

        assert sink.payloads == []
        forwarder.close()

    async def test_slow_send_is_bounded_and_later_handlers_run(self, bus: EventBus):
        send_started = asyncio.Event()
        send_cancelled = asyncio.Event()
        later_handler_ran = asyncio.Event()

        async def blocked_send(_payload: dict[str, Any]) -> None:
            send_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                send_cancelled.set()

        forwarder = BrowserEventForwarder(bus, blocked_send, send_timeout_s=0.01)

        async def later_handler(_event: STTPartial) -> None:
            later_handler_ran.set()

        subscription = bus.subscribe(STTPartial, later_handler)
        await asyncio.wait_for(
            bus.emit(STTPartial(text="hi", turn_id="t1")),
            timeout=0.2,
        )

        await asyncio.wait_for(send_started.wait(), timeout=0.2)
        assert later_handler_ran.is_set()
        await asyncio.wait_for(send_cancelled.wait(), timeout=0.2)
        subscription.unsubscribe()
        forwarder.close()

    async def test_timeout_does_not_wait_for_cancellation_resistant_sender(self, bus: EventBus):
        first_cancelled = asyncio.Event()
        release_first = asyncio.Event()
        second_sent = asyncio.Event()
        attempts = 0

        async def cancellation_resistant_send(_payload: dict[str, Any]) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    first_cancelled.set()
                    await release_first.wait()
            else:
                second_sent.set()

        forwarder = BrowserEventForwarder(
            bus,
            cancellation_resistant_send,
            send_timeout_s=0.01,
        )
        await bus.emit(STTPartial(text="one", turn_id="t1"))
        await asyncio.wait_for(first_cancelled.wait(), timeout=0.2)

        await bus.emit(STTPartial(text="two", turn_id="t1"))
        await asyncio.wait_for(second_sent.wait(), timeout=0.2)

        release_first.set()
        await asyncio.sleep(0)
        forwarder.close()

    async def test_cancellation_suppressing_senders_remain_bounded(self, bus: EventBus):
        release = asyncio.Event()
        attempts = 0

        async def cancellation_suppressing_send(_payload: dict[str, Any]) -> None:
            nonlocal attempts
            attempts += 1
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        forwarder = BrowserEventForwarder(
            bus,
            cancellation_suppressing_send,
            send_timeout_s=0.005,
            max_pending_events=2,
        )
        for index in range(8):
            await bus.emit(STTPartial(text=str(index), turn_id="t1"))
            await _drain(forwarder)

        assert attempts == 2
        assert sum(not task.done() for task in forwarder._send_tasks) == 2

        release.set()
        await asyncio.gather(*tuple(forwarder._send_tasks))
        await asyncio.sleep(0)
        assert forwarder._send_tasks == set()
        forwarder.close()

    @pytest.mark.parametrize("timeout_s", [0, -1, float("nan"), float("inf")])
    def test_send_timeout_must_be_positive_and_finite(
        self,
        bus: EventBus,
        sink: _Sink,
        timeout_s: float,
    ):
        with pytest.raises(ValueError, match="send_timeout_s"):
            BrowserEventForwarder(bus, sink.send, send_timeout_s=timeout_s)

    @pytest.mark.parametrize("max_pending_events", [0, -1, True, 1.5])
    def test_writer_queue_size_must_be_positive(
        self,
        bus: EventBus,
        sink: _Sink,
        max_pending_events: Any,
    ):
        with pytest.raises(ValueError, match="max_pending_events"):
            BrowserEventForwarder(
                bus,
                sink.send,
                max_pending_events=max_pending_events,
            )

    async def test_close_cancels_a_blocked_writer(self, bus: EventBus):
        send_started = asyncio.Event()
        send_cancelled = asyncio.Event()

        async def blocked_send(_payload: dict[str, Any]) -> None:
            send_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                send_cancelled.set()

        forwarder = BrowserEventForwarder(bus, blocked_send, send_timeout_s=1)
        await bus.emit(STTPartial(text="hi", turn_id="t1"))
        await asyncio.wait_for(send_started.wait(), timeout=0.1)

        forwarder.close()

        await asyncio.wait_for(send_cancelled.wait(), timeout=0.1)

    async def test_full_writer_queue_drops_new_event(self, bus: EventBus):
        first_send_started = asyncio.Event()
        release_first_send = asyncio.Event()
        second_send_finished = asyncio.Event()
        attempted: list[dict[str, Any]] = []

        async def controlled_send(payload: dict[str, Any]) -> None:
            attempted.append(payload)
            if len(attempted) == 1:
                first_send_started.set()
                await release_first_send.wait()
            elif len(attempted) == 2:
                second_send_finished.set()

        forwarder = BrowserEventForwarder(
            bus,
            controlled_send,
            max_pending_events=1,
        )
        await bus.emit(STTPartial(text="one", turn_id="t1"))
        await asyncio.wait_for(first_send_started.wait(), timeout=0.1)
        await bus.emit(STTPartial(text="two", turn_id="t1"))
        await bus.emit(STTPartial(text="three", turn_id="t1"))

        release_first_send.set()
        await asyncio.wait_for(second_send_finished.wait(), timeout=0.1)
        assert [payload["text"] for payload in attempted] == ["one", "two"]
        forwarder.close()

    async def test_close_unsubscribes_and_is_idempotent(self, bus: EventBus, sink: _Sink):
        forwarder = BrowserEventForwarder(bus, sink.send)
        forwarder.close()
        forwarder.close()

        await bus.emit(STTPartial(text="hi", turn_id="t1"))

        assert sink.payloads == []

    def test_schema_version_is_stable(self):
        assert BROWSER_EVENT_SCHEMA_VERSION == 1
