"""Tests for typed session actions and executor-backed execution."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from easycat._turn_context import TurnContext
from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.cancel import CancelToken
from easycat.events import (
    SessionActionCompleted,
    SessionActionFailed,
    SessionActionRequested,
    SessionActionStarted,
    TTSEvent,
    TTSEventType,
)
from easycat.noise_reduction import PassthroughNoiseReducer
from easycat.runtime import InMemoryRingBuffer
from easycat.session._session import Session
from easycat.session._types import CallIdentity, SessionConfig
from easycat.session.actions import (
    MAX_DTMF_DIGITS,
    MAX_DTMF_INTER_DIGIT_DELAY_MS,
    AddToDNCAction,
    CustomAction,
    EndCallAction,
    RemoveFromDNCAction,
    SendDTMFAction,
    SessionAction,
    SessionActionExecutor,
    SessionActionResult,
    SessionActions,
    TransferCallAction,
    TransferPlan,
)
from easycat.telephony.compliance import DNCList
from easycat.tts.input import TTSInput
from easycat.turn_manager import TurnManagerConfig


def _make_chunk(n_bytes: int = 320) -> AudioChunk:
    return AudioChunk(data=bytes(n_bytes), format=PCM16_MONO_16K)


class FakeTransport:
    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        if False:
            yield _make_chunk()

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def clear_audio(self) -> None:
        pass


class FakeVAD:
    async def process(self, chunk: AudioChunk) -> AsyncIterator[object]:
        if False:
            yield chunk

    def configure(self, **kwargs: object) -> None:
        pass


class FakeSTT:
    async def start_stream(self) -> None:
        pass

    async def send_audio(self, chunk: AudioChunk) -> None:
        pass

    async def end_stream(self) -> None:
        pass

    async def events(self) -> AsyncIterator[object]:
        if False:
            yield None


class FakeAgent:
    async def run(self, text: str) -> str:
        return text.upper()


class FakeTTS:
    async def synthesize(self, payload: TTSInput) -> AsyncIterator[TTSEvent]:
        yield TTSEvent(type=TTSEventType.AUDIO, audio=_make_chunk())

    async def stop(self) -> None:
        pass

    async def cancel(self) -> None:
        pass


def _config(**overrides: Any) -> SessionConfig:
    defaults: dict[str, Any] = {
        "transport": FakeTransport(),
        "vad": FakeVAD(),
        "stt": FakeSTT(),
        "agent": FakeAgent(),
        "tts": FakeTTS(),
        "noise_reducer": PassthroughNoiseReducer(),
        "enable_noise_reduction": False,
        "turn_manager_config": TurnManagerConfig(end_of_turn_silence_ms=1),
    }
    defaults.update(overrides)
    return SessionConfig(**defaults)


class RecordingExecutor(SessionActionExecutor):
    def __init__(self) -> None:
        self.actions: list[SessionAction] = []

    def supports(self, action: SessionAction) -> bool:
        return isinstance(action, CustomAction)

    async def execute(self, session: Session, action: SessionAction) -> SessionActionResult:
        self.actions.append(action)
        return SessionActionResult(stop_session=True, metadata={"handled": True})


class TestSessionActionsQueue:
    def test_end_call_enqueues_typed_action(self) -> None:
        actions = SessionActions()
        actions.end_call(reason="goodbye")

        drained = actions.drain()

        assert len(drained) == 1
        assert isinstance(drained[0], EndCallAction)
        assert drained[0].reason == "goodbye"
        assert drained[0].no_interrupt is True

    def test_transfer_call_enqueues_plan(self) -> None:
        actions = SessionActions()
        plan = TransferPlan(
            client_message="Connecting you now.",
            post_dial_digits="ww1234",
        )

        actions.transfer_call("+15551234567", reason="billing", plan=plan)
        drained = actions.drain()

        assert len(drained) == 1
        assert isinstance(drained[0], TransferCallAction)
        assert drained[0].target == "+15551234567"
        assert drained[0].reason == "billing"
        assert drained[0].plan == plan

    def test_send_dtmf_enqueues_typed_action(self) -> None:
        actions = SessionActions()
        actions.send_dtmf("1234#", inter_digit_delay_ms=250)

        drained = actions.drain()

        assert len(drained) == 1
        assert isinstance(drained[0], SendDTMFAction)
        assert drained[0].digits == "1234#"
        assert drained[0].inter_digit_delay_ms == 250

    def test_send_dtmf_rejects_excessive_input(self) -> None:
        actions = SessionActions()

        with pytest.raises(ValueError, match="DTMF digits"):
            actions.send_dtmf("1" * (MAX_DTMF_DIGITS + 1))
        with pytest.raises(ValueError, match="inter_digit_delay_ms"):
            actions.send_dtmf("12", inter_digit_delay_ms=MAX_DTMF_INTER_DIGIT_DELAY_MS + 1)
        with pytest.raises(ValueError, match="inter_digit_delay_ms"):
            actions.send_dtmf("12", inter_digit_delay_ms=-1)

    def test_transfer_plan_rejects_excessive_post_dial_digits(self) -> None:
        with pytest.raises(ValueError, match="DTMF digits"):
            TransferPlan(post_dial_digits="w" * (MAX_DTMF_DIGITS + 1))

    def test_custom_action_enqueues_payload(self) -> None:
        actions = SessionActions()
        actions.request("play_hold_music", payload={"track": "jazz"})

        drained = actions.drain()

        assert len(drained) == 1
        assert isinstance(drained[0], CustomAction)
        assert drained[0].name == "play_hold_music"
        assert drained[0].payload == {"track": "jazz"}

    def test_add_to_dnc_enqueues_typed_action(self) -> None:
        actions = SessionActions()
        actions.add_to_dnc("+15551234567", reason="caller requested")

        drained = actions.drain()

        assert len(drained) == 1
        assert isinstance(drained[0], AddToDNCAction)
        assert drained[0].number == "+15551234567"
        assert drained[0].reason == "caller requested"

    def test_remove_from_dnc_enqueues_typed_action(self) -> None:
        actions = SessionActions()
        actions.remove_from_dnc("+15551234567")

        drained = actions.drain()

        assert len(drained) == 1
        assert isinstance(drained[0], RemoveFromDNCAction)
        assert drained[0].number == "+15551234567"

    def test_no_interrupt_tracks_any_queued_action(self) -> None:
        actions = SessionActions()
        actions.send_dtmf("1")
        assert actions.no_interrupt is False

        actions.end_call()
        assert actions.no_interrupt is True

        actions.drain()
        assert actions.no_interrupt is False


@pytest.mark.asyncio
async def test_drain_session_actions_uses_executor_and_emits_lifecycle_events() -> None:
    actions = SessionActions()
    actions.request("route_to_specialist", payload={"team": "billing"})
    executor = RecordingExecutor()
    session = Session(_config(session_actions=actions, action_executors=[executor]))

    requested: list[SessionActionRequested] = []
    started: list[SessionActionStarted] = []
    completed: list[SessionActionCompleted] = []
    failed: list[SessionActionFailed] = []
    session.event_bus.subscribe(SessionActionRequested, requested.append)
    session.event_bus.subscribe(SessionActionStarted, started.append)
    session.event_bus.subscribe(SessionActionCompleted, completed.append)
    session.event_bus.subscribe(SessionActionFailed, failed.append)

    should_stop = await session._drain_session_actions()

    assert should_stop is True
    assert len(executor.actions) == 1
    assert len(requested) == 1
    assert len(started) == 1
    assert len(completed) == 1
    assert not failed
    assert completed[0].result.metadata == {"handled": True}


@pytest.mark.asyncio
async def test_drain_add_to_dnc_applies_to_session_dnc_list(
    caplog: pytest.LogCaptureFixture,
) -> None:
    phone = "+1 (555) 123-4567"
    actions = SessionActions()
    actions.add_to_dnc(phone, reason="caller requested")
    dnc = DNCList()
    session = Session(_config(session_actions=actions, dnc_list=dnc))

    completed: list[SessionActionCompleted] = []
    failed: list[SessionActionFailed] = []
    session.event_bus.subscribe(SessionActionCompleted, completed.append)
    session.event_bus.subscribe(SessionActionFailed, failed.append)

    with caplog.at_level(logging.INFO, logger="easycat.session.actions"):
        should_stop = await session._drain_session_actions()

    # Adding to the DNC list must not end the call.
    assert should_stop is False
    # DNCList normalizes to digits, so query with the same formatting it was added with.
    assert dnc.is_on_dnc(phone)
    assert not failed
    assert len(completed) == 1
    assert completed[0].result.metadata["dnc"] == "add"
    assert completed[0].result.metadata["applied"] is True
    assert phone not in caplog.text
    assert "redacted phone number" in caplog.text.lower()


@pytest.mark.asyncio
async def test_dnc_session_action_lifecycle_is_written_to_journal() -> None:
    actions = SessionActions()
    actions.add_to_dnc("+15551234567", reason="caller requested")
    dnc = DNCList()
    journal = InMemoryRingBuffer(capacity=100)
    session = Session(_config(session_actions=actions, dnc_list=dnc, journal=journal))

    await session._drain_session_actions()

    records = journal.read()
    by_name = {record.name: record for record in records}
    assert {
        "session_action_requested",
        "session_action_started",
        "session_action_completed",
    }.issubset(by_name)

    requested = by_name["session_action_requested"].data or {}
    assert requested["action"]["type"] == "add_to_dnc"
    assert requested["action"]["number"] == "[REDACTED_SESSION_ACTION_VALUE]"
    assert requested["action"]["reason"] == "caller requested"

    started = by_name["session_action_started"].data or {}
    assert started["executor"] == "CoreSessionActionExecutor"
    assert started["action"]["type"] == "add_to_dnc"

    completed = by_name["session_action_completed"].data or {}
    assert completed["executor"] == "CoreSessionActionExecutor"
    assert completed["action"]["type"] == "add_to_dnc"
    assert completed["result"]["metadata"]["dnc"] == "add"
    assert completed["result"]["metadata"]["applied"] is True


@pytest.mark.asyncio
async def test_drain_remove_from_dnc_applies_to_session_dnc_list() -> None:
    dnc = DNCList()
    dnc.add("+1 555 123 4567")
    actions = SessionActions()
    actions.remove_from_dnc("+1 555 123 4567")
    session = Session(_config(session_actions=actions, dnc_list=dnc))

    await session._drain_session_actions()

    assert not dnc.is_on_dnc("+1 555 123 4567")


@pytest.mark.asyncio
async def test_add_to_dnc_falls_back_to_caller_identity() -> None:
    # No number passed (caller just says "stop calling me"), and caller-ID is
    # hidden from tools/LLM — the executor still resolves the private identity.
    actions = SessionActions()
    actions.add_to_dnc(reason="caller requested")
    dnc = DNCList()
    session = Session(
        _config(
            session_actions=actions,
            dnc_list=dnc,
            call_identity=CallIdentity(caller_number="+15551234567"),
            caller_id_exposure="off",
        )
    )

    completed: list[SessionActionCompleted] = []
    session.event_bus.subscribe(SessionActionCompleted, completed.append)

    await session._drain_session_actions()

    assert dnc.is_on_dnc("+15551234567")
    assert completed[0].result.metadata["applied"] is True
    assert completed[0].result.metadata["number"] == "+15551234567"


@pytest.mark.asyncio
async def test_add_to_dnc_explicit_number_takes_precedence_over_caller_identity() -> None:
    # An explicit number wins over the caller fallback (e.g. DNC a third party).
    actions = SessionActions()
    actions.add_to_dnc("+15559999999")
    dnc = DNCList()
    session = Session(
        _config(
            session_actions=actions,
            dnc_list=dnc,
            call_identity=CallIdentity(caller_number="+15551234567"),
        )
    )

    await session._drain_session_actions()

    assert dnc.is_on_dnc("+15559999999")
    assert not dnc.is_on_dnc("+15551234567")


@pytest.mark.asyncio
async def test_remove_from_dnc_falls_back_to_caller_identity() -> None:
    dnc = DNCList()
    dnc.add("+15551234567")
    actions = SessionActions()
    actions.remove_from_dnc()  # no number → resolve from the live caller
    session = Session(
        _config(
            session_actions=actions,
            dnc_list=dnc,
            call_identity=CallIdentity(caller_number="+15551234567"),
            caller_id_exposure="off",
        )
    )

    await session._drain_session_actions()

    assert not dnc.is_on_dnc("+15551234567")


@pytest.mark.asyncio
async def test_add_to_dnc_no_number_and_no_identity_is_noop() -> None:
    actions = SessionActions()
    actions.add_to_dnc()  # no number, and the session has no caller identity
    dnc = DNCList()
    session = Session(_config(session_actions=actions, dnc_list=dnc))

    completed: list[SessionActionCompleted] = []
    session.event_bus.subscribe(SessionActionCompleted, completed.append)

    await session._drain_session_actions()

    assert len(dnc) == 0
    assert completed[0].result.metadata["applied"] is False
    assert completed[0].result.metadata["skipped"] == "no_number"


@pytest.mark.asyncio
async def test_dnc_store_write_failure_is_reported_as_failed_action() -> None:
    class BrokenDNC:
        def add(self, phone: str) -> None:
            raise RuntimeError("locked db")

        def remove(self, phone: str) -> None:
            raise RuntimeError("locked db")

        def is_on_dnc(self, phone: str) -> bool:
            return False

    actions = SessionActions()
    actions.add_to_dnc("+15551234567")
    session = Session(_config(session_actions=actions, dnc_list=BrokenDNC()))

    completed: list[SessionActionCompleted] = []
    failed: list[SessionActionFailed] = []
    session.event_bus.subscribe(SessionActionCompleted, completed.append)
    session.event_bus.subscribe(SessionActionFailed, failed.append)

    await session._drain_session_actions()

    # A real store error surfaces as a failed action, not a misleading completion.
    assert not completed
    assert len(failed) == 1
    assert "locked db" in failed[0].error


@pytest.mark.asyncio
async def test_add_to_dnc_persists_through_sqlite_store(tmp_path) -> None:
    from easycat.telephony.compliance import SQLiteDNCList

    db = tmp_path / "dnc.sqlite"
    store = SQLiteDNCList(db)
    actions = SessionActions()
    actions.add_to_dnc("+15551234567", reason="caller requested")
    session = Session(_config(session_actions=actions, dnc_list=store))

    await session._drain_session_actions()
    store.close()

    # A fresh store at the same path (i.e. a later call / after restart) sees it.
    reopened = SQLiteDNCList(db)
    assert reopened.is_on_dnc("+15551234567")
    reopened.close()


@pytest.mark.asyncio
async def test_drain_add_to_dnc_without_list_is_graceful_noop() -> None:
    actions = SessionActions()
    actions.add_to_dnc("+15551234567")
    session = Session(_config(session_actions=actions))  # no dnc_list configured

    completed: list[SessionActionCompleted] = []
    failed: list[SessionActionFailed] = []
    session.event_bus.subscribe(SessionActionCompleted, completed.append)
    session.event_bus.subscribe(SessionActionFailed, failed.append)

    should_stop = await session._drain_session_actions()

    # A missing dnc_list is a logged no-op, not a turn-crashing failure.
    assert should_stop is False
    assert not failed
    assert len(completed) == 1
    assert completed[0].result.metadata["applied"] is False
    assert completed[0].result.metadata["skipped"] == "no_dnc_list"


@pytest.mark.asyncio
async def test_dnc_noop_log_omits_full_phone_number(
    caplog: pytest.LogCaptureFixture,
) -> None:
    phone = "+15551234567"
    actions = SessionActions()
    actions.add_to_dnc(phone)
    session = Session(_config(session_actions=actions))

    with caplog.at_level(logging.WARNING, logger="easycat.session.actions"):
        await session._drain_session_actions()

    assert phone not in caplog.text
    assert "redacted phone number" in caplog.text.lower()


@pytest.mark.asyncio
async def test_drain_session_actions_emits_failure_when_unsupported() -> None:
    actions = SessionActions()
    actions.request("unsupported")
    session = Session(_config(session_actions=actions))

    failures: list[SessionActionFailed] = []
    session.event_bus.subscribe(SessionActionFailed, failures.append)

    should_stop = await session._drain_session_actions()

    assert should_stop is False
    assert len(failures) == 1
    assert isinstance(failures[0].action, CustomAction)
    assert "No session action executor" in failures[0].error


@pytest.mark.asyncio
async def test_streaming_agent_path_stops_session_after_end_call_action() -> None:
    actions = SessionActions()
    actions.end_call(reason="done")
    session = Session(_config(session_actions=actions))
    session.stop = AsyncMock()  # type: ignore[method-assign]
    session._turn = TurnContext(turn_id="turn-1", cancel_token=CancelToken())

    await session._turn_runner.run_streaming_agent("hello", CancelToken())

    session.stop.assert_awaited_once()
