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
from easycat.tts.test_harness import (
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

    def test_make_markers_event(self):
        base = TTSBase()
        markers = [{"word": "hello", "start": 0.0, "end": 0.5}]
        event = base._make_markers_event(markers)

        assert event.type == TTSEventType.MARKERS
        assert event.markers == markers

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
