"""Tests for outbound call state machine."""

from __future__ import annotations

import asyncio

import pytest

from easycat.events import (
    CallAnswered,
    EventBus,
    TTSAudio,
    VoicemailDetected,
)
from easycat.telephony.call_state import (
    ClassificationGate,
    OutboundCallState,
    OutboundCallStateMachine,
)


class TestClassificationGate:
    @pytest.mark.asyncio
    async def test_gate_buffers_agent_tts_during_classifying(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0)
        gate.start()
        try:
            gate.close()
            ev = TTSAudio(
                chunk=AudioChunk(
                    data=b"\x00" * 100,
                    format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
                )
            )
            await bus.emit(ev)
            assert len(gate.buffer) == 1
            assert gate.is_buffering
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_stale_timeout_cannot_mutate_reopened_gate(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        replay_started = asyncio.Event()
        release_replay = asyncio.Event()
        replay_completed = asyncio.Event()
        gate = ClassificationGate(bus, enabled=True, timeout_s=0)

        async def _replay(_events: list[TTSAudio]) -> None:
            replay_started.set()
            await release_replay.wait()
            replay_completed.set()

        gate.set_flush_async_callback(_replay)
        gate.start()
        old_timeout: asyncio.Task[None] | None = None
        try:
            gate.close()
            old_timeout = gate._timeout_task
            assert old_timeout is not None
            fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
            await bus.emit(TTSAudio(chunk=AudioChunk(data=b"old", format=fmt)))
            await asyncio.wait_for(replay_started.wait(), timeout=1)

            gate.stop()
            gate._timeout_s = 60
            gate.start()
            gate.close()
            new_event = TTSAudio(chunk=AudioChunk(data=b"new", format=fmt))
            await bus.emit(new_event)

            release_replay.set()
            await asyncio.gather(old_timeout, return_exceptions=True)

            assert old_timeout.cancelled()
            assert not replay_completed.is_set()
            assert gate.is_buffering
            assert gate.buffer == [new_event]
            assert gate._started
        finally:
            release_replay.set()
            gate.stop()
            if old_timeout is not None:
                await asyncio.gather(old_timeout, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_gate_overflow_drops_newest_preserving_opener_start(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        # Small timeout -> small buffer cap (50 frames/s * 1s floor = 50).
        gate = ClassificationGate(bus, enabled=True, timeout_s=0.5)
        gate.start()
        try:
            gate.close()
            cap = gate._buffer_max
            fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
            # Emit one extra frame beyond capacity.
            for i in range(cap + 5):
                await bus.emit(TTSAudio(chunk=AudioChunk(data=bytes([i % 256]) * 100, format=fmt)))
            buf = gate.buffer
            assert len(buf) == cap
            # The first (oldest) frame survives so the opener start is intact.
            assert buf[0].chunk.data[0] == 0
            # Newest frames were dropped, surfaced as a metric.
            assert gate.dropped_frames == 5
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_releases_on_amd_result(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        flushed: list[list[TTSAudio]] = []
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0, on_flush=flushed.append)
        gate.start()
        try:
            gate.close()
            ev = TTSAudio(
                chunk=AudioChunk(
                    data=b"\x00" * 100,
                    format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
                )
            )
            await bus.emit(ev)
            assert len(gate.buffer) == 1
            released = gate.release()
            assert len(released) == 1
            assert not gate.is_buffering
            assert len(flushed) == 1
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_releases_on_stt_classification(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0)
        gate.start()
        try:
            gate.close()
            ev = TTSAudio(
                chunk=AudioChunk(
                    data=b"\x00" * 100,
                    format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
                )
            )
            await bus.emit(ev)
            released = gate.release()
            assert len(released) == 1
            assert not gate.is_buffering
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_releases_on_timeout(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=0.05)
        gate.start()
        try:
            gate.close()
            ev = TTSAudio(
                chunk=AudioChunk(
                    data=b"\x00" * 100,
                    format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
                )
            )
            await bus.emit(ev)
            assert gate.is_buffering
            await asyncio.sleep(0.3)
            assert not gate.is_buffering
            assert len(gate.buffer) == 0  # Flushed.
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_timeout_replay_survives_concurrent_gate_release(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        replay_started = asyncio.Event()
        release_replay = asyncio.Event()
        replay_finished = asyncio.Event()
        replayed: list[list[TTSAudio]] = []
        gate = ClassificationGate(bus, enabled=True, timeout_s=0)

        async def _replay(events: list[TTSAudio]) -> None:
            replayed.append(events)
            replay_started.set()
            await release_replay.wait()
            replay_finished.set()

        gate.set_flush_async_callback(_replay)
        gate.start()
        timeout_task: asyncio.Task[None] | None = None
        try:
            gate.close()
            timeout_task = gate._tasks.tasks()[0]
            event = TTSAudio(
                chunk=AudioChunk(
                    data=b"\x00" * 100,
                    format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
                )
            )
            await bus.emit(event)
            await asyncio.wait_for(replay_started.wait(), timeout=1)

            # A classification signal arriving during replay opens the gate.
            # It must not cancel the timeout task after that task dequeued the
            # only copy of the opener audio.
            gate.release()
            release_replay.set()
            await asyncio.wait_for(replay_finished.wait(), timeout=1)

            assert replayed == [[event]]
            assert not timeout_task.cancelled()
        finally:
            release_replay.set()
            gate.stop()
            if timeout_task is not None:
                await asyncio.gather(timeout_task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_reclosing_gate_replaces_existing_timeout(self) -> None:
        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0)
        gate.start()
        try:
            gate.close()
            first_epoch = gate._timeout_epoch.capture()
            first_timeout = gate._tasks.tasks()[0]

            gate.close()
            second_epoch = gate._timeout_epoch.capture()
            second_timeout = gate._tasks.tasks()[0]
            await asyncio.sleep(0)

            assert not first_epoch.is_current()
            assert second_epoch.is_current()
            assert first_timeout.cancelled()
            assert second_timeout is not first_timeout
            assert gate._tasks.active("classification_gate_timeout")
        finally:
            gate.stop()

        assert gate._tasks.empty

    @pytest.mark.asyncio
    async def test_gate_releases_on_first_signal(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0)
        gate.start()
        try:
            gate.close()
            ev = TTSAudio(
                chunk=AudioChunk(
                    data=b"\x00" * 100,
                    format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
                )
            )
            await bus.emit(ev)
            gate.release()
            assert not gate.is_buffering
            # Second release is a no-op.
            second = gate.release()
            assert len(second) == 0
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_hold_audio_plays(self) -> None:
        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0, hold_audio="hold.wav")
        gate.start()
        try:
            gate.close()
            assert gate._hold_audio_playing
            gate.release()
            assert not gate._hold_audio_playing
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_flushes_empty_buffer_to_stop_hold_audio(self) -> None:
        """An early classification still needs the flush callback's cleanup."""
        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0, hold_audio="hold.wav")
        flushed: list[list[TTSAudio]] = []

        async def _flush(events: list[TTSAudio]) -> None:
            flushed.append(events)

        gate.set_flush_async_callback(_flush)
        gate.start()
        try:
            gate.close()
            await gate.flush_and_release()

            assert flushed == [[]]
            assert not gate.is_buffering
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_disabled_no_buffering(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        gate = ClassificationGate(bus, enabled=False)
        gate.start()
        try:
            gate.close()
            ev = TTSAudio(
                chunk=AudioChunk(
                    data=b"\x00" * 100,
                    format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
                )
            )
            await bus.emit(ev)
            assert len(gate.buffer) == 0
            assert not gate.is_buffering
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_no_buffering_after_classifying(self) -> None:
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0)
        gate.start()
        try:
            gate.close()
            gate.release()
            assert not gate.is_buffering
            # After release, new TTS passes through (not buffered).
            ev = TTSAudio(
                chunk=AudioChunk(
                    data=b"\x00" * 100,
                    format=AudioFormat(sample_rate=16000, channels=1, sample_width=2),
                )
            )
            await bus.emit(ev)
            assert len(gate.buffer) == 0
        finally:
            gate.stop()

    @pytest.mark.asyncio
    async def test_gate_opens_on_classifying_to_voicemail_so_leave_message_plays(self) -> None:
        """CLASSIFYING -> VOICEMAIL fully opens the gate (regression).

        ``discard()`` must drop the buffered opener *and* open the gate.  If
        it left the gate closed, a subsequent leave-message voicemail drop
        would be buffered forever (no timeout in VOICEMAIL) and silently
        dropped on overflow, leaving an empty voicemail.
        """
        from easycat.audio_format import AudioChunk, AudioFormat

        bus = EventBus()
        sm = OutboundCallStateMachine(
            bus,
            classification_timeout_s=60,
            classification_gate=True,
        )
        sm.start()
        fmt = AudioFormat(sample_rate=16000, channels=1, sample_width=2)
        try:
            await bus.emit(CallAnswered(call_sid="CA1"))
            assert sm.state == OutboundCallState.CLASSIFYING
            assert sm.gate.is_buffering
            # Opener TTS is buffered while classifying.
            await bus.emit(TTSAudio(chunk=AudioChunk(data=b"\x00" * 100, format=fmt)))
            assert len(sm.gate.buffer) == 1

            # Machine detected -> VOICEMAIL.  The opener is discarded and the
            # gate fully opens.
            await bus.emit(VoicemailDetected(result="machine"))
            assert sm.state == OutboundCallState.VOICEMAIL
            assert not sm.gate.is_buffering
            assert len(sm.gate.buffer) == 0

            # A leave-message TTS drop now reaches the transport instead of
            # being buffered and silently dropped.
            await bus.emit(TTSAudio(chunk=AudioChunk(data=b"\x01" * 100, format=fmt)))
            assert len(sm.gate.buffer) == 0
        finally:
            sm.stop()
