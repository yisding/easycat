"""Session-level telephony plumbing: opt-out, greeting, transport_kind.

Covers the three feature wires added alongside the caller-ID support:

- ``EasyConfig.greeting`` / ``SessionConfig.greeting`` auto-
  synthesizes on the first ``CallAnswered`` event, without a second
  ``CallAnswered`` re-greeting.
- ``opt_out_detection`` (default on) listens for STT finals matching
  :data:`OPT_OUT_PHRASES`, emits :class:`OptOutDetected`, adds the
  caller number to an attached ``DNCList``, and enqueues
  :class:`EndCallAction`.
- ``session.transport_kind`` labels the transport for tool-side
  branching.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from easycat import (
    Session,
    SessionActions,
    SessionConfig,
    STTFinal,
    TwilioConnectionTransport,
)
from easycat.events import CallAnswered, CallEnded, EventBus, OptOutDetected
from easycat.session._types import CallIdentity
from easycat.stubs import NoopAgent
from easycat.telephony.compliance import DNCList
from easycat.transports.local import LocalTransport, LocalTransportConfig
from easycat.transports.twilio_media import TwilioTransport, TwilioTransportConfig


def _text_session(**overrides: Any) -> Session:
    defaults: dict[str, Any] = dict(
        agent=NoopAgent(),
        runtime_mode="text_session",
    )
    defaults.update(overrides)
    return Session(SessionConfig(**defaults))


class _DummyWebSocket:
    async def send(self, _message: str) -> None:
        return None

    async def close(self) -> None:
        return None


class _ClosingWebSocket(_DummyWebSocket):
    def __init__(self, messages: list[str]) -> None:
        self._messages = messages

    def __aiter__(self) -> _ClosingWebSocket:
        return self

    async def __anext__(self) -> str:
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


# ── transport_kind ─────────────────────────────────────────────────


def test_transport_kind_telephony() -> None:
    # text-session mode skips the live-provider validation so we can
    # stamp an arbitrary transport and verify the kind label.
    session = _text_session(
        transport=TwilioTransport(TwilioTransportConfig(), event_bus=EventBus()),
    )
    assert session.transport_kind == "telephony"


def test_transport_kind_local() -> None:
    session = _text_session(transport=LocalTransport(LocalTransportConfig()))
    assert session.transport_kind == "local"


def test_transport_kind_noop_default() -> None:
    # text-session uses NoopTransport under the hood.
    session = _text_session()
    assert session.transport_kind == "noop"


# ── Greeting on CallAnswered ───────────────────────────────────────


@pytest.mark.asyncio
async def test_greeting_plays_once_on_call_answered() -> None:
    session = _text_session(greeting="Hello, thanks for calling.")
    session.synthesize_bypass = AsyncMock()  # type: ignore[method-assign]

    await session.event_bus.emit(CallAnswered(call_sid="CA1"))
    task = session._greeting.task
    assert task is not None
    await session.event_bus.emit(CallAnswered(call_sid="CA2"))  # warm-transfer sim
    await task

    session.synthesize_bypass.assert_awaited_once_with("Hello, thanks for calling.")


@pytest.mark.asyncio
async def test_greeting_does_not_block_call_answered_dispatch() -> None:
    session = _text_session(greeting="Hello, thanks for calling.")
    started = asyncio.Event()
    release = asyncio.Event()
    later_handlers: list[str] = []

    async def slow_synthesize(text: str) -> None:
        started.set()
        await release.wait()

    async def later_handler(event: CallAnswered) -> None:
        later_handlers.append(event.call_sid)

    session.synthesize_bypass = AsyncMock(side_effect=slow_synthesize)  # type: ignore[method-assign]
    session.event_bus.subscribe(CallAnswered, later_handler)

    await session.event_bus.emit(CallAnswered(call_sid="CA1"))

    assert later_handlers == ["CA1"]
    assert session._greeting.task is not None
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert session._greeting.spoken is False

    release.set()
    await session._greeting.task
    assert session._greeting.spoken is True


@pytest.mark.asyncio
async def test_greeting_marks_spoken_only_after_success() -> None:
    session = _text_session(greeting="Hello, thanks for calling.")
    session.synthesize_bypass = AsyncMock(side_effect=[RuntimeError("tts failed"), None])  # type: ignore[method-assign]

    await session.event_bus.emit(CallAnswered(call_sid="CA1"))
    first = session._greeting.task
    assert first is not None
    await first
    assert session._greeting.spoken is False

    await session.event_bus.emit(CallAnswered(call_sid="CA1"))
    second = session._greeting.task
    assert second is not None
    await second

    assert session._greeting.spoken is True
    assert session.synthesize_bypass.await_count == 2


@pytest.mark.asyncio
async def test_greeting_not_spoken_when_disabled() -> None:
    session = _text_session()  # no greeting
    session.synthesize_bypass = AsyncMock()  # type: ignore[method-assign]

    await session.event_bus.emit(CallAnswered(call_sid="CA1"))

    session.synthesize_bypass.assert_not_called()


# ── Opt-out auto-detection ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_opt_out_match_emits_event_and_adds_to_dnc() -> None:
    dnc = DNCList()
    actions = SessionActions()
    session = _text_session(dnc_list=dnc, session_actions=actions)
    session.call_identity = CallIdentity(caller_number="+15551234567", direction="inbound")

    detected: list[OptOutDetected] = []
    session.event_bus.subscribe(OptOutDetected, detected.append)

    await session.event_bus.emit(
        STTFinal(text="Please take me off your list, seriously stop calling")
    )

    assert len(detected) == 1
    assert detected[0].number == "+15551234567"
    assert detected[0].phrase in ("take me off your list", "stop calling")
    # DNCList now blocks the number.
    assert dnc.is_on_dnc("+15551234567")
    # EndCallAction has been enqueued so the call terminates after the
    # agent's current utterance.
    assert actions.has_pending
    drained = actions.drain()
    assert len(drained) == 1
    assert drained[0].type.value == "end_call"


@pytest.mark.asyncio
async def test_opt_out_does_not_fire_on_neutral_text() -> None:
    dnc = DNCList()
    session = _text_session(dnc_list=dnc)
    session.call_identity = CallIdentity(caller_number="+15551234567", direction="inbound")

    detected: list[OptOutDetected] = []
    session.event_bus.subscribe(OptOutDetected, detected.append)
    await session.event_bus.emit(STTFinal(text="Hello, how are you?"))

    assert detected == []
    assert not dnc.is_on_dnc("+15551234567")


@pytest.mark.asyncio
async def test_opt_out_disabled_skips_detection() -> None:
    dnc = DNCList()
    session = _text_session(dnc_list=dnc, opt_out_detection=False)
    session.call_identity = CallIdentity(caller_number="+15551234567", direction="inbound")

    detected: list[OptOutDetected] = []
    session.event_bus.subscribe(OptOutDetected, detected.append)
    await session.event_bus.emit(STTFinal(text="stop calling"))

    assert detected == []
    assert not dnc.is_on_dnc("+15551234567")


@pytest.mark.asyncio
async def test_opt_out_custom_phrase_list() -> None:
    dnc = DNCList()
    actions = SessionActions()
    session = _text_session(
        dnc_list=dnc,
        session_actions=actions,
        opt_out_phrases=("retire me",),
    )
    session.call_identity = CallIdentity(caller_number="+15551234567", direction="inbound")

    await session.event_bus.emit(STTFinal(text="please retire me from your list"))
    assert dnc.is_on_dnc("+15551234567")
    # Stock phrase list no longer fires when overridden.
    dnc2 = DNCList()
    session2 = _text_session(dnc_list=dnc2, opt_out_phrases=("retire me",))
    session2.call_identity = CallIdentity(caller_number="+15550001111", direction="inbound")
    await session2.event_bus.emit(STTFinal(text="stop calling me"))
    assert not dnc2.is_on_dnc("+15550001111")


# ── Inbound CallEnded on stop ─────────────────────────────────────


@pytest.mark.asyncio
async def test_twilio_stop_emits_call_ended() -> None:
    bus = EventBus()
    transport = TwilioTransport(TwilioTransportConfig(), event_bus=bus)
    ended: list[Any] = []

    bus.subscribe(CallEnded, ended.append)

    # Prime the transport as if a start happened.
    await transport._handle_start(
        {
            "streamSid": "MZ1",
            "start": {
                "streamSid": "MZ1",
                "callSid": "CA1",
                "customParameters": {"From": "+15551234567"},
            },
        }
    )
    # Simulate the stop message handler directly.
    await transport._handle_message('{"event": "stop", "streamSid": "MZ1", "stop": {}}')

    assert len(ended) == 1
    assert ended[0].call_sid == "CA1"
    assert ended[0].number == "+15551234567"
    assert ended[0].duration_s is not None and ended[0].duration_s >= 0


@pytest.mark.asyncio
async def test_twilio_connection_start_and_stop_emit_lifecycle_events() -> None:
    bus = EventBus()
    transport = TwilioConnectionTransport(_DummyWebSocket(), event_bus=bus)
    answered: list[CallAnswered] = []
    ended: list[CallEnded] = []
    bus.subscribe(CallAnswered, answered.append)
    bus.subscribe(CallEnded, ended.append)

    await transport._handle_start(
        {
            "streamSid": "MZ1",
            "start": {
                "streamSid": "MZ1",
                "callSid": "CA1",
                "customParameters": {"From": "+15551234567"},
            },
        }
    )
    await transport._handle_message('{"event": "stop", "streamSid": "MZ1", "stop": {}}')

    assert transport.call_identity is not None
    assert transport.call_identity.caller_number == "+15551234567"
    assert len(answered) == 1
    assert answered[0].call_sid == "CA1"
    assert len(ended) == 1
    assert ended[0].call_sid == "CA1"
    assert ended[0].number == "+15551234567"
    assert ended[0].duration_s is not None and ended[0].duration_s >= 0


@pytest.mark.asyncio
async def test_twilio_connection_close_without_stop_emits_call_ended() -> None:
    bus = EventBus()
    ws = _ClosingWebSocket(
        [
            (
                '{"event": "start", "streamSid": "MZ1", "start": {'
                '"streamSid": "MZ1", "callSid": "CA1", '
                '"customParameters": {"From": "+15551234567"}}}'
            )
        ]
    )
    transport = TwilioConnectionTransport(ws, event_bus=bus)
    ended: list[CallEnded] = []
    bus.subscribe(CallEnded, ended.append)

    await transport.connect()
    assert transport._receive_task is not None
    await transport._receive_task

    assert len(ended) == 1
    assert ended[0].call_sid == "CA1"
    assert ended[0].number == "+15551234567"


@pytest.mark.asyncio
async def test_twilio_connection_stop_then_close_emits_call_ended_once() -> None:
    bus = EventBus()
    ws = _ClosingWebSocket(
        [
            (
                '{"event": "start", "streamSid": "MZ1", "start": {'
                '"streamSid": "MZ1", "callSid": "CA1", '
                '"customParameters": {"From": "+15551234567"}}}'
            ),
            '{"event": "stop", "streamSid": "MZ1", "stop": {}}',
        ]
    )
    transport = TwilioConnectionTransport(ws, event_bus=bus)
    ended: list[CallEnded] = []
    bus.subscribe(CallEnded, ended.append)

    await transport.connect()
    assert transport._receive_task is not None
    await transport._receive_task

    assert len(ended) == 1
    assert ended[0].call_sid == "CA1"


@pytest.mark.asyncio
async def test_twilio_transport_stop_then_socket_close_emits_call_ended_once() -> None:
    bus = EventBus()
    ws = _ClosingWebSocket(
        [
            (
                '{"event": "start", "streamSid": "MZ1", "start": {'
                '"streamSid": "MZ1", "callSid": "CA1", '
                '"customParameters": {"From": "+15551234567"}}}'
            ),
            '{"event": "stop", "streamSid": "MZ1", "stop": {}}',
        ]
    )
    transport = TwilioTransport(TwilioTransportConfig(), event_bus=bus)
    ended: list[CallEnded] = []
    bus.subscribe(CallEnded, ended.append)

    await transport._handle_connection(ws)

    assert len(ended) == 1
    assert ended[0].call_sid == "CA1"
