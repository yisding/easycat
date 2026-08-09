"""Session journal and accounting tests."""

from __future__ import annotations

import asyncio
import zipfile

import pytest

from easycat._bounded_queue import DropPolicy
from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.events import (
    VADStopSpeaking,
)
from easycat.runtime import InMemoryRingBuffer, SqliteJournal
from easycat.runtime.artifacts import FilesystemArtifactStore, InMemoryArtifactStore
from easycat.runtime.records import JournalRecordKind
from easycat.session._session import Session
from easycat.turn_manager import TurnManagerConfig, TurnManagerState
from tests.session._session_core_helpers import (
    FakeTransport,
    MarkerTTS,
    SegmentingSTT,
    _full_config,
    _make_chunk,
)


@pytest.mark.asyncio
async def test_stop_keeps_sqlite_journal_and_bundle_readable(tmp_path):
    session_id = "sess"
    transport = FakeTransport()
    journal = SqliteJournal(session_id, data_dir=tmp_path)
    artifact_store = FilesystemArtifactStore(session_id, data_dir=tmp_path)
    ref = artifact_store.put(b"artifact-bytes")
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="before_stop",
        session_id=session_id,
        input_ref=ref,
    )
    session = Session(
        _full_config(
            transport=transport,
            journal=journal,
            artifact_store=artifact_store,
            session_id=session_id,
        )
    )

    await session.stop()

    assert (
        journal.append(
            kind=JournalRecordKind.EVENT,
            name="after_stop",
            session_id=session_id,
        )
        == -1
    )

    assert session.journal is not None
    records = session.journal.read()
    assert [record.name for record in records] == ["before_stop"]

    bundle_path = tmp_path / "after-stop-full.zip"
    session.export_debug_bundle(str(bundle_path))
    with zipfile.ZipFile(bundle_path) as zf:
        assert "journal.ndjson" in zf.namelist()
        assert f"artifacts/{ref}.bin" in zf.namelist()


@pytest.mark.asyncio
async def test_stop_keeps_in_memory_bundle_exportable(tmp_path):
    session_id = "sess"
    transport = FakeTransport()
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(artifact_store=artifact_store)
    ref = artifact_store.put(b"artifact-bytes")
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="before_stop",
        session_id=session_id,
        input_ref=ref,
    )
    session = Session(
        _full_config(
            transport=transport,
            journal=journal,
            artifact_store=artifact_store,
            session_id=session_id,
        )
    )

    await session.stop()

    assert artifact_store.get(ref) is None
    assert session.journal is not None
    assert [record.name for record in session.journal.read()] == ["before_stop"]

    bundle_path = tmp_path / "after-stop-light.zip"
    session.export_debug_bundle(str(bundle_path))
    with zipfile.ZipFile(bundle_path) as zf:
        assert f"artifacts/{ref}.bin" in zf.namelist()


@pytest.mark.asyncio
async def test_journaled_task_records_scheduled_and_completed():
    """``RuntimeScope.create_journaled_task`` must write ``task_scheduled`` at creation and
    ``task_completed`` when the coroutine finishes cleanly."""
    journal = InMemoryRingBuffer(capacity=32)
    session = Session(_full_config(journal=journal))
    session._turn = TurnContext("tj-1", CancelToken())

    async def _ok() -> str:
        return "ok"

    task = session._runtime_scope.create_journaled_task(
        _ok(), name="unit_test_task", journal_sink=session._journal_sink
    )
    await task
    # add_done_callback schedules the emit callback — let it run.
    await asyncio.sleep(0)

    names = [r.name for r in journal.read()]
    assert "task_scheduled" in names
    assert "task_completed" in names
    scheduled = next(r for r in journal.read() if r.name == "task_scheduled")
    completed = next(r for r in journal.read() if r.name == "task_completed")
    assert scheduled.data["task_name"] == "unit_test_task"
    assert completed.data["task_name"] == "unit_test_task"


@pytest.mark.asyncio
async def test_journaled_task_records_cancelled():
    journal = InMemoryRingBuffer(capacity=32)
    session = Session(_full_config(journal=journal))
    never_released = asyncio.Event()

    async def _slow() -> None:
        await never_released.wait()

    task = session._runtime_scope.create_journaled_task(
        _slow(), name="slow_task", journal_sink=session._journal_sink
    )
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0)

    names = [r.name for r in journal.read()]
    assert "task_cancelled" in names


@pytest.mark.asyncio
async def test_journaled_task_records_raised():
    journal = InMemoryRingBuffer(capacity=32)
    session = Session(_full_config(journal=journal))

    async def _boom() -> None:
        raise ValueError("explosion")

    task = session._runtime_scope.create_journaled_task(
        _boom(), name="boom_task", journal_sink=session._journal_sink
    )
    try:
        await task
    except ValueError:
        pass
    await asyncio.sleep(0)

    recs = journal.read()
    raised = [r for r in recs if r.name == "task_raised"]
    assert len(raised) == 1
    assert raised[0].data["exc_type"] == "ValueError"


@pytest.mark.asyncio
async def test_turn_state_changed_recorded_on_transition():
    """Every TurnManager state change must land as a journal record —
    no more "why did it go to PROCESSING" bugs that require a logger
    dump to answer.

    Drive the transition directly via start_turn() / end_turn() so the
    test doesn't depend on VAD timing.
    """
    journal = InMemoryRingBuffer(capacity=64)
    session = Session(_full_config(journal=journal))
    session._is_running = True
    try:
        await session.start_turn()
        await session.end_turn()

        transitions = [r for r in journal.read() if r.name == "turn_state_changed"]
        assert transitions, "expected at least one turn_state_changed record"
        reasons = {r.data["reason"] for r in transitions}
        assert "manual_start" in reasons
        assert "manual_end" in reasons
        # Idle → UserSpeaking then UserSpeaking → Processing.
        pairs = {(r.data["from"], r.data["to"]) for r in transitions}
        assert ("idle", "user_speaking") in pairs
        assert ("user_speaking", "processing") in pairs
    finally:
        await session.stop(force=True)


def test_outbound_queue_default_policy_is_drop_newest():
    """The played-back (TTS) queue must not use DROP_OLDEST.

    Dropping the *oldest* unsent bot audio makes the listener hear the
    utterance jump forward mid-sentence; DROP_NEWEST trims only the tail.
    """
    session = Session(_full_config())
    assert session._outbound_queue.policy == DropPolicy.DROP_NEWEST


@pytest.mark.asyncio
async def test_audio_queue_drop_recorded_when_queue_overflows():
    """BoundedAudioQueue drops must land in the journal via the
    ``on_drop`` hook so backpressure is visible from a bundle."""
    journal = InMemoryRingBuffer(capacity=32)
    session = Session(_full_config(journal=journal))
    # Shrink the outbound queue so we can overflow it deterministically.
    q = session._outbound_queue
    q._max_size = 2  # type: ignore[attr-defined]

    chunk = _make_chunk(n_bytes=320)
    await q.put(chunk)
    await q.put(chunk)
    # This one should be dropped (DROP_NEWEST policy — played-back speech
    # trims the tail, never the beginning).
    await q.put(chunk)

    drops = [r for r in journal.read() if r.name == "audio_queue_drop"]
    assert len(drops) == 1
    assert drops[0].data["queue"] == "outbound_audio"
    assert drops[0].data["kind"] == "drop_newest"
    assert drops[0].data["total_drops"] == 1


@pytest.mark.asyncio
async def test_pipeline_heartbeat_emits_records_at_interval():
    """Drive ``_emit_heartbeats`` directly with a short interval and
    verify each record carries the expected shape (loop_lag_ms,
    queue len, drops)."""
    journal = InMemoryRingBuffer(capacity=64)
    session = Session(_full_config(journal=journal))
    session._is_running = True

    task = asyncio.create_task(session._emit_heartbeats(interval_s=0.05))
    try:
        await asyncio.sleep(0.25)
    finally:
        session._is_running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    heartbeats = [r for r in journal.read() if r.name == "pipeline_heartbeat"]
    assert len(heartbeats) >= 2, f"expected at least 2 heartbeats, got {len(heartbeats)}"
    data = heartbeats[0].data
    assert data["interval_ms"] == 50
    assert "loop_lag_ms" in data
    assert "outbound_queue_len" in data
    assert "outbound_queue_drops" in data


@pytest.mark.asyncio
async def test_pause_commit_journals_segment_commit_and_final():
    stt = SegmentingSTT(["hello"])
    journal = InMemoryRingBuffer()
    session = Session(
        _full_config(
            stt=stt,
            journal=journal,
            turn_manager_config=TurnManagerConfig(
                end_of_turn_silence_ms=1000,
                stt_segment_silence_ms=1,
            ),
        )
    )
    session._turn = TurnContext("turn-segment-journal", CancelToken())
    session._turn.stt_has_uncommitted_audio = True
    session._stt_committer.mark_active()
    session._turn_manager._state = TurnManagerState.USER_PAUSED
    session._stt_committer.start_event_loop(session._turn)

    try:
        await session._stt_committer._start_segment_commit(turn=session._turn)
        await session._stt_committer.await_inflight_commit()
        await session._stt_committer.await_pending(session._turn)

        records = [record for record in journal.read() if record.name.startswith("stt_segment_")]
        records_by_name = {record.name: record for record in records}
        assert set(records_by_name) == {
            "stt_segment_commit_requested",
            "stt_segment_final",
            "stt_segment_commit_result",
        }
        assert records_by_name["stt_segment_commit_requested"].data == {
            "segment_index": 1,
            "transcript_text": "",
            "pending_commit_bytes": None,
        }
        assert records_by_name["stt_segment_final"].data == {
            "segment_index": 1,
            "text": "hello",
            "track": None,
            "transcript_text": "hello",
        }
        assert records_by_name["stt_segment_commit_result"].data == {
            "segment_index": 1,
            "committed": True,
            "transcript_text": "",
        }
    finally:
        await session._stt_committer.cancel(session._turn)


@pytest.mark.asyncio
async def test_force_stop_cancels_runtime_scoped_stt_pause_commit() -> None:
    journal = InMemoryRingBuffer(capacity=64)
    session = Session(
        _full_config(
            journal=journal,
            turn_manager_config=TurnManagerConfig(
                end_of_turn_silence_ms=1000,
                stt_segment_silence_ms=1000,
            ),
        )
    )
    session._is_running = True
    session._turn = TurnContext("turn-runtime-scope", CancelToken())
    session._turn.stt_has_uncommitted_audio = True
    session._stt_committer.mark_active()
    session._turn_manager._state = TurnManagerState.USER_PAUSED

    session._stt_committer.schedule(VADStopSpeaking(), turn=session._turn)
    task = session._stt_committer._pause_commit_task
    assert task is not None
    assert session._runtime_scope.tasks("stt_pause_commit") == (task,)

    await session.stop(force=True)

    records = [
        record for record in journal.read() if record.data.get("task_name") == "stt_pause_commit"
    ]
    assert task.cancelled()
    assert session._stt_committer._pause_commit_task is None
    assert session._runtime_scope.empty
    assert [record.name for record in records] == ["task_scheduled", "task_cancelled"]


@pytest.mark.asyncio
async def test_stop_cancels_runtime_scoped_stt_pause_commit() -> None:
    journal = InMemoryRingBuffer(capacity=64)
    session = Session(
        _full_config(
            journal=journal,
            turn_manager_config=TurnManagerConfig(
                end_of_turn_silence_ms=1000,
                stt_segment_silence_ms=1000,
            ),
        )
    )
    session._is_running = True
    session._turn = TurnContext("turn-runtime-scope", CancelToken())
    session._turn.stt_has_uncommitted_audio = True
    session._stt_committer.mark_active()
    session._turn_manager._state = TurnManagerState.USER_PAUSED

    session._stt_committer.schedule(VADStopSpeaking(), turn=session._turn)
    task = session._stt_committer._pause_commit_task
    assert task is not None

    await session.stop()

    records = [
        record for record in journal.read() if record.data.get("task_name") == "stt_pause_commit"
    ]
    assert task.cancelled()
    assert session._stt_committer._pause_commit_task is None
    assert session._runtime_scope.empty
    assert [record.name for record in records] == ["task_scheduled", "task_cancelled"]


@pytest.mark.asyncio
async def test_tts_audio_and_markers_are_journaled_with_artifact_ref():
    artifact_store = InMemoryArtifactStore()
    journal = InMemoryRingBuffer(artifact_store=artifact_store)
    session = Session(
        _full_config(
            tts=MarkerTTS(),
            journal=journal,
            artifact_store=artifact_store,
        )
    )
    session._turn = TurnContext("turn-tts-audio", CancelToken())

    await session._tts_scheduler.synthesizer.synthesize("hello", token=None)

    audio_records = [record for record in journal.read() if record.name == "tts_audio"]
    marker_records = [record for record in journal.read() if record.name == "tts_markers"]
    tts_frame_records = [record for record in journal.read() if record.name == "tts_frame"]

    assert len(audio_records) == 1
    assert audio_records[0].turn_id == "turn-tts-audio"
    # Session-level tts_audio record no longer carries output_ref — WS3
    # T3.9 moved artifact capture into TTSStage, which emits one
    # ``tts_frame`` record per chunk with ``output_ref`` set.
    assert audio_records[0].data == {
        "audio_bytes": 320,
        "duration_ms": 10.0,
        "sample_rate": 16000,
        "channels": 1,
        "sample_width": 2,
        "encoding": "pcm",
        "bypass_gate": False,
    }
    assert len(tts_frame_records) >= 1
    assert tts_frame_records[0].turn_id == "turn-tts-audio"
    assert tts_frame_records[0].output_ref is not None
    assert artifact_store.has(tts_frame_records[0].output_ref)
    assert len(marker_records) == 1
    assert marker_records[0].data == {"markers": [{"word": "hello", "start_ms": 0}]}
