"""Tests for TTS base class and test harness utilities."""

from __future__ import annotations

import struct
import tempfile
import wave
from pathlib import Path

import pytest

from easycat.audio_format import PCM16_MONO_16K, PCM16_MONO_24K, AudioChunk, AudioFormat
from easycat.events import TTSEvent, TTSEventType
from easycat.tts.base import TTSBase
from easycat.tts.input import TTSInput
from tests.tts._harness import (
    collect_tts_output,
    concatenate_audio,
    extract_audio_chunks,
    verify_pcm16_audio,
    write_wav,
)

# ── Helper: Fake TTS provider for testing ─────────────────────────


class FakeTTS(TTSBase):
    """A fake TTS provider that yields predetermined audio chunks."""

    def __init__(
        self,
        chunks: list[bytes] | None = None,
        output_format: AudioFormat = PCM16_MONO_24K,
    ):
        super().__init__(output_format=output_format)
        self._chunks = chunks or []

    async def synthesize(self, payload: TTSInput):
        self._start_synthesis()
        try:
            for chunk_data in self._chunks:
                if self._cancelled:
                    break
                yield self._make_audio_event(chunk_data)
        finally:
            self._end_synthesis()


def _make_pcm16_data(n_samples: int = 100, value: int = 1000) -> bytes:
    """Generate PCM16 audio data with a constant sample value."""
    return struct.pack(f"<{n_samples}h", *([value] * n_samples))


# ── TTSBase tests ─────────────────────────────────────────────────


class TestTTSBase:
    def test_initial_state(self):
        base = TTSBase()
        assert not base.is_active
        assert not base.is_cancelled
        assert base._output_format == PCM16_MONO_24K

    def test_custom_output_format(self):
        base = TTSBase(output_format=PCM16_MONO_16K)
        assert base._output_format == PCM16_MONO_16K

    def test_start_and_end_synthesis(self):
        base = TTSBase()
        base._start_synthesis()
        assert base.is_active
        assert not base.is_cancelled

        base._end_synthesis()
        assert not base.is_active

    def test_cancel_resets_on_start(self):
        base = TTSBase()
        base._cancelled = True
        base._start_synthesis()
        assert not base.is_cancelled

    def test_make_audio_event(self):
        base = TTSBase()
        data = _make_pcm16_data(50)
        event = base._make_audio_event(data)

        assert event.type == TTSEventType.AUDIO
        assert event.audio is not None
        assert event.audio.data == data
        assert event.audio.format == PCM16_MONO_24K

    def test_make_audio_event_with_format_conversion(self):
        base = TTSBase(output_format=PCM16_MONO_24K)
        data = _make_pcm16_data(160)  # 160 samples at 16kHz = 10ms
        source_fmt = PCM16_MONO_16K

        event = base._make_audio_event(data, source_fmt)
        assert event.type == TTSEventType.AUDIO
        assert event.audio is not None
        # Resampled from 16kHz to 24kHz, so more samples
        assert event.audio.format == PCM16_MONO_24K
        assert len(event.audio.data) > len(data)

    def test_make_audio_event_aligns_odd_frame_without_resample(self):
        """An odd-length frame at the output sample rate (no resample) must be
        emitted sample-aligned, with the trailing byte carried to the next."""
        base = TTSBase(output_format=PCM16_MONO_24K)
        base._start_synthesis()
        # 5 bytes, source == output format so no resample path runs.
        event = base._make_audio_event(b"\x01\x02\x03\x04\x05", PCM16_MONO_24K)
        assert event.audio is not None
        assert len(event.audio.data) % 2 == 0
        assert event.audio.data == b"\x01\x02\x03\x04"
        assert base._sample_carry == b"\x05"

    def test_make_audio_event_carries_split_sample_across_frames(self):
        """The byte held back from one frame is prepended to the next so no
        sample is lost or corrupted at a streaming-frame boundary."""
        base = TTSBase(output_format=PCM16_MONO_24K)
        base._start_synthesis()
        first = base._make_audio_event(b"\xaa\xbb\xcc", PCM16_MONO_24K)
        assert first.audio is not None
        assert first.audio.data == b"\xaa\xbb"
        assert base._sample_carry == b"\xcc"
        second = base._make_audio_event(b"\xdd", PCM16_MONO_24K)
        assert second.audio is not None
        assert second.audio.data == b"\xcc\xdd"
        assert base._sample_carry == b""

    def test_make_audio_event_aligns_without_explicit_format(self):
        """Even when no source format is passed, an odd frame is aligned to the
        output sample width."""
        base = TTSBase(output_format=PCM16_MONO_24K)
        base._start_synthesis()
        event = base._make_audio_event(b"\x01\x02\x03")
        assert event.audio is not None
        assert len(event.audio.data) % 2 == 0
        assert base._sample_carry == b"\x03"

    def test_start_synthesis_resets_sample_carry(self):
        base = TTSBase(output_format=PCM16_MONO_24K)
        base._sample_carry = b"\x99"
        base._start_synthesis()
        assert base._sample_carry == b""

    def test_make_markers_event(self):
        base = TTSBase()
        markers = [{"word": "hello", "start": 0.0, "end": 0.5}]
        event = base._make_markers_event(markers)

        assert event.type == TTSEventType.MARKERS
        assert event.markers == markers

    def test_make_audio_event_odd_chunk_with_resample_does_not_crash(self):
        """An odd-length frame routed through the real _make_audio_event ->
        _normalize_audio resample path must not raise struct.error: the
        sub-sample byte is held back by _sample_carry before resample."""
        base = TTSBase(output_format=PCM16_MONO_24K)
        base._start_synthesis()
        # 5 bytes at 16kHz -> resampled to 24kHz; the trailing byte is held.
        event = base._make_audio_event(b"\x01\x02\x03\x04\x05", PCM16_MONO_16K)
        assert event.audio is not None
        assert isinstance(event.audio.data, bytes)
        assert base._sample_carry == b"\x05"

    def test_make_audio_event_carries_split_sample_across_resample_chunks(self):
        """Through the real call path, the byte held back from one resampled
        frame is prepended to the next so no sample is lost or corrupted."""
        base = TTSBase(output_format=PCM16_MONO_24K)
        base._start_synthesis()
        # First chunk: 3 bytes -> one full sample resampled, one byte carried.
        base._make_audio_event(b"\xaa\xbb\xcc", PCM16_MONO_16K)
        assert base._sample_carry == b"\xcc"
        # Second chunk: carry (1) + 1 byte = 2 bytes -> a full sample, no carry.
        base._make_audio_event(b"\xdd", PCM16_MONO_16K)
        assert base._sample_carry == b""

    def test_end_synthesis_drops_subsample_carry(self):
        """A leftover sub-sample byte that no frame completed is intentionally
        dropped (not emitted) when synthesis ends, and cleared so it cannot
        leak into the next turn."""
        base = TTSBase(output_format=PCM16_MONO_24K)
        base._start_synthesis()
        base._make_audio_event(b"\x01\x02\x03", PCM16_MONO_24K)
        assert base._sample_carry == b"\x03"
        base._end_synthesis()
        assert base._sample_carry == b""

    def test_normalize_stereo_to_mono(self):
        base = TTSBase(output_format=PCM16_MONO_24K)
        # Stereo: 2 channels, each sample is 2 int16 values
        stereo_format = AudioFormat(sample_rate=24000, channels=2, sample_width=2)
        # 4 stereo frames = 8 int16 values = 16 bytes
        stereo_data = struct.pack("<8h", 100, 200, 300, 400, 500, 600, 700, 800)

        mono_data = base._normalize_audio(stereo_data, stereo_format)
        # Should have 4 mono samples = 8 bytes
        assert len(mono_data) == 8

    async def test_stop(self):
        base = TTSBase()
        base._active = True
        await base.stop()
        assert not base.is_active

    async def test_cancel(self):
        base = TTSBase()
        base._active = True
        await base.cancel()
        assert base.is_cancelled
        assert not base.is_active

    def test_synthesize_not_implemented(self):
        base = TTSBase()
        with pytest.raises(NotImplementedError):
            base.synthesize(TTSInput("hello"))


# ── FakeTTS synthesize tests ──────────────────────────────────────


class TestFakeTTS:
    async def test_synthesize_yields_events(self):
        chunks = [_make_pcm16_data(100), _make_pcm16_data(100)]
        tts = FakeTTS(chunks=chunks)

        events = []
        async for event in tts.synthesize(TTSInput("hello")):
            events.append(event)

        assert len(events) == 2
        for e in events:
            assert e.type == TTSEventType.AUDIO

    async def test_synthesize_tracks_active_state(self):
        tts = FakeTTS(chunks=[_make_pcm16_data(10)])
        assert not tts.is_active

        async for _ in tts.synthesize(TTSInput("hi")):
            assert tts.is_active

        assert not tts.is_active

    async def test_synthesize_respects_cancel(self):
        chunks = [_make_pcm16_data(100)] * 10
        tts = FakeTTS(chunks=chunks)

        events = []
        async for event in tts.synthesize(TTSInput("long text")):
            events.append(event)
            if len(events) == 2:
                await tts.cancel()

        assert len(events) == 2
        assert tts.is_cancelled

    async def test_synthesize_empty(self):
        tts = FakeTTS(chunks=[])
        events = await collect_tts_output(tts, "hello")
        assert events == []


# ── Test harness utility tests ────────────────────────────────────


class TestHarnessUtils:
    async def test_collect_tts_output(self):
        chunks = [_make_pcm16_data(50), _make_pcm16_data(50)]
        tts = FakeTTS(chunks=chunks)
        events = await collect_tts_output(tts, "hello")
        assert len(events) == 2

    def test_extract_audio_chunks(self):
        audio_event = TTSEvent(
            type=TTSEventType.AUDIO,
            audio=AudioChunk(data=_make_pcm16_data(10), format=PCM16_MONO_24K),
        )
        marker_event = TTSEvent(
            type=TTSEventType.MARKERS,
            markers=[{"word": "hi"}],
        )
        chunks = extract_audio_chunks([audio_event, marker_event])
        assert len(chunks) == 1

    def test_concatenate_audio(self):
        c1 = AudioChunk(data=b"\x01\x00", format=PCM16_MONO_24K)
        c2 = AudioChunk(data=b"\x02\x00", format=PCM16_MONO_24K)
        assert concatenate_audio([c1, c2]) == b"\x01\x00\x02\x00"

    def test_write_wav(self):
        data = _make_pcm16_data(240)  # 240 samples at 24kHz = 10ms
        chunks = [AudioChunk(data=data, format=PCM16_MONO_24K)]

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name

        result = write_wav(chunks, path)
        assert Path(result).exists()

        # Verify the WAV file is valid
        with wave.open(str(result), "rb") as wf:
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getframerate() == 24000
            assert wf.getnframes() == 240

        Path(result).unlink()

    def test_write_wav_empty_raises(self):
        with pytest.raises(ValueError, match="No audio chunks"):
            write_wav([], "/tmp/test.wav")

    def test_verify_pcm16_audio_valid(self):
        chunks = [AudioChunk(data=_make_pcm16_data(10), format=PCM16_MONO_24K)]
        assert verify_pcm16_audio(chunks) is True

    def test_verify_pcm16_audio_wrong_width(self):
        fmt = AudioFormat(sample_rate=24000, channels=1, sample_width=4)
        chunks = [AudioChunk(data=b"\x00" * 40, format=fmt)]
        assert verify_pcm16_audio(chunks) is False

    def test_verify_pcm16_audio_stereo(self):
        fmt = AudioFormat(sample_rate=24000, channels=2, sample_width=2)
        chunks = [AudioChunk(data=b"\x00" * 40, format=fmt)]
        assert verify_pcm16_audio(chunks) is False

    def test_verify_pcm16_audio_odd_bytes(self):
        chunks = [AudioChunk(data=b"\x00\x01\x02", format=PCM16_MONO_24K)]
        assert verify_pcm16_audio(chunks) is False
