"""Tests for WS3: Stage abstraction, RunContext, and Session text mode."""

from __future__ import annotations

import ast
import hashlib
import struct
from collections.abc import AsyncIterator

import pytest

from easycat import _observability as observability
from easycat._turn_context import TurnContext
from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.cancel import CancelToken
from easycat.integrations.agents.base import (
    AgentBridgeEvent,
    AgentTurnInput,
    NullAgentRecorder,
)
from easycat.runtime import InMemoryRingBuffer
from easycat.runtime.artifacts import InMemoryArtifactStore
from easycat.runtime.context import RunContext
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
from easycat.stages.base import journal_append_event, put_artifact, put_artifact_async

# ── Helpers ──────────────────────────────────────────────────────


def _make_ctx(
    *,
    runtime_mode: str = "chained_pipeline",
    journal: InMemoryRingBuffer | None = None,
    artifact_store: InMemoryArtifactStore | None = None,
    journal_detail: str = "full",
) -> RunContext:
    return RunContext(
        run_id="run-1",
        session_id="sess-1",
        runtime_mode=runtime_mode,
        journal=journal,
        artifact_store=artifact_store,
        journal_detail=journal_detail,
    )


def _make_turn() -> TurnContext:
    return TurnContext(turn_id="turn-1", cancel_token=CancelToken())


@pytest.mark.asyncio
async def test_stage_capture_skips_artifact_puts_when_journal_is_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(artifact_store=artifact_store)
    journal._degraded = True
    ctx = _make_ctx(journal=journal, artifact_store=artifact_store)
    put_calls: list[bytes] = []

    def unexpected_put(
        payload: bytes,
        *,
        artifact_class: str = "debug_verbose",
    ) -> str:
        del artifact_class
        put_calls.append(payload)
        return "unexpected"

    monkeypatch.setattr(artifact_store, "put", unexpected_put)

    payloads = (b"sync-stage-audio", b"async-stage-audio")
    assert put_artifact(ctx, payloads[0]) is None
    assert await put_artifact_async(ctx, payloads[1]) is None
    assert put_calls == []
    assert all(not artifact_store.has(hashlib.sha256(payload).hexdigest()) for payload in payloads)


# ── Stub providers ───────────────────────────────────────────────


class _ContextRecordingBridge:
    COMMITTABLE_BOUNDARIES = {}  # noqa: RUF012 test fake uses shared class fixture

    def __init__(self, response: str = "ok") -> None:
        self.response = response
        self.contexts: list[list[dict[str, str]]] = []
        self.reset_count = 0

    async def invoke(
        self,
        turn_input: AgentTurnInput,
        recorder,
        cancel_token: CancelToken | None = None,
    ) -> AsyncIterator[AgentBridgeEvent]:
        _ = recorder, cancel_token
        self.contexts.append(list(turn_input.context))
        yield AgentBridgeEvent(kind="done", text=f"{self.response}:{turn_input.text}")

    def snapshot_state(self):
        return {}

    def apply_interruption(self, *args, **kwargs) -> None:
        pass

    def replace_last_assistant_text(self, text: str) -> None:
        pass

    def append_interruption_note(self, note: str) -> None:
        pass

    def reset(self) -> None:
        self.reset_count += 1


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
        assert ctx.journal_detail == "full"
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

    def test_invalid_journal_detail(self):
        with pytest.raises(ValueError, match="Unsupported journal_detail"):
            RunContext(
                run_id="r3",
                session_id="s3",
                runtime_mode="chained_pipeline",
                journal_detail="verbose",
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
    def test_stage_journal_record_writes_elapsed_ms_without_budget_metadata(self):
        journal = InMemoryRingBuffer(capacity=100)
        ctx = RunContext(
            run_id="run-1",
            session_id="sess-1",
            runtime_mode="chained_pipeline",
            journal=journal,
        )

        journal_append_event(
            ctx,
            stage="tts",
            name="stage_complete",
            turn_id="turn-1",
            data_extra={"elapsed_ms": 25.0},
        )

        record = journal.read()[0]
        assert record.data["elapsed_ms"] == 25.0
        # Latency is reported, not gated: stage records carry no budget tags.
        assert "latency_budget_exceeded" not in record.data
        assert "latency_budget_violations" not in record.data

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
        complete = next(r for r in records if r.name == "stage_complete")
        assert complete.data["elapsed_ms"] >= 0

    async def test_light_mode_omits_per_frame_stage_records_and_artifacts(self):
        class _StreamingTTS:
            async def synthesize(self, payload):
                _ = payload
                yield type("_Event", (), {"audio": AudioChunk(b"\x00\x00", PCM16_MONO_16K)})()

        journal = InMemoryRingBuffer(capacity=5)
        artifact_store = InMemoryArtifactStore()
        ctx = _make_ctx(
            journal=journal,
            artifact_store=artifact_store,
            journal_detail="light",
        )
        turn = _make_turn()
        stages = (
            AudioStage(_StubNoiseReducer(), journal=journal),
            VADStage(_StubVAD(), journal=journal),
            STTStage(_StubSTT(), journal=journal),
        )

        for _ in range(20):
            for stage in stages:
                await stage.execute(b"\x00\x00", ctx, turn)
            await TransportStage(_StubTransport(), journal=journal).execute(
                b"\x00\x00",
                ctx,
                turn,
            )
            stream = await TTSStage(_StreamingTTS(), journal=journal).execute("hello", ctx, turn)
            assert [event async for event in stream]

        assert journal.read() == []
        assert journal.dropped_records == 0
        assert artifact_store._store == {}

    def test_run_context_keeps_preexisting_positional_config_snapshot_slot(self):
        snapshot = {"provider": "test"}

        ctx = RunContext(
            "run",
            "session",
            "chained_pipeline",
            None,
            None,
            snapshot,
        )

        assert ctx.config_snapshot is snapshot
        assert ctx.journal_detail == "full"

    async def test_light_mode_keeps_stage_failures(self):
        class _BrokenSTT:
            async def send_audio(self, chunk):
                raise RuntimeError("stt failed")

        journal = InMemoryRingBuffer(capacity=5)
        ctx = _make_ctx(journal=journal, journal_detail="light")
        stage = STTStage(_BrokenSTT(), journal=journal)

        with pytest.raises(RuntimeError, match="stt failed"):
            await stage.execute(b"\x00\x00", ctx, _make_turn())

        assert [record.name for record in journal.read()] == ["stage_error"]

    async def test_stt_stage_keeps_journal_without_audio_artifact(self):
        journal = InMemoryRingBuffer(capacity=100)
        artifacts = InMemoryArtifactStore()
        ctx = RunContext(
            run_id="run-1",
            session_id="sess-1",
            runtime_mode="chained_pipeline",
            journal=journal,
            artifact_store=artifacts,
            audio_capture_enabled=lambda: False,
        )

        await STTStage(_StubSTT(), journal=journal).execute(b"chunk", ctx, _make_turn())

        records = journal.read()
        assert [record.name for record in records] == ["stage_start", "stage_complete"]
        assert records[0].input_ref is None
        assert artifacts._current_bytes == 0

    async def test_chunk_capture_decision_survives_later_consent_change(self):
        journal = InMemoryRingBuffer(capacity=100)
        artifacts = InMemoryArtifactStore()
        consent = {"enabled": False}
        ctx = RunContext(
            run_id="run-1",
            session_id="sess-1",
            runtime_mode="chained_pipeline",
            journal=journal,
            artifact_store=artifacts,
            audio_capture_enabled=lambda: consent["enabled"],
        )
        chunk = AudioChunk(data=b"\x01\x00" * 160, format=PCM16_MONO_16K)
        await STTStage(_StubSTT(), journal=journal).execute(chunk, ctx, _make_turn())
        consent["enabled"] = True
        await TransportStage(_StubTransport(), journal=journal).execute(
            chunk,
            ctx,
            _make_turn(),
        )

        assert all(record.input_ref is None for record in journal.read())
        assert all(record.output_ref is None for record in journal.read())
        assert artifacts._current_bytes == 0

    async def test_smart_turn_does_not_capture_mixed_consent_window(self):
        journal = InMemoryRingBuffer(capacity=100)
        artifacts = InMemoryArtifactStore()
        ctx = RunContext(
            run_id="run-1",
            session_id="sess-1",
            runtime_mode="chained_pipeline",
            journal=journal,
            artifact_store=artifacts,
            audio_capture_enabled=lambda: True,
        )
        denied = AudioChunk(data=b"\x01\x00" * 160, format=PCM16_MONO_16K)
        denied._easycat_capture_allowed = False
        allowed = AudioChunk(data=b"\x02\x00" * 160, format=PCM16_MONO_16K)

        await TurnStage(_StubSmartTurn(), journal=journal).execute(
            [denied, allowed],
            ctx,
            _make_turn(),
        )

        start = next(record for record in journal.read() if record.name == "stage_start")
        assert start.input_ref is None
        assert artifacts._current_bytes == 0

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

    async def test_agent_stage_journals_delta_before_yield_on_stream_close(self):
        class _OneDeltaBridge:
            COMMITTABLE_BOUNDARIES = {}  # noqa: RUF012 test fake uses shared class fixture

            async def invoke(
                self,
                turn_input: AgentTurnInput,
                recorder,
                cancel_token: CancelToken | None = None,
            ) -> AsyncIterator[AgentBridgeEvent]:
                _ = turn_input, recorder, cancel_token
                yield AgentBridgeEvent(kind="text_delta", text="delivered")
                yield AgentBridgeEvent(kind="done", text="delivered")

            def snapshot_state(self):
                return {}

            def apply_interruption(self, *args, **kwargs) -> None:
                pass

            def replace_last_assistant_text(self, text: str) -> None:
                pass

            def append_interruption_note(self, note: str) -> None:
                pass

            def reset(self) -> None:
                pass

        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        turn = _make_turn()
        stream = AgentStage(_OneDeltaBridge(), journal=journal).execute_streaming(
            "hello", ctx, turn
        )

        first = await anext(stream)
        assert first.kind == "text_delta"
        assert first.text == "delivered"
        await stream.aclose()

        records = journal.read()
        delta = next(
            r for r in records if r.name == "agent_delta" and r.data.get("type") == "TEXT_DELTA"
        )
        complete = next(r for r in records if r.name == "stage_complete")
        assert delta.data["text"] == "delivered"
        assert complete.data["response"] == "delivered"

    async def test_agent_stage_excludes_cancelled_delta_from_completion_history(self):
        class _CancelledDeltaBridge:
            COMMITTABLE_BOUNDARIES = {}  # noqa: RUF012 test fake uses shared class fixture

            async def invoke(
                self,
                turn_input: AgentTurnInput,
                recorder,
                cancel_token: CancelToken | None = None,
            ) -> AsyncIterator[AgentBridgeEvent]:
                _ = turn_input, recorder, cancel_token
                yield AgentBridgeEvent(kind="text_delta", text="dropped")

            def snapshot_state(self):
                return {}

            def apply_interruption(self, *args, **kwargs) -> None:
                pass

            def replace_last_assistant_text(self, text: str) -> None:
                pass

            def append_interruption_note(self, note: str) -> None:
                pass

            def reset(self) -> None:
                pass

        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        turn = _make_turn()
        turn.cancel_token.cancel()
        stage = AgentStage(_CancelledDeltaBridge(), journal=journal)
        stream = stage.execute_streaming("hello", ctx, turn, cancel_token=turn.cancel_token)

        first = await anext(stream)
        assert first.kind == "text_delta"
        assert first.text == "dropped"
        await stream.aclose()

        records = journal.read()
        complete = next(r for r in records if r.name == "stage_complete")
        assert not any(
            record.name == "agent_delta" and record.data.get("type") == "TEXT_DELTA"
            for record in records
        )
        assert complete.data["response"] == ""
        assert stage._history == []

    async def test_agent_stage_excludes_cancelled_done_from_completion_history(self):
        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        turn = _make_turn()
        turn.cancel_token.cancel()
        stage = AgentStage(_ContextRecordingBridge("unheard"), journal=journal)

        events = [
            event
            async for event in stage.execute_streaming(
                "hello",
                ctx,
                turn,
                cancel_token=turn.cancel_token,
            )
        ]

        assert [event.kind for event in events] == ["done"]
        complete = next(record for record in journal.read() if record.name == "stage_complete")
        assert complete.data["response"] == ""
        assert stage._history == []

    async def test_agent_stage_rechecks_cancellation_after_done_stream_drain(self):
        class _CancelDuringDoneDrainBridge(_ContextRecordingBridge):
            async def invoke(
                self,
                turn_input: AgentTurnInput,
                recorder,
                cancel_token: CancelToken | None = None,
            ) -> AsyncIterator[AgentBridgeEvent]:
                _ = recorder
                yield AgentBridgeEvent(kind="done", text=f"{self.response}:{turn_input.text}")
                assert cancel_token is not None
                cancel_token.cancel()

        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        turn = _make_turn()
        stage = AgentStage(_CancelDuringDoneDrainBridge("unheard"), journal=journal)

        events = [
            event
            async for event in stage.execute_streaming(
                "hello",
                ctx,
                turn,
                cancel_token=turn.cancel_token,
            )
        ]

        assert [event.kind for event in events] == ["done"]
        complete = next(record for record in journal.read() if record.name == "stage_complete")
        assert complete.data["response"] == ""
        assert stage._history == []

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

    @pytest.mark.parametrize("journal_detail", ["off", "full"])
    async def test_tts_stage_early_close_closes_provider_stream(self, journal_detail):
        class _ClosableStreamingTTS:
            def __init__(self):
                self.closed = False

            async def synthesize(self, payload):
                _ = payload
                try:
                    yield type("_Event", (), {"audio": None})()
                    yield type("_Event", (), {"audio": None})()
                finally:
                    self.closed = True

        provider = _ClosableStreamingTTS()
        journal = InMemoryRingBuffer(capacity=100) if journal_detail == "full" else None
        stage = TTSStage(provider, journal=journal)
        stream = await stage.execute(
            "hello",
            _make_ctx(journal=journal, journal_detail=journal_detail),
            _make_turn(),
        )

        await anext(stream)
        await stream.aclose()

        assert provider.closed is True

    async def test_stage_error_recording(self):
        class _FailingAgent:
            async def run(self, text):
                raise ValueError("boom")

        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        turn = _make_turn()
        stage = AgentStage(_FailingAgent(), journal=journal)
        with pytest.raises(ValueError, match="boom") as exc_info:
            await stage.execute("hello", ctx, turn)
        records = journal.read()
        names = [r.name for r in records]
        assert "stage_error" in names
        stage_error = next(r for r in records if r.name == "stage_error")
        assert stage_error.data["stage"] == "agent"
        assert stage_error.data["elapsed_ms"] >= 0
        assert stage_error.data["input_sequence"] == 1
        assert stage_error.data["input_record_ref"] == "cp_1"
        notes = exc_info.value.__notes__
        assert "stage=agent" in notes
        assert "provider=agentrunner" in notes
        assert "sequence=1" in notes
        assert "record_key=cp_1" in notes
        assert any(note.startswith("elapsed_ms=") for note in notes)

    async def test_no_journal_does_not_error(self):
        """Stages should work fine with no journal."""
        ctx = _make_ctx(journal=None)
        turn = _make_turn()
        stage = AgentStage(_StubAgent())
        result = await stage.execute("hello", ctx, turn)
        assert result == "reply:hello"

    async def test_agent_stage_skips_journal_metadata_without_journal(self, monkeypatch):
        """The live agent path avoids replay-only work when journaling is disabled."""
        stage = AgentStage(_StubAgent())
        monkeypatch.setattr(
            stage,
            "snapshot_state",
            lambda: pytest.fail("snapshot_state should not run without a journal"),
        )
        monkeypatch.setattr(
            "easycat.stages.agent.journal_append_event",
            lambda *args, **kwargs: pytest.fail("journal_append_event should not run"),
        )

        result = await stage.execute("hello", _make_ctx(), _make_turn())

        assert result == "reply:hello"

    @pytest.mark.parametrize(
        ("stage", "input", "expected"),
        [
            (TTSStage(_StubTTS()), "hello", "audio:hello"),
            (TransportStage(_StubTransport()), b"audio", True),
        ],
    )
    async def test_tts_output_stages_skip_snapshots_without_capture(
        self, monkeypatch, stage, input, expected
    ):
        """The live path avoids replay state work when both capture sinks are absent."""
        monkeypatch.setattr(
            stage,
            "snapshot_state",
            lambda: pytest.fail("snapshot_state should not run without capture"),
        )

        result = await stage.execute(input, _make_ctx(), _make_turn())

        assert result == expected

    async def test_streaming_tts_skips_frame_inspection_without_capture(self, monkeypatch):
        class _OpaqueEvent:
            @property
            def audio(self):
                pytest.fail("audio replay metadata should not be inspected without capture")

        class _StreamingTTS:
            async def synthesize(self, payload):
                _ = payload
                yield _OpaqueEvent()

        monkeypatch.setattr(observability, "_get_tracer", lambda: None)
        monkeypatch.setattr(observability, "_get_meter", lambda: None)
        monkeypatch.setattr(
            observability,
            "span",
            lambda *args, **kwargs: pytest.fail("unavailable tracing must be skipped"),
        )
        monkeypatch.setattr(
            observability,
            "record_histogram",
            lambda *args, **kwargs: pytest.fail("unavailable metrics must be skipped"),
        )

        stage = TTSStage(_StreamingTTS())
        stream = await stage.execute("hello", _make_ctx(), _make_turn())

        assert isinstance(await anext(stream), _OpaqueEvent)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)

    def test_agent_stage_uses_contextual_null_recorder_without_capture(self):
        stage = AgentStage(
            _StubAgent(),
            session_id="session-1",
            mcp_servers=("weather",),
        )

        recorder = stage._make_recorder("turn-1", _make_ctx())

        assert isinstance(recorder, NullAgentRecorder)
        assert recorder.context.session_id == "session-1"
        assert recorder.context.turn_id == "turn-1"
        assert recorder.context.mcp_servers == ("weather",)

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

    async def test_agent_stage_reset_history_clears_raw_bridge_context(self):
        ctx = _make_ctx()
        stage = AgentStage(_ContextRecordingBridge())

        await stage.execute("secret", ctx, _make_turn())
        assert stage._history == [
            {"role": "user", "content": "secret"},
            {"role": "assistant", "content": "ok:secret"},
        ]

        stage.reset_history()
        bridge = stage._provider
        await stage.execute("after reset", ctx, _make_turn())

        assert bridge.contexts[-1] == []

    async def test_agent_stage_set_provider_clears_history_and_recomputes_tracking(self):
        ctx = _make_ctx()
        stage = AgentStage(_ContextRecordingBridge("first"))

        await stage.execute("secret", ctx, _make_turn())
        assert stage._history

        replacement = _ContextRecordingBridge("second")
        stage.set_provider(replacement)
        await stage.execute("fresh", ctx, _make_turn())

        assert stage._tracks_history is True
        assert replacement.contexts[-1] == []
        assert stage._history == [
            {"role": "user", "content": "fresh"},
            {"role": "assistant", "content": "second:fresh"},
        ]

    async def test_agent_stage_replace_last_assistant_text_journals(self):
        """Routing the post-turn rewrite through the stage records the
        framework-state mutation on the journal recording boundary."""
        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        stage = AgentStage(_StubAgent(), journal=journal)
        stage.replace_last_assistant_text("cleaned", ctx=ctx, turn_id="turn-x")
        records = [r for r in journal.read() if r.name == "replace_last_assistant_text"]
        assert len(records) == 1
        assert records[0].turn_id == "turn-x"
        assert records[0].data["stage"] == "agent"
        assert records[0].data["text"] == "cleaned"

    async def test_agent_stage_apply_interruption_threads_recorder(self):
        """The stage's apply_interruption passes a journal-backed recorder to
        the bridge so four-step interruption records flow to the journal."""
        from easycat.integrations.agents.base import (
            AgentBridgeEvent,
            CancellationMode,
        )
        from tests._bridge_helpers import _TestBridgeBase

        seen: dict[str, object] = {}

        class _RecordingBridge(_TestBridgeBase):
            async def invoke(
                self, turn_input, recorder, cancel_token=None
            ) -> AsyncIterator[AgentBridgeEvent]:
                yield AgentBridgeEvent(kind="done", text="hi")

            def configure_runtime(self, **kwargs):
                pass

            def apply_interruption(
                self, delivered_text, mode, recorder=None, caused_by_signal_id=None
            ):
                seen["delivered"] = delivered_text
                seen["recorder_is_none"] = recorder is None

        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        stage = AgentStage(_RecordingBridge(), journal=journal)
        stage.apply_interruption(
            "heard this",
            CancellationMode.IMMEDIATE_STOP,
            ctx=ctx,
            turn_id="turn-y",
        )
        assert seen["delivered"] == "heard this"
        # A recorder must be threaded so the bridge can journal its
        # four-step atomic interruption write ordering.
        assert seen["recorder_is_none"] is False

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

    async def test_transport_stage_treats_legacy_none_return_as_delivered(self):
        class _LegacyTransport:
            async def send_audio(self, chunk):
                self.sent = chunk

        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        turn = _make_turn()
        transport = _LegacyTransport()
        stage = TransportStage(transport, journal=journal)

        delivered = await stage.execute(b"chunk", ctx, turn)

        assert delivered is True
        assert transport.sent == b"chunk"
        records = journal.read()
        complete = next(r for r in records if r.name == "stage_complete")
        assert complete.data.get("delivered") is True

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
    @pytest.mark.parametrize(
        "audio_format",
        [
            AudioFormat(sample_rate=8_000, channels=2, sample_width=1),
            AudioFormat(
                sample_rate=8_000,
                channels=2,
                sample_width=2,
                encoding="mulaw",
            ),
        ],
    )
    async def test_vad_stage_rejects_non_pcm16_before_capture_or_downmix(
        self,
        audio_format: AudioFormat,
    ):
        class _RecordingVAD:
            def __init__(self):
                self.called = False

            async def process(self, chunk):
                self.called = True
                if False:
                    yield None

        provider = _RecordingVAD()
        journal = InMemoryRingBuffer(capacity=100)
        artifacts = InMemoryArtifactStore()
        stage = VADStage(provider, journal=journal)
        chunk = AudioChunk(data=b"\x10\xf0\x20\xe0", format=audio_format)

        with pytest.raises(ValueError, match="VAD requires PCM16 audio"):
            await stage.execute(
                chunk,
                _make_ctx(journal=journal, artifact_store=artifacts),
                _make_turn(),
            )

        assert provider.called is False
        assert journal.read() == []
        assert artifacts._store == {}

    async def test_vad_stage_artifact_metadata_matches_pre_downmix_geometry(self):
        class _GeometryVAD:
            def __init__(self):
                self.inputs = []

            async def process(self, chunk):
                self.inputs.append(chunk)
                return
                yield  # pragma: no cover

        provider = _GeometryVAD()
        journal = InMemoryRingBuffer(capacity=100)
        artifacts = InMemoryArtifactStore()
        stage = VADStage(provider, journal=journal)
        stereo = AudioChunk(
            data=struct.pack("<hhhh", 1200, -1200, -900, 900),
            format=AudioFormat(sample_rate=48_000, channels=2, sample_width=2),
        )

        await stage.execute(
            stereo,
            _make_ctx(journal=journal, artifact_store=artifacts),
            _make_turn(),
        )

        start = next(record for record in journal.read() if record.name == "stage_start")
        assert start.input_ref is not None
        assert artifacts.get(start.input_ref) == stereo.data
        assert start.data["sample_rate"] == 48_000
        assert start.data["channels"] == 2
        assert start.data["sample_width"] == 2
        assert provider.inputs[0].format.channels == 1
        assert provider.inputs[0].data == struct.pack("<hh", 0, 0)

    async def test_vad_stage_preserves_stereo_frames_split_across_chunks(self):
        class _RecordingVAD:
            def __init__(self):
                self.inputs = []

            async def process(self, chunk):
                self.inputs.append(chunk)
                return
                yield  # pragma: no cover

        provider = _RecordingVAD()
        stage = VADStage(provider)
        stereo = AudioFormat(sample_rate=16_000, channels=2, sample_width=2)
        data = struct.pack("<6h", 100, 300, 1_000, 2_000, 3_000, 4_000)
        ctx = _make_ctx()
        idle_turn = _make_turn()
        active_turn = TurnContext(turn_id="turn-2", cancel_token=CancelToken())

        await stage.execute(AudioChunk(data=data[:6], format=stereo), ctx, idle_turn)
        await stage.execute(AudioChunk(data=data[6:], format=stereo), ctx, active_turn)

        assert [chunk.data for chunk in provider.inputs] == [
            struct.pack("<h", 200),
            struct.pack("<2h", 1_500, 3_500),
        ]

    async def test_vad_stage_preserves_split_frames_across_idle_to_turn_boundary(self):
        """VAD may start a real turn after receiving part of a transport frame."""

        class _RecordingVAD:
            def __init__(self):
                self.inputs = []

            async def process(self, chunk):
                self.inputs.append(chunk)
                return
                yield  # pragma: no cover

        provider = _RecordingVAD()
        stage = VADStage(provider)
        stereo = AudioFormat(sample_rate=16_000, channels=2, sample_width=2)
        data = struct.pack("<6h", 100, 300, 1_000, 2_000, 3_000, 4_000)
        ctx = _make_ctx()
        idle_turn = TurnContext(turn_id="no-turn", cancel_token=CancelToken())

        await stage.execute(AudioChunk(data=data[:6], format=stereo), ctx, idle_turn)
        await stage.execute(AudioChunk(data=data[6:], format=stereo), ctx, _make_turn())

        assert [chunk.data for chunk in provider.inputs] == [
            struct.pack("<h", 200),
            struct.pack("<2h", 1_500, 3_500),
        ]

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

    async def test_vad_stage_closes_overproducing_provider_stream(self):
        """The per-chunk event cap must not retain a provider stream."""

        class _OverproducingVAD:
            def __init__(self) -> None:
                self.closed = False
                self.stream = self._events()

            async def _events(self):
                try:
                    for _ in range(VADStage._MAX_EVENTS_PER_CHUNK + 1):
                        yield object()
                finally:
                    self.closed = True

            def process(self, chunk):
                return self.stream

        provider = _OverproducingVAD()
        result = await VADStage(provider).execute(b"\x00\x00", _make_ctx(), _make_turn())

        assert len(result) == VADStage._MAX_EVENTS_PER_CHUNK
        assert provider.closed is True

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

    async def test_turn_stage_materializes_generator_input_once_for_detection(self):
        chunk = AudioChunk(data=b"\x01\x02", format=PCM16_MONO_16K)
        seen: list[AudioChunk] = []

        class _CapturingSmartTurn:
            async def detect(self, audio_chunks):
                seen.extend(audio_chunks)
                return {"prediction": 1, "probability": 0.95}

        def audio_window():
            yield chunk

        result = await TurnStage(_CapturingSmartTurn()).execute(
            audio_window(),
            _make_ctx(),
            _make_turn(),
        )

        assert result["prediction"] == 1
        assert seen == [chunk]

    async def test_turn_stage_journals_current_prediction_in_state_after(self):
        journal = InMemoryRingBuffer(capacity=100)
        ctx = _make_ctx(journal=journal)
        turn = _make_turn()
        stage = TurnStage(_StubSmartTurn(), journal=journal)

        await stage.execute([b"\x00\x00"], ctx, turn)

        complete = next(r for r in journal.read() if r.name == "stage_complete")
        state_after = complete.data.get("state_after")
        assert isinstance(state_after, str)
        snapshot_expr = ast.parse(state_after, mode="eval").body
        assert isinstance(snapshot_expr, ast.Call)
        fields_expr = next(
            keyword.value for keyword in snapshot_expr.keywords if keyword.arg == "fields"
        )
        fields = ast.literal_eval(fields_expr)
        assert isinstance(fields, dict)
        assert fields["decision"] == 1

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
