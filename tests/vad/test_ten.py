"""VAD tests."""

from __future__ import annotations

import asyncio
import struct
import types
from unittest.mock import MagicMock

import pytest

from easycat.audio_format import AudioChunk, AudioFormat
from easycat.events import VADStartSpeaking
from easycat.vad import (
    TenVAD,
)
from easycat.vad import ten as vad_ten_module
from tests.vad._helpers import _make_chunk


def test_ten_vad_fails_without_sdk(monkeypatch: pytest.MonkeyPatch):
    """TenVAD should raise RuntimeError if ten_vad package is missing."""

    def _require_module(module_name: str, **_: object) -> object:
        if module_name == "ten_vad":
            raise ImportError("TEN VAD requires ten_vad")
        if module_name == "numpy":
            return types.SimpleNamespace()
        raise AssertionError(f"unexpected module load: {module_name}")

    monkeypatch.setattr(vad_ten_module, "require_module", _require_module)
    with pytest.raises(RuntimeError, match="TEN VAD|ten_vad"):
        TenVAD()


@pytest.mark.asyncio
async def test_ten_vad_process_mocked(monkeypatch: pytest.MonkeyPatch):
    """TenVAD with mocked SDK should process audio."""
    mock_ten_vad = MagicMock()
    mock_instance = MagicMock()
    mock_instance.process.return_value = (0.9, 1)
    mock_ten_vad.TenVad.return_value = mock_instance

    import sys

    monkeypatch.setitem(sys.modules, "ten_vad", mock_ten_vad)

    import types

    class _FakeArray:
        def copy(self):
            return self

    fake_numpy = types.SimpleNamespace(
        int16="int16",
        frombuffer=lambda data, dtype: _FakeArray(),
    )
    monkeypatch.setitem(sys.modules, "numpy", fake_numpy)

    vad = TenVAD()
    vad._min_speech_duration_ms = 0
    vad._threshold = 0.5

    chunk = _make_chunk(1000)
    events = []
    async for event in vad.process(chunk):
        events.append(event)

    assert any(isinstance(e, VADStartSpeaking) for e in events)
    assert mock_instance.process.call_count == 2


@pytest.mark.asyncio
async def test_ten_preserves_stereo_frames_split_across_chunks(
    monkeypatch: pytest.MonkeyPatch,
):
    """A split interleaved frame must be downmixed only after reassembly."""
    mock_ten_vad = MagicMock()
    mock_ten_vad.TenVad.return_value = MagicMock()
    monkeypatch.setattr(
        vad_ten_module,
        "require_module",
        lambda name, **_kwargs: mock_ten_vad if name == "ten_vad" else object(),
    )
    vad = TenVAD()
    fmt = AudioFormat(sample_rate=16_000, channels=2, sample_width=2)
    data = struct.pack("<6h", 100, 300, 1_000, 2_000, 3_000, 4_000)

    async for _ in vad.process(AudioChunk(data=data[:6], format=fmt)):
        pass
    async for _ in vad.process(AudioChunk(data=data[6:], format=fmt)):
        pass

    assert vad._buffer == struct.pack("<3h", 200, 1_500, 3_500)


@pytest.mark.asyncio
async def test_ten_vad_yields_to_event_loop_while_draining_backlog(
    monkeypatch: pytest.MonkeyPatch,
):
    """Large silent chunks must not defer cancellation behind the whole backlog."""
    mock_ten_vad = MagicMock()
    mock_instance = MagicMock()
    mock_instance.process.return_value = (0.0, 0)
    mock_ten_vad.TenVad.return_value = mock_instance
    monkeypatch.setattr(
        vad_ten_module,
        "require_module",
        lambda name, **_kwargs: {
            "ten_vad": mock_ten_vad,
            "numpy": types.SimpleNamespace(
                int16="int16",
                frombuffer=lambda _data, dtype: types.SimpleNamespace(copy=lambda: object()),
            ),
        }[name],
    )

    vad = TenVAD()
    observer_ran = False

    async def observe_loop() -> None:
        nonlocal observer_ran
        observer_ran = True

    observer = asyncio.create_task(observe_loop())
    try:
        events = [event async for event in vad.process(_make_chunk(n_samples=vad._hop_size * 3))]
        assert events == []
        assert observer_ran
    finally:
        await observer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_rate", "second_rate"),
    [(48_000, 16_000), (24_000, 16_000), (8_000, 16_000), (48_000, 24_000), (16_000, 48_000)],
)
async def test_ten_vad_keeps_buffered_audio_across_source_rate_switch(
    monkeypatch: pytest.MonkeyPatch, first_rate: int, second_rate: int
):
    """A mid-stream source-rate switch must not drop already-resampled 16 kHz audio.

    The buffer always holds 16 kHz model-rate PCM, but it used to be cleared
    whenever the *source* rate changed, and the resampler tail was reset
    instead of flushed. A 48 kHz segment followed by native 16 kHz input
    therefore lost up to one hop plus the interpolation tail, and the VAD
    clock fell behind the audio actually fed.
    """
    frame_samples: list[int] = []
    mock_ten_vad = MagicMock()
    mock_instance = MagicMock()

    def _process(frame: int) -> tuple[float, int]:
        frame_samples.append(frame)
        return 0.0, 0

    mock_instance.process.side_effect = _process
    mock_ten_vad.TenVad.return_value = mock_instance
    fake_numpy = types.SimpleNamespace(
        int16="int16",
        frombuffer=lambda data, dtype: types.SimpleNamespace(copy=lambda: len(data) // 2),
    )
    monkeypatch.setattr(
        vad_ten_module,
        "require_module",
        lambda name, **_kwargs: {"ten_vad": mock_ten_vad, "numpy": fake_numpy}[name],
    )

    vad = TenVAD()
    fed_samples_16k = 0
    for rate, chunks in ((first_rate, 2), (second_rate, 10)):
        fmt = AudioFormat(sample_rate=rate, channels=1, sample_width=2)
        samples_per_chunk = rate * 15 // 1000  # 15 ms
        for _ in range(chunks):
            chunk = AudioChunk(data=bytes(samples_per_chunk * 2), format=fmt)
            async for _ in vad.process(chunk):
                pass
            fed_samples_16k += 240

    assert all(n == vad._hop_size for n in frame_samples)
    accounted = (
        sum(frame_samples) + len(vad._buffer) // 2 + vad._audio_resampler.pending_output_bytes // 2
    )
    assert abs(accounted - fed_samples_16k) <= 1
    assert vad._audio_time_s == pytest.approx(len(frame_samples) * vad._hop_size / 16_000)
