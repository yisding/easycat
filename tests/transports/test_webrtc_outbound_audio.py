"""WebRTC outbound audio source tests."""

from __future__ import annotations

import asyncio

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.transports.webrtc import _OutboundAudioSource

from ._webrtc_fakes import _HAS_WEBRTC_DEPS


class TestOutboundAudioSource:
    def test_enqueue_and_drain(self):
        source = _OutboundAudioSource()
        data = bytes(960 * 2)  # 20ms at 48kHz mono s16
        source.enqueue(data, original_chunk=AudioChunk(data=data, format=PCM16_MONO_16K))
        assert not source._queue.empty()

    def test_enqueue_overflow(self):
        source = _OutboundAudioSource()
        source._queue = asyncio.Queue(maxsize=2)
        chunk = AudioChunk(data=bytes(100), format=PCM16_MONO_16K)
        # Fill queue.
        assert source.enqueue(bytes(100), original_chunk=chunk) is True
        assert source.enqueue(bytes(100), original_chunk=chunk) is True
        # Overflow — should not raise, and should report dropped frame.
        assert source.enqueue(bytes(100), original_chunk=chunk) is False

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_recv_produces_silence_when_empty(self):
        source = _OutboundAudioSource()
        frame = await source._recv()
        assert frame.sample_rate == 48000
        assert frame.samples == 960
        # Frame data should be all zeros (silence).
        data = bytes(frame.planes[0])
        assert data == bytes(960 * 2)

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_recv_returns_enqueued_data(self):
        source = _OutboundAudioSource()
        # Enqueue one frame of non-silent data.
        test_data = bytes(range(256)) * (960 * 2 // 256 + 1)
        test_data = test_data[: 960 * 2]
        source.enqueue(test_data, original_chunk=AudioChunk(data=test_data, format=PCM16_MONO_16K))

        frame = await source._recv()
        actual = bytes(frame.planes[0])
        assert actual == test_data

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_recv_preserves_audio_order_with_remainder(self):
        """Verify that audio chunks larger than one frame don't reorder."""
        source = _OutboundAudioSource()
        frame_bytes = 960 * 2  # one 20ms frame at 48kHz mono s16

        # Create chunk A (1.5 frames) and chunk B (1 frame).
        chunk_a = bytes([0xAA]) * (frame_bytes + frame_bytes // 2)
        chunk_b = bytes([0xBB]) * frame_bytes
        source.enqueue(chunk_a, original_chunk=AudioChunk(data=chunk_a, format=PCM16_MONO_16K))
        source.enqueue(chunk_b, original_chunk=AudioChunk(data=chunk_b, format=PCM16_MONO_16K))

        # Frame 1: first frame of A.
        frame1 = await source._recv()
        data1 = bytes(frame1.planes[0])
        assert data1 == bytes([0xAA]) * frame_bytes

        # Frame 2: remainder of A (half frame) + start of B (half frame).
        frame2 = await source._recv()
        data2 = bytes(frame2.planes[0])
        expected = bytes([0xAA]) * (frame_bytes // 2) + bytes([0xBB]) * (frame_bytes // 2)
        assert data2 == expected

        # Frame 3: remainder of B (half frame) + silence padding.
        frame3 = await source._recv()
        data3 = bytes(frame3.planes[0])
        expected3 = bytes([0xBB]) * (frame_bytes // 2) + bytes(frame_bytes // 2)
        assert data3 == expected3

    def test_clear_discards_queued_data(self):
        source = _OutboundAudioSource()
        chunk = AudioChunk(data=bytes(200), format=PCM16_MONO_16K)
        source.enqueue(bytes(100), original_chunk=chunk)
        source.enqueue(bytes(200), original_chunk=chunk)
        source._pending.append(source._queue.get_nowait())

        source.clear()

        assert source._queue.empty()
        assert not source._pending

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_clear_then_recv_produces_silence(self):
        source = _OutboundAudioSource()
        test_data = bytes([0xFF]) * 960 * 2
        source.enqueue(
            test_data,
            original_chunk=AudioChunk(data=test_data, format=PCM16_MONO_16K),
        )
        source.clear()

        frame = await source._recv()
        data = bytes(frame.planes[0])
        assert data == bytes(960 * 2)  # silence
