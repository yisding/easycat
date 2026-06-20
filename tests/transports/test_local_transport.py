"""Local audio transport tests."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util

import pytest

import easycat.transports.local as local_mod
from easycat.audio_format import PCM16_MONO_24K, AudioChunk
from easycat.events import EventBus
from easycat.transports.local import LocalTransport, LocalTransportConfig

from .conftest import make_chunk

_make_chunk = make_chunk


def _sounddevice_available() -> bool:
    if importlib.util.find_spec("sounddevice") is None:
        return False
    try:
        importlib.import_module("sounddevice")
    except (ImportError, OSError):
        return False
    return True


async def _connect_or_skip(transport: LocalTransport) -> None:
    """Connect, skipping when sounddevice is installed but no device exists.

    ``sounddevice.PortAudioError`` does not subclass ``OSError``, so catch it
    explicitly (it is importable here because callers gate on
    ``_sounddevice_available()`` first).
    """
    import sounddevice

    try:
        await transport.connect()
    except (OSError, sounddevice.PortAudioError):
        pytest.skip("No audio device available (CI/container environment)")


class TestLocalTransport:
    """Tests for LocalTransport (without requiring audio hardware)."""

    @pytest.mark.asyncio
    async def test_connect_disconnect_without_sounddevice(self):
        """LocalTransport requires sounddevice to connect."""
        transport = LocalTransport()
        if not _sounddevice_available():
            with pytest.raises(ImportError):
                await transport.connect()
            assert not transport.is_connected
        else:
            await _connect_or_skip(transport)
            assert transport.is_connected
            await transport.disconnect()
            assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_connect_preflights_numpy_before_starting_streams(self, monkeypatch):
        """The local extra must cover both sounddevice and numpy before audio callbacks run."""
        transport = LocalTransport()
        requested: list[str] = []

        def fake_require_module(module_name: str, **kwargs: object) -> object:
            requested.append(module_name)
            if module_name == "sounddevice":
                return object()
            raise ImportError("LocalTransport audio I/O requires the numpy package.")

        monkeypatch.setattr(local_mod, "require_module", fake_require_module)

        with pytest.raises(ImportError, match="numpy"):
            await transport.connect()

        assert requested == ["sounddevice", "numpy"]
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self):
        transport = LocalTransport()
        await transport.disconnect()
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_send_audio_when_not_connected(self):
        """send_audio reports False when the device is not connected."""
        transport = LocalTransport()
        chunk = _make_chunk()
        delivered = await transport.send_audio(chunk)
        assert delivered is False

    @pytest.mark.asyncio
    async def test_send_audio_returns_false_when_output_queue_full(self):
        """Dropped frames surface as a False return so AudioOut isn't emitted."""
        if not _sounddevice_available():
            pytest.skip("sounddevice not available")
        # Tight queue so even a single split chunk overflows.
        config = LocalTransportConfig(max_pending_out_chunks=1)
        transport = LocalTransport(config)
        await _connect_or_skip(transport)
        try:
            # A 4800-byte chunk splits into ~8 frames; after the first one
            # the output queue is full and the remainder is dropped.
            big_chunk = _make_chunk(4800, sample_rate=16000)
            delivered = await transport.send_audio(big_chunk)
            assert delivered is False
        finally:
            await transport.disconnect()

    @pytest.mark.asyncio
    async def test_config_defaults(self):
        config = LocalTransportConfig()
        assert config.audio_format == PCM16_MONO_24K
        assert config.frame_duration_ms == 20
        assert config.input_device is None
        assert config.output_device is None

    @pytest.mark.asyncio
    async def test_receive_audio_returns_on_disconnect(self):
        """receive_audio iterator ends when transport disconnects."""
        if not _sounddevice_available():
            pytest.skip("sounddevice not available")
        transport = LocalTransport()
        await _connect_or_skip(transport)

        chunks: list[AudioChunk] = []

        async def collect():
            async for chunk in transport.receive_audio():
                chunks.append(chunk)

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        await transport.disconnect()
        await asyncio.wait_for(task, timeout=2.0)
        # Should have exited cleanly.

    @pytest.mark.asyncio
    async def test_send_audio_splits_oversized_chunks(self):
        """Chunks larger than one frame are split into frame-sized pieces."""
        if not _sounddevice_available():
            pytest.skip("sounddevice not available")
        transport = LocalTransport()
        await _connect_or_skip(transport)

        # Default: 16kHz, 20ms frames → 320 samples → 640 bytes per frame.
        # Send a 4800-byte chunk (typical TTS size) — should produce 8 pieces.
        big_chunk = _make_chunk(4800, sample_rate=16000)
        await transport.send_audio(big_chunk)

        pieces: list[bytes] = []
        while not transport._out_queue.empty():
            pieces.append(transport._out_queue.get_nowait().chunk.data)

        # 4800 / 640 = 7.5 → 8 pieces (last one is a 320-byte remainder).
        assert len(pieces) == 8
        assert all(len(p) == 640 for p in pieces[:7])
        assert len(pieces[7]) == 320  # 4800 - 7*640 = 320

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_send_audio_drops_whole_chunk_when_out_queue_lacks_capacity(self):
        transport = LocalTransport(
            LocalTransportConfig(
                audio_format=PCM16_MONO_24K,
                frame_duration_ms=20,
                max_pending_out_chunks=1,
            )
        )
        transport._connected = True
        transport._out_queue.put_nowait(None)

        chunk = _make_chunk(1920, sample_rate=24000)  # needs two 20ms output frames
        delivered = await transport.send_audio(chunk)

        assert delivered is False
        assert transport._out_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_mic_queue_full_emits_inbound_queue_full(self):
        """Mic-queue overflow surfaces a TransportDegraded like other transports."""
        from easycat.events import TransportDegraded
        from easycat.transports._base import _DEGRADED_INBOUND_QUEUE_FULL

        transport = LocalTransport(LocalTransportConfig(max_pending_in_chunks=1))
        bus = EventBus()
        received: list[TransportDegraded] = []
        bus.subscribe(TransportDegraded, lambda e: received.append(e))
        transport._event_bus = bus

        transport._enqueue_chunk(_make_chunk(), context="mic")  # fills the 1 slot
        transport._enqueue_chunk(_make_chunk(), context="mic")  # dropped
        for _ in range(5):
            await asyncio.sleep(0)

        assert [e.reason for e in received] == [_DEGRADED_INBOUND_QUEUE_FULL]
        assert received[0].provider == "local"
        assert "mic" in received[0].detail

    @pytest.mark.asyncio
    async def test_output_callback_buffers_silence_until_primed(self):
        """The jitter buffer emits silence until the pre-roll fills, then audio."""
        np = pytest.importorskip("numpy")
        from easycat.transports.local import (
            _OUTPUT_PREROLL_FRAMES,
            _QueuedOutputChunk,
        )

        transport = LocalTransport()
        frame_samples = transport._frame_samples

        def _fresh_outdata():
            return np.ones((frame_samples, 1), dtype=np.float32)

        # One frame queued is below the pre-roll threshold: still silence.
        loud = AudioChunk(
            data=(1000).to_bytes(2, "little", signed=True) * frame_samples,
            format=transport._audio_format,
        )
        transport._out_queue.put_nowait(_QueuedOutputChunk(chunk=loud))
        assert transport._out_queue.qsize() < _OUTPUT_PREROLL_FRAMES

        outdata = _fresh_outdata()
        transport._output_callback(np, outdata, frame_samples, None, None)
        assert not transport._primed
        assert (outdata == 0).all()  # silence before prime
        assert transport._out_queue.qsize() == 1  # frame retained, not drained

        # Fill up to the pre-roll target; the next callback primes and drains.
        while transport._out_queue.qsize() < _OUTPUT_PREROLL_FRAMES:
            transport._out_queue.put_nowait(_QueuedOutputChunk(chunk=loud))

        outdata = _fresh_outdata()
        transport._output_callback(np, outdata, frame_samples, None, None)
        assert transport._primed
        assert not (outdata == 0).all()  # real audio after prime
        assert transport._out_queue.qsize() == _OUTPUT_PREROLL_FRAMES - 1

    @pytest.mark.asyncio
    async def test_clear_audio_re_primes_jitter_buffer(self):
        """clear_audio() drops queued audio and re-arms the pre-roll."""
        transport = LocalTransport()
        transport._primed = True
        chunk = _make_chunk(640, sample_rate=24000)
        from easycat.transports.local import _QueuedOutputChunk

        transport._out_queue.put_nowait(_QueuedOutputChunk(chunk=chunk))

        await transport.clear_audio()

        assert transport._out_queue.empty()
        assert transport._primed is False

    @pytest.mark.asyncio
    async def test_connect_re_primes_jitter_buffer(self):
        """connect() re-arms the pre-roll so each session starts unprimed."""
        if not _sounddevice_available():
            pytest.skip("sounddevice not available")
        transport = LocalTransport()
        transport._primed = True
        await _connect_or_skip(transport)
        try:
            assert transport._primed is False
        finally:
            await transport.disconnect()

    @pytest.mark.asyncio
    async def test_pending_playout_ms_reflects_queue_depth(self):
        """pending_playout_ms scales with queued frames and per-frame duration."""
        transport = LocalTransport()
        assert transport.pending_playout_ms() == 0.0

        from easycat.transports.local import _QueuedOutputChunk

        chunk = _make_chunk(640, sample_rate=24000)
        transport._out_queue.put_nowait(_QueuedOutputChunk(chunk=chunk))
        transport._out_queue.put_nowait(_QueuedOutputChunk(chunk=chunk))

        assert transport.pending_playout_ms() == 2 * transport._config.frame_duration_ms

    @pytest.mark.asyncio
    async def test_schedule_audio_delivery_tracks_emit_task(self):
        """The audio-delivery emit task is retained so it isn't GC'd mid-flight."""
        from easycat.events import TransportAudioDelivered
        from easycat.transports.local import _QueuedOutputChunk

        transport = LocalTransport()
        bus = EventBus()
        received: list[TransportAudioDelivered] = []
        bus.subscribe(TransportAudioDelivered, lambda e: received.append(e))
        transport._event_bus = bus
        transport._loop = asyncio.get_running_loop()

        queued = _QueuedOutputChunk(chunk=_make_chunk(), turn_id="t1")
        transport._schedule_audio_delivery(queued)
        # call_soon_threadsafe callback runs first, then the emit task.
        for _ in range(5):
            await asyncio.sleep(0)

        assert len(received) == 1
        assert received[0].turn_id == "t1"
        assert transport._emit_tasks == set()  # drained after completion
