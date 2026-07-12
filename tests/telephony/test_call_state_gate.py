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
    async def test_reclosing_gate_replaces_existing_timeout(self) -> None:
        bus = EventBus()
        gate = ClassificationGate(bus, enabled=True, timeout_s=5.0)
        gate.start()
        try:
            gate.close()
            first_timeout = gate._tasks.tasks()[0]

            gate.close()
            second_timeout = gate._tasks.tasks()[0]
            await asyncio.sleep(0)

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
