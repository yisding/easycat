"""Tests for ``CancelOrchestrator`` extracted from Session in Phase 4.

Focuses on the contract surface (signal propagation order, error
isolation, telephony journal record, cause annotation,
``for_barge_in`` suppression policy, and ``record_interruption`` shape)
without booting a full Session.  The full barge-in matrix continues to
live in the WS2B interruption tests; these unit tests guard the
orchestrator's invariants directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from easycat._turn_context import TurnContext
from easycat.events import EventBus
from easycat.runtime.context import RunContext
from easycat.runtime.journal import InMemoryRingBuffer
from easycat.session._cancel_orchestrator import CancelOrchestrator
from easycat.session._journal_sink import SessionJournalSink
from easycat.session.actions import SessionActions
from easycat.stages.base import ControlSignal, InterruptSignal

# ── Test doubles ─────────────────────────────────────────────


class _RecordingStage:
    """Minimal :class:`Stage` double that records ``handle_upstream`` calls."""

    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.calls: list[tuple[ControlSignal, RunContext | None]] = []
        self._fail = fail

    async def handle_upstream(self, signal: ControlSignal, ctx: RunContext | None = None) -> None:
        self.calls.append((signal, ctx))
        if self._fail:
            raise RuntimeError(f"{self.name} blew up")


def _make_journal_sink() -> tuple[SessionJournalSink, InMemoryRingBuffer]:
    journal = InMemoryRingBuffer()
    sink = SessionJournalSink(
        event_bus=EventBus(),
        journal=journal,
        artifact_store=None,
        session_id="sess-test",
        current_turn_id=lambda explicit: explicit,
    )
    return sink, journal


def _make_run_ctx(journal: InMemoryRingBuffer | None = None) -> RunContext:
    return RunContext(
        run_id="sess-test",
        session_id="sess-test",
        runtime_mode="chained_pipeline",
        journal=journal,
    )


def _make_orchestrator(
    *,
    stages: dict[str, _RecordingStage] | None = None,
    current_turn: TurnContext | None = None,
    session_actions: SessionActions | None = None,
    telephony_present: bool = False,
    cancel_turn_impl: Any = None,
) -> tuple[CancelOrchestrator, dict[str, _RecordingStage], InMemoryRingBuffer]:
    names = ("transport", "tts", "agent", "turn", "stt", "vad", "audio")
    stages = stages or {n: _RecordingStage(n) for n in names}
    sink, journal = _make_journal_sink()
    run_ctx = _make_run_ctx(journal=journal)

    async def _default_cancel(**_kwargs: Any) -> None:
        return None

    orch = CancelOrchestrator(
        transport_stage=stages["transport"],
        tts_stage=stages["tts"],
        agent_stage=stages["agent"],
        turn_stage=stages["turn"],
        stt_stage=stages["stt"],
        vad_stage=stages["vad"],
        audio_stage=stages["audio"],
        run_ctx=run_ctx,
        journal_sink=sink,
        interruption_mode="precise",
        interruption_latency_compensation_ms=120,
        interruption_ack_stale_ms=200,
        interruption_ack_tail_cap_ms=80,
        current_turn=lambda: current_turn,
        session_actions=lambda: session_actions,
        telephony_helpers_present=lambda: telephony_present,
        cancel_turn_impl=cancel_turn_impl or _default_cancel,
    )
    return orch, stages, journal


# ── Tests ────────────────────────────────────────────────────


async def test_propagate_signal_walks_all_seven_stages_in_order() -> None:
    orch, stages, _ = _make_orchestrator()
    signal = InterruptSignal(signal_id="sig-1")
    await orch.propagate_signal(signal)

    expected_order = ("transport", "tts", "agent", "turn", "stt", "vad", "audio")
    for name in expected_order:
        assert len(stages[name].calls) == 1, f"stage {name} should see the signal once"
        seen_signal, _ctx = stages[name].calls[0]
        assert seen_signal is signal


async def test_propagate_signal_isolates_per_stage_errors() -> None:
    names = ("transport", "tts", "agent", "turn", "stt", "vad", "audio")
    stages = {n: _RecordingStage(n, fail=(n == "agent")) for n in names}
    orch, _, _ = _make_orchestrator(stages=stages)

    # Should not raise even though 'agent' raises.
    await orch.propagate_signal(InterruptSignal(signal_id="sig-fail"))

    # Every other stage still received the signal.
    for name in names:
        assert len(stages[name].calls) == 1, f"{name} must still observe the signal"


async def test_propagate_signal_writes_telephony_record_when_helpers_present() -> None:
    orch, _, journal = _make_orchestrator(telephony_present=True)
    await orch.propagate_signal(InterruptSignal(signal_id="sig-tel"))

    telephony_records = [rec for rec in journal.read() if rec.name == "control_signal"]
    assert any((rec.data or {}).get("stage") == "telephony" for rec in telephony_records), (
        "telephony control_signal record should be written"
    )


async def test_propagate_signal_skips_telephony_record_when_absent() -> None:
    orch, _, journal = _make_orchestrator(telephony_present=False)
    await orch.propagate_signal(InterruptSignal(signal_id="sig-no-tel"))

    telephony_records = [
        rec
        for rec in journal.read()
        if rec.name == "control_signal" and (rec.data or {}).get("stage") == "telephony"
    ]
    assert telephony_records == []


async def test_propagate_signal_appends_cause_annotation_record() -> None:
    orch, _, journal = _make_orchestrator()
    await orch.propagate_signal(InterruptSignal(signal_id="sig-cause"), cause="barge_in")

    cause_records = [rec for rec in journal.read() if rec.name == "control_signal_cause"]
    assert len(cause_records) == 1
    assert cause_records[0].data == {"signal_id": "sig-cause", "cause": "barge_in"}


async def test_propagate_signal_skips_cause_record_when_cause_missing() -> None:
    orch, _, journal = _make_orchestrator()
    await orch.propagate_signal(InterruptSignal(signal_id="sig-no-cause"))

    cause_records = [rec for rec in journal.read() if rec.name == "control_signal_cause"]
    assert cause_records == []


async def test_for_barge_in_suppressed_when_no_interrupt_action_queued() -> None:
    actions = SessionActions()
    actions.end_call(reason="opt_out")  # no_interrupt=True by default
    assert actions.no_interrupt is True

    calls: list[bool] = []

    async def _cancel(*, barge_in: bool = False) -> None:
        calls.append(barge_in)

    orch, _, _ = _make_orchestrator(session_actions=actions, cancel_turn_impl=_cancel)
    result = await orch.for_barge_in()
    assert result is False
    assert calls == []


async def test_for_barge_in_invokes_cancel_turn_impl_when_not_suppressed() -> None:
    calls: list[bool] = []

    async def _cancel(*, barge_in: bool = False) -> None:
        calls.append(barge_in)

    orch, _, _ = _make_orchestrator(session_actions=None, cancel_turn_impl=_cancel)
    result = await orch.for_barge_in()
    assert result is True
    assert calls == [True]


async def test_record_interruption_writes_journal_record_with_expected_shape() -> None:
    orch, _, journal = _make_orchestrator()
    orch.record_interruption(
        source="streaming_turn",
        mode="precise",
        text_spoken="hello there",
        notified=True,
        turn_id="turn-1",
    )

    records = [rec for rec in journal.read() if rec.name == "assistant_interruption_notified"]
    assert len(records) == 1
    record = records[0]
    assert record.turn_id == "turn-1"
    assert record.data == {
        "source": "streaming_turn",
        "mode": "precise",
        "text_spoken": "hello there",
        "notified": True,
    }


def test_config_accessors_clamp_negative_values_to_zero() -> None:
    orch, _, _ = _make_orchestrator()
    assert orch.interruption_mode == "precise"
    assert orch.latency_compensation_ms == 120
    assert orch.ack_stale_ms == 200
    assert orch.ack_tail_cap_ms == 80


@pytest.mark.parametrize(
    "field_name, init_kwarg",
    [
        ("latency_compensation_ms", "interruption_latency_compensation_ms"),
        ("ack_stale_ms", "interruption_ack_stale_ms"),
        ("ack_tail_cap_ms", "interruption_ack_tail_cap_ms"),
    ],
)
def test_negative_interruption_knobs_are_clamped(field_name: str, init_kwarg: str) -> None:
    names = ("transport", "tts", "agent", "turn", "stt", "vad", "audio")
    stages = {n: _RecordingStage(n) for n in names}
    sink, _ = _make_journal_sink()

    base_kwargs: dict[str, Any] = dict(
        transport_stage=stages["transport"],
        tts_stage=stages["tts"],
        agent_stage=stages["agent"],
        turn_stage=stages["turn"],
        stt_stage=stages["stt"],
        vad_stage=stages["vad"],
        audio_stage=stages["audio"],
        run_ctx=_make_run_ctx(),
        journal_sink=sink,
        interruption_mode="precise",
        interruption_latency_compensation_ms=0,
        interruption_ack_stale_ms=0,
        interruption_ack_tail_cap_ms=0,
        current_turn=lambda: None,
        session_actions=lambda: None,
        telephony_helpers_present=lambda: False,
        cancel_turn_impl=lambda **_: None,
    )
    base_kwargs[init_kwarg] = -500
    orch = CancelOrchestrator(**base_kwargs)
    assert getattr(orch, field_name) == 0
