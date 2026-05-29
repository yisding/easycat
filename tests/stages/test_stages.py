"""Tests for WS3: Stage abstraction, RunContext, and Session text mode."""

from __future__ import annotations

import pytest

from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.runtime.context import RunContext
from easycat.runtime.journal import InMemoryRingBuffer
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
        return True


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
    def test_fidelity_required(self):
        from easycat.runtime.replay import ReplayFidelity

        # fidelity has no default; ReplaySpec() must fail.
        with pytest.raises(TypeError):
            ReplaySpec()  # type: ignore[call-arg]
        spec = ReplaySpec(fidelity=ReplayFidelity.ARTIFACT)
        assert spec.fidelity is ReplayFidelity.ARTIFACT
        assert spec.from_sequence is None
        assert spec.to_sequence is None

    def test_with_range(self):
        from easycat.runtime.replay import ReplayFidelity

        spec = ReplaySpec(
            fidelity=ReplayFidelity.LIVE,
            from_sequence=5,
            to_sequence=10,
        )
        assert spec.fidelity is ReplayFidelity.LIVE
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
        from easycat.runtime.replay import ReplayFidelity

        stage = stage_cls(provider_cls())
        # WS4: replay() now returns captured data (None when no overrides).
        result = stage.replay(ReplaySpec(fidelity=ReplayFidelity.ARTIFACT))
        assert result is None or result == []

    @pytest.mark.parametrize(
        "stage_cls,provider_cls",
        _STAGE_CLASSES,
        ids=[c[0].__name__ for c in _STAGE_CLASSES],
    )
    def test_replay_protocol_signature_accepts_cassette(self, stage_cls, provider_cls):
        """The ``Stage.replay`` protocol contract matches its implementors.

        Every concrete stage accepts a ``cassette`` second argument, so a
        caller typed against ``Stage`` must be able to pass one.
        """
        import inspect

        from easycat.runtime.replay import ReplayFidelity

        proto_params = list(inspect.signature(Stage.replay).parameters)
        assert "cassette" in proto_params

        stage = stage_cls(provider_cls())
        impl_params = list(inspect.signature(stage.replay).parameters)
        assert "cassette" in impl_params
        # Passing the protocol's optional cassette must not raise.
        assert stage.replay(ReplaySpec(fidelity=ReplayFidelity.ARTIFACT), None) in (None, [])

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
    async def test_handle_upstream_journals_control_signal_when_ctx_supplied(
        self, stage_cls, provider_cls
    ):
        """WS3 T3.8: every stage's ``handle_upstream`` writes a
        ``ControlSignalRecord`` when called with a ``RunContext``.

        Without ctx (legacy callers), it stays a silent passthrough so
        the protocol change is non-breaking.
        """
        from easycat.runtime.records import JournalRecordKind

        journal = InMemoryRingBuffer(capacity=32)
        ctx = _make_ctx(journal=journal)
        stage = stage_cls(provider_cls())

        await stage.handle_upstream(InterruptSignal(signal_id="sig-1"), ctx)

        records = journal.read()
        signal_records = [r for r in records if r.kind == JournalRecordKind.CONTROL]
        assert len(signal_records) == 1
        rec = signal_records[0]
        assert rec.name == "control_signal"
        assert rec.data["signal_kind"] == "interrupt"
        assert rec.data["signal_id"] == "sig-1"
        assert rec.data["observed_stage"] == stage.name
        assert rec.data["direction"] == "upstream"

    @pytest.mark.parametrize(
        "stage_cls,provider_cls",
        _STAGE_CLASSES,
        ids=[c[0].__name__ for c in _STAGE_CLASSES],
    )
    async def test_handle_upstream_without_ctx_is_silent(self, stage_cls, provider_cls):
        """Legacy callers (no ctx) keep the historical no-op behaviour."""
        from easycat.runtime.records import JournalRecordKind

        journal = InMemoryRingBuffer(capacity=32)
        stage = stage_cls(provider_cls(), journal=journal)
        await stage.handle_upstream(InterruptSignal(signal_id="sig-1"))
        signals = [r for r in journal.read() if r.kind == JournalRecordKind.CONTROL]
        assert signals == []

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

    async def test_constructor_journal_used_when_ctx_journal_is_none(self):
        """Lock the fallback: a constructor journal records even when
        ``ctx.journal`` is None (otherwise the ctor arg would be dead state).
        """
        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=None)
        turn = _make_turn()
        stage = STTStage(_StubSTT(), journal=journal)
        await stage.execute(b"chunk", ctx, turn)
        names = [r.name for r in journal.read()]
        assert "stage_start" in names
        assert "stage_complete" in names

    async def test_agent_constructor_journal_used_when_ctx_journal_is_none(self):
        """AgentStage routes both stage events and the recorder through the
        same fallback journal when ``ctx.journal`` is None.
        """
        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=None)
        turn = _make_turn()
        stage = AgentStage(_StubAgent(), journal=journal)
        result = await stage.execute("hello", ctx, turn)
        assert result == "reply:hello"
        names = [r.name for r in journal.read()]
        assert "stage_start" in names
        assert "stage_complete" in names

    async def test_transport_stage_returns_true_when_send_audio_returns_true(self):
        class _DeliveringTransport:
            async def send_audio(self, chunk):
                return True

        ctx = _make_ctx(journal=None)
        turn = _make_turn()
        stage = TransportStage(_DeliveringTransport())
        delivered = await stage.execute(b"chunk", ctx, turn)
        assert delivered is True

    async def test_transport_stage_returns_false_when_send_audio_returns_false(self):
        class _DisconnectedTransport:
            async def send_audio(self, chunk):
                return False

        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        turn = _make_turn()
        stage = TransportStage(_DisconnectedTransport(), journal=journal)
        delivered = await stage.execute(b"chunk", ctx, turn)
        assert delivered is False
        records = journal.read()
        complete = next(r for r in records if r.name == "stage_complete")
        assert complete.data.get("delivered") is False

    def test_transport_live_replay_returns_captured_outbound_bytes(self):
        """LIVE replay reads the captured outbound bytes from the
        ``stage_complete`` ``output_ref`` (execute never writes ``input_ref``,
        so the old ``stage_start``/``input_ref`` lookup always returned None).
        """
        from easycat.runtime.replay import ReplayCassette, ReplayFidelity

        records = (
            {"name": "stage_start", "data": {}, "input_ref": None, "output_ref": None},
            {"name": "stage_complete", "data": {}, "input_ref": None, "output_ref": "out-1"},
        )
        cassette = ReplayCassette(
            stage_name="transport",
            records=records,
            _resolver=lambda ref: b"outbound" if ref == "out-1" else None,
        )
        stage = TransportStage(_StubTransport())
        assert stage.replay(ReplaySpec(fidelity=ReplayFidelity.LIVE), cassette) == b"outbound"


# ── VAD event serialization + Turn dataclass recording ───────────


class TestReplayDecision:
    async def test_vad_stage_serializes_event_fields(self):
        """``stage_complete`` records reconstructable event descriptors
        (type + dataclass fields), not bare class-name strings."""
        from easycat.events import VADStartSpeaking, VADStopSpeaking

        class _SpeakingVAD:
            async def process(self, chunk):
                yield VADStartSpeaking(turn_id="t-1")
                yield VADStopSpeaking(turn_id="t-1")

            def configure(self, **kwargs):
                pass

        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        turn = _make_turn()
        stage = VADStage(_SpeakingVAD(), journal=journal)
        await stage.execute(b"\x00\x00", ctx, turn)
        complete = next(r for r in journal.read() if r.name == "stage_complete")
        events = complete.data.get("events")
        assert isinstance(events, list)
        assert [e["type"] for e in events] == ["VADStartSpeaking", "VADStopSpeaking"]
        # Payload (timestamps, correlation ids) is preserved for replay.
        assert all(isinstance(e["fields"], dict) for e in events)
        assert all(e["fields"]["turn_id"] == "t-1" for e in events)
        assert all("timestamp" in e["fields"] for e in events)

    def test_vad_replay_decision_none_before_run(self):
        stage = VADStage(_StubVAD())
        assert stage.replay_decision(stage.snapshot_state()) is None

    async def test_vad_replay_decision_returns_last_event(self):
        """After execute, ``snapshot_state`` carries the last event type and
        ``replay_decision`` returns it (was previously dead code → None)."""
        from easycat.events import VADStartSpeaking

        class _SpeakingVAD:
            async def process(self, chunk):
                yield VADStartSpeaking(turn_id="t-1")

            def configure(self, **kwargs):
                pass

        ctx = _make_ctx(journal=None)
        turn = _make_turn()
        stage = VADStage(_SpeakingVAD())
        await stage.execute(b"\x00\x00", ctx, turn)
        assert stage.replay_decision(stage.snapshot_state()) == "VADStartSpeaking"

    def test_turn_replay_decision_none_before_run(self):
        stage = TurnStage(_StubSmartTurn())
        assert stage.replay_decision(stage.snapshot_state()) is None

    async def test_turn_replay_decision_returns_last_prediction(self):
        """After execute, ``replay_decision`` returns the recorded prediction
        (was previously dead code → None)."""
        ctx = _make_ctx(journal=None)
        turn = _make_turn()
        stage = TurnStage(_StubSmartTurn())
        await stage.execute([b"\x00\x00"], ctx, turn)
        assert stage.replay_decision(stage.snapshot_state()) == 1

    async def test_turn_stage_records_dataclass_result(self):
        """``detect`` may return a dataclass; ``stage_complete`` still records
        the prediction/probability keys."""
        import dataclasses as _dc

        @_dc.dataclass
        class _SmartTurnResult:
            prediction: int
            probability: float
            unrelated: str = "ignored"

        class _DataclassSmartTurn:
            async def detect(self, audio_chunks):
                return _SmartTurnResult(prediction=1, probability=0.87)

        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        turn = _make_turn()
        stage = TurnStage(_DataclassSmartTurn(), journal=journal)
        await stage.execute([b"\x00\x00"], ctx, turn)
        complete = next(r for r in journal.read() if r.name == "stage_complete")
        assert complete.data.get("prediction") == 1
        assert complete.data.get("probability") == pytest.approx(0.87)
        assert "unrelated" not in complete.data

    async def test_turn_replay_round_trips_recorded_prediction(self):
        """ARTIFACT replay reads back the ``prediction`` key the stage records,
        and an explicit ``prediction`` override wins (no phantom 'decision')."""
        from easycat.runtime.replay import ReplayCassette, ReplayFidelity

        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        turn = _make_turn()
        stage = TurnStage(_StubSmartTurn(), journal=journal)
        await stage.execute([b"\x00\x00"], ctx, turn)

        records = tuple(
            {"name": r.name, "data": r.data, "input_ref": None, "output_ref": None}
            for r in journal.read()
        )
        cassette = ReplayCassette(stage_name="turn", records=records)
        assert stage.replay(ReplaySpec(fidelity=ReplayFidelity.ARTIFACT), cassette) == 1

        override = stage.replay(
            ReplaySpec(fidelity=ReplayFidelity.ARTIFACT, overrides={"prediction": 0}),
            cassette,
        )
        assert override == 0


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
