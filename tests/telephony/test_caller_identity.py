"""Caller-ID propagation from transports and outbound call manager.

Covers:

- ``TwilioTransport._handle_start`` parses ``<Stream>`` customParameters
  and pushes them into the bound identity sink.
- ``OutboundCallManager.place_call`` flows through ``CallInitiated`` so
  ``create_session`` stamps ``session.call_identity`` with
  direction="outbound".
- ``CallerIdState.system_message`` respects the exposure policy.
- ``AgentStage.execute_streaming`` prepends the system prefix to the
  bridge's ``turn_input.context``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.events import CallInitiated, EventBus, STTFinal
from easycat.integrations.agents._agent_runner import AgentRunner
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentTurnInput,
    CommitRule,
    UnitKind,
)
from easycat.runtime.context import RunContext
from easycat.session._types import CallIdentity
from easycat.stages.agent import AgentStage
from easycat.telephony.compliance import DNCList
from easycat.transports.twilio_media import TwilioTransport, TwilioTransportConfig


class _CaptureBridge:
    """ExternalAgentBridge stub that records every turn_input.context."""

    COMMITTABLE_BOUNDARIES = {UnitKind.AGENT: CommitRule.BETWEEN_TURNS}

    def __init__(self) -> None:
        self.captured_contexts: list[list[dict[str, str]]] = []

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder: Any,
        cancel_token: Any | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        self.captured_contexts.append(list(turn_input.context))
        yield AgentBridgeEvent(kind="text_delta", text="ok")
        yield AgentBridgeEvent(kind="done", text="ok")

    def snapshot_state(self) -> Any:  # pragma: no cover - unused
        from easycat.integrations.agents.base import FrameworkStateSnapshot

        return FrameworkStateSnapshot(fields={})

    def apply_interruption(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        pass

    def replace_last_assistant_text(self, text: str) -> None:  # pragma: no cover
        pass

    def append_interruption_note(self, note: str) -> None:  # pragma: no cover
        pass

    def reset(self) -> None:  # pragma: no cover
        pass


def _ctx() -> RunContext:
    return RunContext(run_id="r", session_id="s", runtime_mode="chained_pipeline")


def _turn() -> TurnContext:
    return TurnContext(turn_id="t1", cancel_token=CancelToken())


# ── Twilio start-event parsing ─────────────────────────────────────


def _make_twilio_transport() -> TwilioTransport:
    return TwilioTransport(TwilioTransportConfig(), event_bus=EventBus())


@pytest.mark.asyncio
async def test_twilio_handle_start_parses_caller_identity() -> None:
    from easycat.events import CallAnswered

    transport = _make_twilio_transport()
    received: list[CallIdentity] = []
    transport.bind_identity_sink(received.append)
    answered: list[CallAnswered] = []
    transport._event_bus.subscribe(CallAnswered, answered.append)

    await transport._handle_start(
        {
            "streamSid": "MZ1",
            "start": {
                "streamSid": "MZ1",
                "callSid": "CA1",
                "customParameters": {
                    "From": "+15551234567",
                    "To": "+15557654321",
                    "CallerName": "Alice Example",
                    "FromCity": "SAN FRANCISCO",
                    "FromState": "CA",
                    "FromZip": "94105",
                    "FromCountry": "US",
                    "crm_account_id": "ACC-42",
                },
            },
        }
    )
    assert transport.call_identity is not None
    assert transport.call_identity.caller_number == "+15551234567"
    assert transport.call_identity.called_number == "+15557654321"
    assert transport.call_identity.direction == "inbound"
    assert transport.call_identity.display_name == "Alice Example"
    assert transport.call_identity.call_sid == "CA1"
    assert transport.call_identity.city == "SAN FRANCISCO"
    assert transport.call_identity.state == "CA"
    assert transport.call_identity.zip_code == "94105"
    assert transport.call_identity.country == "US"
    assert transport.call_identity.custom_fields == {"crm_account_id": "ACC-42"}
    assert received == [transport.call_identity]
    # CallAnswered fires for lifecycle-symmetry with outbound.
    assert len(answered) == 1
    assert answered[0].call_sid == "CA1"


@pytest.mark.asyncio
async def test_twilio_handle_start_without_custom_parameters() -> None:
    transport = _make_twilio_transport()
    await transport._handle_start({"streamSid": "MZ1", "start": {"callSid": "CA1"}})
    assert transport.call_identity is not None
    assert transport.call_identity.caller_number == ""
    assert transport.call_identity.called_number == ""
    assert transport.call_identity.direction == "inbound"
    assert transport.call_identity.city is None
    assert transport.call_identity.state is None


@pytest.mark.asyncio
async def test_twilio_handle_start_ignores_unsubstituted_template_parameters() -> None:
    transport = _make_twilio_transport()
    await transport._handle_start(
        {
            "streamSid": "MZ1",
            "start": {
                "callSid": "CA1",
                "customParameters": {
                    "Direction": "{{Direction}}",
                    "From": "{{From}}",
                    "To": "{{To}}",
                    "CallerName": "{{CallerName}}",
                },
            },
        }
    )
    assert transport.call_identity is not None
    assert transport.call_identity.caller_number == ""
    assert transport.call_identity.called_number == ""
    assert transport.call_identity.display_name is None


# ── create_session wires OutboundCallManager → session.call_identity ─


@pytest.mark.asyncio
async def test_outbound_call_initiated_populates_session_call_identity() -> None:
    """A :class:`CallInitiated` event emitted by the outbound manager
    should stamp ``session.call_identity`` with direction="outbound".

    The wiring lives in :func:`easycat.config.create_session`; this
    test rebuilds the handler directly to keep the assertion scoped."""
    from easycat import Session, SessionConfig
    from easycat.events import EventBus as _EventBus
    from easycat.stubs import NoopAgent

    bus = _EventBus()
    session = Session(
        SessionConfig(
            agent=NoopAgent(),
            event_bus=bus,
            runtime_mode="text_session",
            caller_id_exposure="system_message",
        )
    )

    # Subscribe the same handler that create_session wires.
    def _on_initiated(event: CallInitiated) -> None:
        if session.call_identity is not None and session.call_identity.direction == "inbound":
            return
        session.call_identity = CallIdentity(
            caller_number=event.to,
            called_number=event.from_,
            direction="outbound",
            call_sid=event.call_sid,
        )

    bus.subscribe(CallInitiated, _on_initiated)

    await bus.emit(CallInitiated(call_sid="CA99", to="+15551112222", from_="+15559876543"))
    assert session.call_identity is not None
    assert session.call_identity.caller_number == "+15551112222"
    assert session.call_identity.called_number == "+15559876543"
    assert session.call_identity.direction == "outbound"
    assert session.call_identity.call_sid == "CA99"


@pytest.mark.asyncio
async def test_outbound_initiated_does_not_clobber_inbound_identity() -> None:
    """If an inbound identity is already set, don't overwrite it with
    an outbound stamp — covers the corner case of a session that
    places a warm-transfer call mid-conversation."""
    from easycat import Session, SessionConfig
    from easycat.events import EventBus as _EventBus
    from easycat.stubs import NoopAgent

    bus = _EventBus()
    session = Session(
        SessionConfig(
            agent=NoopAgent(),
            event_bus=bus,
            runtime_mode="text_session",
            call_identity=CallIdentity(caller_number="+15550000000", direction="inbound"),
        )
    )

    def _on_initiated(event: CallInitiated) -> None:
        if session.call_identity is not None and session.call_identity.direction == "inbound":
            return
        session.call_identity = CallIdentity(
            caller_number=event.to, direction="outbound", call_sid=event.call_sid
        )

    bus.subscribe(CallInitiated, _on_initiated)
    await bus.emit(CallInitiated(call_sid="CA10", to="+15552223333", from_="+15559876543"))
    assert session.call_identity is not None
    assert session.call_identity.direction == "inbound"
    assert session.call_identity.caller_number == "+15550000000"


# ── Session exposure policy ────────────────────────────────────────


@pytest.mark.asyncio
async def test_exposure_system_message_prepends_to_agent_context() -> None:
    bridge = _CaptureBridge()
    stage = AgentStage(bridge)
    async for _ in stage.execute_streaming(
        "hello",
        _ctx(),
        _turn(),
        system_prefix="The caller's phone number is +15551234567.",
    ):
        pass
    assert bridge.captured_contexts
    first_context = bridge.captured_contexts[0]
    assert first_context
    assert first_context[0]["role"] == "system"
    assert "+15551234567" in first_context[0]["content"]


@pytest.mark.asyncio
async def test_exposure_system_message_does_not_pollute_history() -> None:
    """System prefixes are per-turn — they must not persist in the
    stage's shadow history across turns."""
    bridge = _CaptureBridge()
    stage = AgentStage(bridge)
    async for _ in stage.execute_streaming(
        "hello",
        _ctx(),
        _turn(),
        system_prefix="Turn-1 system note",
    ):
        pass
    async for _ in stage.execute_streaming("followup", _ctx(), _turn()):
        pass
    second = bridge.captured_contexts[1]
    assert all(msg.get("content") != "Turn-1 system note" for msg in second)


def test_session_caller_id_message_off_returns_none() -> None:
    from easycat import Session, SessionConfig
    from easycat.stubs import NoopAgent

    session = Session(
        SessionConfig(
            agent=NoopAgent(),
            runtime_mode="text_session",
            call_identity=CallIdentity(caller_number="+15550000000", direction="inbound"),
            caller_id_exposure="off",
        )
    )
    assert session._caller_id.system_message() is None
    assert session.call_identity is None
    assert session._caller_id.private_identity is not None
    assert session._caller_id.private_identity.caller_number == "+15550000000"


@pytest.mark.asyncio
async def test_session_caller_id_off_hides_tools_but_keeps_opt_out_internal() -> None:
    from easycat import Session, SessionConfig
    from easycat.stubs import NoopAgent

    dnc = DNCList()
    session = Session(
        SessionConfig(
            agent=NoopAgent(),
            runtime_mode="text_session",
            dnc_list=dnc,
            call_identity=CallIdentity(caller_number="+15550000000", direction="inbound"),
            caller_id_exposure="off",
        )
    )

    assert session.call_identity is None
    await session.event_bus.emit(STTFinal(text="Please stop calling me"))
    assert dnc.is_on_dnc("+15550000000")


def test_session_caller_id_message_tools_only_hides_from_llm() -> None:
    from easycat import Session, SessionConfig
    from easycat.stubs import NoopAgent

    session = Session(
        SessionConfig(
            agent=NoopAgent(),
            runtime_mode="text_session",
            call_identity=CallIdentity(caller_number="+15550000000", direction="inbound"),
            caller_id_exposure="tools_only",
        )
    )
    assert session._caller_id.system_message() is None
    # …but tool code can still read it directly.
    assert session.call_identity is not None
    assert session.call_identity.caller_number == "+15550000000"


def test_session_caller_id_message_renders_outbound() -> None:
    from easycat import Session, SessionConfig
    from easycat.stubs import NoopAgent

    session = Session(
        SessionConfig(
            agent=NoopAgent(),
            runtime_mode="text_session",
            call_identity=CallIdentity(
                caller_number="+15551112222",
                called_number="+15559876543",
                direction="outbound",
            ),
            caller_id_exposure="system_message",
        )
    )
    message = session._caller_id.system_message()
    assert message is not None
    assert "+15551112222" in message
    assert "outbound" in message.lower()
    assert "They dialed" not in message
    assert "placed from +15559876543" in message


def test_session_caller_id_message_handles_empty_identity() -> None:
    from easycat import Session, SessionConfig
    from easycat.stubs import NoopAgent

    session = Session(
        SessionConfig(
            agent=NoopAgent(),
            runtime_mode="text_session",
            call_identity=CallIdentity(),
            caller_id_exposure="system_message",
        )
    )
    # No numbers + no display name ⇒ nothing to say.
    assert session._caller_id.system_message() is None


# ── Bridge wrapping keeps system prefix even when no shadow history ─


@pytest.mark.asyncio
async def test_agent_stage_forces_context_when_system_prefix_given() -> None:
    """AgentStage wrapped around a non-tracked bridge: even without
    shadow history, the system_prefix must reach the bridge through
    ``turn_input.context``."""
    bridge = _CaptureBridge()
    stage = AgentStage(bridge)
    async for _ in stage.execute_streaming(
        "hi",
        _ctx(),
        _turn(),
        system_prefix="caller note about +15550000000",
    ):
        pass
    assert bridge.captured_contexts[0]
    assert bridge.captured_contexts[0][0]["role"] == "system"
    assert "+15550000000" in bridge.captured_contexts[0][0]["content"]


@pytest.mark.asyncio
async def test_agent_stage_system_prefix_preserves_agent_runner_bridge_history() -> None:
    bridge = _CaptureBridge()
    stage = AgentStage(AgentRunner(bridge))

    async for _ in stage.execute_streaming("first", _ctx(), _turn()):
        pass
    async for _ in stage.execute_streaming(
        "second",
        _ctx(),
        _turn(),
        system_prefix="caller note about +15550000000",
    ):
        pass

    second_context = bridge.captured_contexts[1]
    assert second_context == [
        {"role": "system", "content": "caller note about +15550000000"},
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "ok"},
    ]
