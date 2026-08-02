"""Tests for TTS base class and test harness utilities."""

from __future__ import annotations

import struct

import pytest

from easycat.audio_format import PCM16_MONO_8K, PCM16_MONO_16K, PCM16_MONO_24K, AudioFormat
from easycat.events import TTSEventType
from easycat.tts.base import TTSBase
from easycat.tts.input import TTSInput
from tests.tts._harness import collect_tts_output

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
                event = self._make_audio_event(chunk_data)
                if event is not None:
                    yield event
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

    @pytest.mark.parametrize(
        "output_format",
        [
            AudioFormat(sample_rate=24_000, channels=1, sample_width=1),
            AudioFormat(sample_rate=24_000, channels=1, sample_width=2, encoding="mulaw"),
        ],
    )
    def test_rejects_non_pcm16_output_format(self, output_format):
        with pytest.raises(ValueError, match="output_format must be PCM16"):
            TTSBase(output_format=output_format)

    def test_default_input_policy_is_plain_text(self):
        base = TTSBase()
        assert base.input_policy.accepted_formats == ("plain",)

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

        assert event is not None
        assert event.type == TTSEventType.AUDIO
        assert event.audio is not None
        assert event.audio.data == data
        assert event.audio.format == PCM16_MONO_24K

    def test_make_audio_event_with_format_conversion(self):
        base = TTSBase(output_format=PCM16_MONO_24K)
        data = _make_pcm16_data(160)  # 160 samples at 16kHz = 10ms
        source_fmt = PCM16_MONO_16K

        base._start_synthesis()
        event = base._make_audio_event(data, source_fmt)
        tail = base._finish_audio_event()
        events = [item for item in (event, tail) if item is not None]

        assert all(item.type == TTSEventType.AUDIO for item in events)
        assert all(item.audio is not None for item in events)
        # Resampled from 16kHz to 24kHz, so more samples
        assert all(item.audio.format == PCM16_MONO_24K for item in events if item.audio)
        output = b"".join(item.audio.data for item in events if item.audio)
        assert len(output) > len(data)

    def test_make_audio_event_upmixes_mono_to_stereo_output(self):
        stereo_output = AudioFormat(sample_rate=24_000, channels=2, sample_width=2)
        base = TTSBase(output_format=stereo_output)

        event = base._make_audio_event(struct.pack("<2h", 100, -200), PCM16_MONO_24K)

        assert event is not None and event.audio is not None
        assert event.audio.format == stereo_output
        assert event.audio.num_samples == 2
        assert struct.unpack("<4h", event.audio.data) == (100, 100, -200, -200)

    def test_make_audio_event_preserves_matching_stereo_channels(self):
        stereo_format = AudioFormat(sample_rate=24_000, channels=2, sample_width=2)
        base = TTSBase(output_format=stereo_format)
        data = struct.pack("<4h", 1000, -1000, 2000, -2000)

        event = base._make_audio_event(data, stereo_format)

        assert event is not None and event.audio is not None
        assert event.audio.data == data
        assert struct.unpack("<4h", event.audio.data) == (1000, -1000, 2000, -2000)

    def test_make_audio_event_discards_carry_when_source_format_changes(self):
        stereo_format = AudioFormat(sample_rate=24_000, channels=2, sample_width=2)
        base = TTSBase(output_format=PCM16_MONO_24K)
        base._start_synthesis()

        # Half a stereo frame cannot be completed by a later mono chunk.
        assert base._make_audio_event(struct.pack("<h", 100), stereo_format) is None
        assert base._sample_carry == struct.pack("<h", 100)
        assert base._sample_carry_format == stereo_format

        event = base._make_audio_event(struct.pack("<h", 200), PCM16_MONO_24K)

        assert event is not None and event.audio is not None
        assert struct.unpack("<h", event.audio.data) == (200,)
        assert base._sample_carry == b""
        assert base._sample_carry_format is None

    def test_resampler_tail_is_upmixed_to_stereo_output(self):
        stereo_output = AudioFormat(sample_rate=8_000, channels=2, sample_width=2)
        base = TTSBase(output_format=stereo_output)
        base._start_synthesis()

        first = base._make_audio_event(_make_pcm16_data(2400), PCM16_MONO_24K)
        tail = base._finish_audio_event()
        output = b"".join(
            event.audio.data
            for event in (first, tail)
            if event is not None and event.audio is not None
        )

        assert len(output) == 800 * stereo_output.frame_size
        samples = struct.unpack(f"<{len(output) // 2}h", output)
        assert samples[::2] == samples[1::2]

    def test_make_audio_event_rejects_non_pcm16_source_format(self):
        base = TTSBase()
        source_format = AudioFormat(
            sample_rate=24_000,
            channels=1,
            sample_width=1,
            encoding="mulaw",
        )

        with pytest.raises(ValueError, match="source_format must be PCM16"):
            base._make_audio_event(b"\x00", source_format)

    def test_finish_audio_event_emits_delayed_resampler_tail(self):
        base = TTSBase(output_format=PCM16_MONO_8K)
        base._start_synthesis()
        data = _make_pcm16_data(2400)

        first = base._make_audio_event(data, PCM16_MONO_24K)
        tail = base._finish_audio_event()

        assert first is not None
        assert first.audio is not None
        assert tail is not None and tail.audio is not None
        assert len(tail.audio.data) > 0
        assert len(first.audio.data) + len(tail.audio.data) == 800 * 2

    def test_end_synthesis_discards_delayed_resampler_tail(self):
        base = TTSBase(output_format=PCM16_MONO_8K)
        base._start_synthesis()
        base._make_audio_event(_make_pcm16_data(2400), PCM16_MONO_24K)

        base._end_synthesis()

        assert base._finish_audio_event() is None

    def test_make_audio_event_aligns_odd_frame_without_resample(self):
        """An odd-length frame at the output sample rate (no resample) must be
        emitted sample-aligned, with the trailing byte carried to the next."""
        base = TTSBase(output_format=PCM16_MONO_24K)
        base._start_synthesis()
        # 5 bytes, source == output format so no resample path runs.
        event = base._make_audio_event(b"\x01\x02\x03\x04\x05", PCM16_MONO_24K)
        assert event is not None
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
        assert first is not None
        assert first.audio is not None
        assert first.audio.data == b"\xaa\xbb"
        assert base._sample_carry == b"\xcc"
        second = base._make_audio_event(b"\xdd", PCM16_MONO_24K)
        assert second is not None
        assert second.audio is not None
        assert second.audio.data == b"\xcc\xdd"
        assert base._sample_carry == b""

    def test_make_audio_event_aligns_without_explicit_format(self):
        """Even when no source format is passed, an odd frame is aligned to the
        output sample width."""
        base = TTSBase(output_format=PCM16_MONO_24K)
        base._start_synthesis()
        event = base._make_audio_event(b"\x01\x02\x03")
        assert event is not None
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
        if event is not None:
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

    def test_isolated_audio_states_do_not_share_carry_or_resampler(self):
        """Interleaved streams must match their separately rendered PCM."""
        base = TTSBase(output_format=PCM16_MONO_16K)
        source_format = PCM16_MONO_24K
        first_stream = _make_pcm16_data(2400, value=100)
        second_stream = _make_pcm16_data(2400, value=-200)

        def render(parts: list[bytes]) -> bytes:
            state = base._new_audio_conversion_state()
            events = [base._make_audio_event(part, source_format, state=state) for part in parts]
            events.append(base._finish_audio_event(state=state))
            return b"".join(
                event.audio.data
                for event in events
                if event is not None and event.audio is not None
            )

        split_at = 2401  # Deliberately split inside one 16-bit source sample.
        expected_first = render([first_stream[:split_at], first_stream[split_at:]])
        expected_second = render([second_stream])

        first_state = base._new_audio_conversion_state()
        second_state = base._new_audio_conversion_state()
        first_events = [
            base._make_audio_event(first_stream[:split_at], source_format, state=first_state),
        ]
        second_events = [
            base._make_audio_event(second_stream, source_format, state=second_state),
        ]
        first_events.extend(
            [
                base._make_audio_event(first_stream[split_at:], source_format, state=first_state),
                base._finish_audio_event(state=first_state),
            ]
        )
        second_events.append(base._finish_audio_event(state=second_state))
        actual_first = b"".join(
            event.audio.data
            for event in first_events
            if event is not None and event.audio is not None
        )
        actual_second = b"".join(
            event.audio.data
            for event in second_events
            if event is not None and event.audio is not None
        )

        assert actual_first == expected_first
        assert actual_second == expected_second

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
