"""Tests for WS3: Stage abstraction, RunContext, and Session text mode."""

from __future__ import annotations

import pytest

from easycat.cancel import CancelToken
from easycat.runtime.context import RunContext
from easycat.runtime.journal import InMemoryRingBuffer
from easycat.session._interruption_controller import InterruptionController
from easycat.session._turn_context import TurnContext
from easycat.session._voice_delivery_ledger import VoiceDeliveryLedger
from easycat.stages import (
    NONDETERMINISTIC_FIELDS,
    AgentStage,
    AudioStage,
    BackpressureSignal,
    CancelSignal,
    ControlSignal,
    InterruptSignal,
    PauseSignal,
    ReplaySpec,
    ResumeSignal,
    Stage,
    StageStateSnapshot,
    STTStage,
    TelephonyStage,
    TransportStage,
    TTSStage,
    TurnStage,
    VADStage,
)

# ── Helpers ──────────────────────────────────────────────────────


def _make_ctx(
    *,
    runtime_mode: str = "chained_pipeline",
    journal: InMemoryRingBuffer | None = None,
) -> RunContext:
    return RunContext(
        run_id="run-1",
        session_id="sess-1",
        runtime_mode=runtime_mode,
        journal=journal,
    )


def _make_turn() -> TurnContext:
    return TurnContext(turn_id="turn-1", cancel_token=CancelToken())


# ── Stub providers ───────────────────────────────────────────────


class _StubSTT:
    async def send_audio(self, chunk):
        pass


class _StubTTS:
    def synthesize(self, payload):
        return f"audio:{payload}"


class _StubAgent:
    async def run(self, text: str) -> str:
        return f"reply:{text}"


class _StubTransport:
    async def send_audio(self, chunk):
        pass


class _StubNoiseReducer:
    async def process(self, chunk):
        return chunk


class _StubEchoCanceller:
    async def process(self, chunk):
        return chunk


class _StubVAD:
    async def process(self, chunk):
        return
        yield

    def configure(self, **kwargs):
        pass


class _StubSmartTurn:
    async def detect(self, audio_chunks):
        return {"prediction": 1, "probability": 0.95}


class _StubTelephony:
    pass


# ── RunContext ───────────────────────────────────────────────────


class TestRunContext:
    def test_construction(self):
        ctx = RunContext(
            run_id="r1",
            session_id="s1",
            runtime_mode="chained_pipeline",
        )
        assert ctx.run_id == "r1"
        assert ctx.session_id == "s1"
        assert ctx.runtime_mode == "chained_pipeline"
        assert ctx.journal is None
        assert ctx.artifact_store is None
        assert ctx.config_snapshot == {}

    def test_text_session_mode(self):
        ctx = RunContext(
            run_id="r2",
            session_id="s2",
            runtime_mode="text_session",
        )
        assert ctx.runtime_mode == "text_session"

    def test_invalid_runtime_mode(self):
        with pytest.raises(ValueError, match="Unsupported runtime_mode"):
            RunContext(
                run_id="r3",
                session_id="s3",
                runtime_mode="realtime",
            )

    def test_frozen(self):
        ctx = RunContext(run_id="r1", session_id="s1", runtime_mode="chained_pipeline")
        with pytest.raises(AttributeError):
            ctx.run_id = "other"

    def test_config_snapshot(self):
        ctx = RunContext(
            run_id="r1",
            session_id="s1",
            runtime_mode="chained_pipeline",
            config_snapshot={"key": "value"},
        )
        assert ctx.config_snapshot == {"key": "value"}

    def test_journal_attached(self):
        j = InMemoryRingBuffer(capacity=100)
        ctx = RunContext(
            run_id="r1",
            session_id="s1",
            runtime_mode="chained_pipeline",
            journal=j,
        )
        assert ctx.journal is j


# ── ControlSignal types ──────────────────────────────────────────


class TestControlSignals:
    def test_interrupt_signal(self):
        sig = InterruptSignal(signal_id="int-1")
        assert sig.signal_id == "int-1"
        assert isinstance(sig, ControlSignal)

    def test_cancel_signal(self):
        sig = CancelSignal(signal_id="can-1")
        assert isinstance(sig, ControlSignal)

    def test_pause_signal(self):
        sig = PauseSignal(signal_id="pau-1")
        assert isinstance(sig, ControlSignal)

    def test_resume_signal(self):
        sig = ResumeSignal(signal_id="res-1")
        assert isinstance(sig, ControlSignal)

    def test_backpressure_signal(self):
        sig = BackpressureSignal(signal_id="bp-1")
        assert isinstance(sig, ControlSignal)

    def test_all_frozen(self):
        for cls in (InterruptSignal, CancelSignal, PauseSignal, ResumeSignal, BackpressureSignal):
            sig = cls(signal_id="test")
            with pytest.raises(AttributeError):
                sig.signal_id = "other"


# ── StageStateSnapshot ───────────────────────────────────────────


class TestStageStateSnapshot:
    def test_construction(self):
        snap = StageStateSnapshot(stage_name="stt")
        assert snap.stage_name == "stt"
        assert snap.fields == {}
        assert snap.state_ref is None

    def test_with_fields(self):
        snap = StageStateSnapshot(
            stage_name="tts",
            fields={"model": "gpt-4o-mini-tts"},
            state_ref="sha256:abc",
        )
        assert snap.fields["model"] == "gpt-4o-mini-tts"
        assert snap.state_ref == "sha256:abc"

    def test_frozen(self):
        snap = StageStateSnapshot(stage_name="vad")
        with pytest.raises(AttributeError):
            snap.stage_name = "other"


# ── NONDETERMINISTIC_FIELDS ──────────────────────────────────────


class TestNondeterministicFields:
    def test_is_frozenset(self):
        assert isinstance(NONDETERMINISTIC_FIELDS, frozenset)

    def test_expected_fields(self):
        assert "timing.wall_ns" in NONDETERMINISTIC_FIELDS
        assert "timing.cpu_ns" in NONDETERMINISTIC_FIELDS
        assert "timing.mono_ns" in NONDETERMINISTIC_FIELDS
        assert "cursor.entered_at" in NONDETERMINISTIC_FIELDS

    def test_immutable(self):
        with pytest.raises(AttributeError):
            NONDETERMINISTIC_FIELDS.add("foo")


# ── ReplaySpec ───────────────────────────────────────────────────


class TestReplaySpec:
    def test_defaults(self):
        spec = ReplaySpec()
        assert spec.fidelity == "artifact"
        assert spec.from_sequence is None
        assert spec.to_sequence is None

    def test_with_range(self):
        spec = ReplaySpec(fidelity="full", from_sequence=5, to_sequence=10)
        assert spec.fidelity == "full"
        assert spec.from_sequence == 5
        assert spec.to_sequence == 10


# ── Stage protocol conformance ───────────────────────────────────


_STAGE_CLASSES = [
    (STTStage, _StubSTT),
    (TTSStage, _StubTTS),
    (AgentStage, _StubAgent),
    (TransportStage, _StubTransport),
    (VADStage, _StubVAD),
    (TurnStage, _StubSmartTurn),
    (TelephonyStage, _StubTelephony),
]


class TestStageProtocol:
    @pytest.mark.parametrize(
        "stage_cls,provider_cls",
        _STAGE_CLASSES,
        ids=[c[0].__name__ for c in _STAGE_CLASSES],
    )
    def test_has_name(self, stage_cls, provider_cls):
        stage = stage_cls(provider_cls())
        assert isinstance(stage.name, str)
        assert stage.name

    @pytest.mark.parametrize(
        "stage_cls,provider_cls",
        _STAGE_CLASSES,
        ids=[c[0].__name__ for c in _STAGE_CLASSES],
    )
    def test_snapshot_state(self, stage_cls, provider_cls):
        stage = stage_cls(provider_cls())
        snap = stage.snapshot_state()
        assert isinstance(snap, StageStateSnapshot)
        assert snap.stage_name == stage.name

    @pytest.mark.parametrize(
        "stage_cls,provider_cls",
        _STAGE_CLASSES,
        ids=[c[0].__name__ for c in _STAGE_CLASSES],
    )
    def test_replay_returns_without_error(self, stage_cls, provider_cls):
        stage = stage_cls(provider_cls())
        # WS4: replay() now returns captured data (None when no overrides)
        result = stage.replay(ReplaySpec())
        # With no overrides, result should be None or empty
        assert result is None or result == []

    @pytest.mark.parametrize(
        "stage_cls,provider_cls",
        _STAGE_CLASSES,
        ids=[c[0].__name__ for c in _STAGE_CLASSES],
    )
    async def test_handle_upstream(self, stage_cls, provider_cls):
        stage = stage_cls(provider_cls())
        # Should not raise
        await stage.handle_upstream(InterruptSignal(signal_id="test"))

    @pytest.mark.parametrize(
        "stage_cls,provider_cls",
        _STAGE_CLASSES,
        ids=[c[0].__name__ for c in _STAGE_CLASSES],
    )
    def test_runtime_checkable(self, stage_cls, provider_cls):
        stage = stage_cls(provider_cls())
        assert isinstance(stage, Stage)


class TestAudioStageProtocol:
    """AudioStage has extra ctor params; test separately."""

    def test_has_name(self):
        stage = AudioStage(_StubNoiseReducer(), echo_canceller=_StubEchoCanceller())
        assert stage.name == "audio"

    def test_snapshot_state(self):
        stage = AudioStage(_StubNoiseReducer(), echo_canceller=_StubEchoCanceller())
        snap = stage.snapshot_state()
        assert snap.stage_name == "audio"
        assert "noise_reducer" in snap.fields
        assert "echo_canceller" in snap.fields

    async def test_execute(self):
        stage = AudioStage(_StubNoiseReducer(), echo_canceller=_StubEchoCanceller())
        ctx = _make_ctx()
        turn = _make_turn()
        result = await stage.execute(b"audio-data", ctx, turn)
        assert result == b"audio-data"

    def test_runtime_checkable(self):
        stage = AudioStage(_StubNoiseReducer())
        assert isinstance(stage, Stage)


# ── Stage execute with journal recording ─────────────────────────


class TestStageExecuteRecording:
    async def test_stt_stage_records(self):
        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        turn = _make_turn()
        stage = STTStage(_StubSTT(), journal=journal)
        await stage.execute(b"chunk", ctx, turn)
        records = journal.read()
        names = [r.name for r in records]
        assert "stage_start" in names
        assert "stage_complete" in names

    async def test_agent_stage_records(self):
        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        turn = _make_turn()
        stage = AgentStage(_StubAgent(), journal=journal)
        result = await stage.execute("hello", ctx, turn)
        assert result == "reply:hello"
        records = journal.read()
        names = [r.name for r in records]
        assert "stage_start" in names
        assert "stage_complete" in names

    async def test_tts_stage_records(self):
        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        turn = _make_turn()
        stage = TTSStage(_StubTTS(), journal=journal)
        result = await stage.execute("hello", ctx, turn)
        assert result == "audio:hello"
        records = journal.read()
        names = [r.name for r in records]
        assert "stage_start" in names
        assert "stage_complete" in names

    async def test_stage_error_recording(self):
        class _FailingAgent:
            async def run(self, text):
                raise ValueError("boom")

        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        turn = _make_turn()
        stage = AgentStage(_FailingAgent(), journal=journal)
        with pytest.raises(ValueError, match="boom"):
            await stage.execute("hello", ctx, turn)
        records = journal.read()
        names = [r.name for r in records]
        assert "stage_error" in names

    async def test_no_journal_does_not_error(self):
        """Stages should work fine with no journal."""
        ctx = _make_ctx(journal=None)
        turn = _make_turn()
        stage = AgentStage(_StubAgent())
        result = await stage.execute("hello", ctx, turn)
        assert result == "reply:hello"


# ── VAD and Turn replay_decision stubs ───────────────────────────


class TestReplayDecision:
    def test_vad_replay_decision(self):
        stage = VADStage(_StubVAD())
        snap = stage.snapshot_state()
        # WS4: replay_decision now returns the decision from snapshot fields
        result = stage.replay_decision(snap)
        assert result is None  # no "decision" field in default snapshot

    def test_turn_replay_decision(self):
        stage = TurnStage(_StubSmartTurn())
        snap = stage.snapshot_state()
        # WS4: replay_decision now returns the decision from snapshot fields
        result = stage.replay_decision(snap)
        assert result is None  # no "decision" field in default snapshot


# ── InterruptionController ───────────────────────────────────────


class TestInterruptionController:
    def test_basic_flow(self):
        journal = InMemoryRingBuffer(capacity=100)
        ctrl = InterruptionController(journal=journal)
        ctrl.signal_interrupt("barge_in", delivered_text="Hello")
        records = journal.read()
        assert any(r.name == "interruption_signal" for r in records)
        assert any(r.data.get("cause") == "barge_in" for r in records)

    def test_no_journal(self):
        ctrl = InterruptionController()
        # Should not raise even without a journal
        ctrl.signal_interrupt("timeout")

    def test_with_bridge(self):
        class _MockBridge:
            applied = False

            def apply_interruption(self, *, delivered_text, mode):
                self.applied = True

        bridge = _MockBridge()
        ctrl = InterruptionController()
        ctrl.signal_interrupt("barge_in", bridge=bridge, delivered_text="Hi")
        assert bridge.applied

    def test_bridge_failure_sets_downgrade(self):
        class _FailBridge:
            def apply_interruption(self, **kwargs):
                raise RuntimeError("fail")

        ctrl = InterruptionController()
        ctrl.signal_interrupt("barge_in", bridge=_FailBridge())
        assert ctrl._pending_downgrade

    def test_text_interrupt(self):
        journal = InMemoryRingBuffer(capacity=100)
        ctrl = InterruptionController(journal=journal)
        ctrl.signal_text_interrupt("new input")
        records = journal.read()
        assert any("text_interrupt" in r.data.get("cause", "") for r in records)


# ── VoiceDeliveryLedger ──────────────────────────────────────────


class TestVoiceDeliveryLedger:
    def test_voice_mode(self):
        ledger = VoiceDeliveryLedger()
        ledger.record_agent_text("Hello world")
        # In voice mode, agent text is NOT auto-delivered
        assert ledger.delivered_text == ""

    def test_text_mode(self):
        ledger = VoiceDeliveryLedger(text_mode=True)
        ledger.record_agent_text("Hello")
        ledger.record_agent_text(" world")
        assert ledger.delivered_text == "Hello world"

    def test_mark_delivered(self):
        ledger = VoiceDeliveryLedger()
        ledger.record_agent_text("Full response")
        ledger.mark_delivered("Full")
        assert ledger.delivered_text == "Full"

    def test_record_spoken_text(self):
        ledger = VoiceDeliveryLedger()
        ledger.record_spoken_text("spoken part")
        assert ledger._spoken_text == "spoken part"

    def test_reset(self):
        ledger = VoiceDeliveryLedger(text_mode=True)
        ledger.record_agent_text("Hello")
        ledger.record_spoken_text("spoken")
        ledger.reset()
        assert ledger.delivered_text == ""
        assert ledger._raw_agent_text == ""
        assert ledger._spoken_text == ""


# ── Text mode (create_text_session / send_text) ──────────────────


class TestTextMode:
    def test_create_text_session(self):
        from easycat.config import create_text_session

        class _SimpleAgent:
            async def run(self, text: str) -> str:
                return f"echo:{text}"

        session = create_text_session(agent=_SimpleAgent(), wrap_agent=False)
        assert session._runtime_mode == "text_session"

    async def test_send_text(self):
        from easycat.config import create_text_session

        class _SimpleAgent:
            async def run(self, text: str) -> str:
                return f"echo:{text}"

        session = create_text_session(agent=_SimpleAgent(), wrap_agent=False)
        result = await session.send_text("hello")
        assert result == "echo:hello"

    async def test_send_text_raises_in_chained_mode(self):
        """send_text must raise RuntimeError when not in text_session mode."""
        # We can't easily create a chained_pipeline Session without
        # real providers, so we directly test the guard by constructing
        # a text_session and then overriding the mode.
        from easycat.config import create_text_session
        from easycat.stubs import NoopAgent

        session = create_text_session(agent=NoopAgent(), wrap_agent=False)
        session._runtime_mode = "chained_pipeline"
        with pytest.raises(RuntimeError, match="text_session"):
            await session.send_text("hi")


# ── Guardrails ───────────────────────────────────────────────────


class TestGuardrails:
    def test_no_realtime_mode(self):
        """RunContext rejects 'realtime' as a runtime_mode."""
        with pytest.raises(ValueError, match="Unsupported runtime_mode"):
            RunContext(
                run_id="r1",
                session_id="s1",
                runtime_mode="realtime",
            )

    def test_no_invalid_mode(self):
        with pytest.raises(ValueError, match="Unsupported runtime_mode"):
            RunContext(
                run_id="r1",
                session_id="s1",
                runtime_mode="streaming",
            )
