"""Local audio transport tests."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util

import pytest

import easycat.transports.local as local_mod
from easycat.audio_format import PCM16_MONO_24K, AudioChunk, AudioFormat
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
    async def test_connect_rolls_back_input_when_output_start_fails(self, monkeypatch):
        """A speaker startup failure must not leak the already-started microphone."""
        transport = LocalTransport()
        events: list[str] = []

        class FakeStream:
            def __init__(self, name: str, *, fail_start: bool = False) -> None:
                self.name = name
                self.fail_start = fail_start

            def start(self) -> None:
                events.append(f"{self.name}.start")
                if self.fail_start:
                    raise RuntimeError("output start failed")

            def stop(self) -> None:
                events.append(f"{self.name}.stop")

            def close(self) -> None:
                events.append(f"{self.name}.close")

        class FakeSoundDevice:
            @staticmethod
            def InputStream(**_kwargs: object) -> FakeStream:
                return FakeStream("input")

            @staticmethod
            def OutputStream(**_kwargs: object) -> FakeStream:
                return FakeStream("output", fail_start=True)

        def fake_require_module(module_name: str, **_kwargs: object) -> object:
            if module_name == "sounddevice":
                return FakeSoundDevice()
            return object()

        monkeypatch.setattr(local_mod, "require_module", fake_require_module)

        with pytest.raises(RuntimeError, match="output start failed"):
            await transport.connect()

        assert events == [
            "input.start",
            "output.start",
            "input.stop",
            "input.close",
            "output.stop",
            "output.close",
        ]
        assert transport._input_stream is None
        assert transport._output_stream is None
        assert transport._loop is None
        assert not transport.is_connected

        # The exit-stack callback used by the teaching example may invoke
        # disconnect again after connect() has already rolled itself back.
        cleanup_events = events.copy()
        await transport.disconnect()
        assert events == cleanup_events

    @pytest.mark.asyncio
    async def test_input_callback_clips_overdriven_mic_samples(self, monkeypatch):
        """Out-of-range float32 mic samples saturate to int16 limits, not wrap.

        sounddevice/PortAudio does not hard-clamp float32 capture, so an
        overdriven input can exceed [-1, 1].  numpy's int16 cast wraps
        (sign-flips) rather than saturating, which would inject a harsh
        opposite-polarity click into AEC/VAD/STT.  The conversion must clip.
        """
        np = pytest.importorskip("numpy")

        captured: dict[str, object] = {}

        class FakeStream:
            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def close(self) -> None:
                pass

        class FakeSoundDevice:
            @staticmethod
            def InputStream(*, callback: object, **_kwargs: object) -> FakeStream:
                captured["input_callback"] = callback
                return FakeStream()

            @staticmethod
            def OutputStream(**_kwargs: object) -> FakeStream:
                return FakeStream()

        def fake_require_module(module_name: str, **_kwargs: object) -> object:
            if module_name == "sounddevice":
                return FakeSoundDevice()
            return np

        monkeypatch.setattr(local_mod, "require_module", fake_require_module)

        transport = LocalTransport()
        await transport.connect()
        try:
            input_callback = captured["input_callback"]
            # Overdriven buffer: values above +1.0 and below -1.0.
            frame_samples = transport._frame_samples
            overdriven = np.full((frame_samples, 1), 1.5, dtype=np.float32)
            overdriven[0, 0] = -1.5
            input_callback(overdriven, frame_samples, None, None)  # type: ignore[operator]

            # The callback schedules ``_enqueue_chunk`` onto the loop.
            for _ in range(5):
                await asyncio.sleep(0)

            chunk = transport._in_queue.get_nowait()
            assert chunk is not None
            samples = np.frombuffer(chunk.data, dtype=np.int16)
            # Saturated, not wrapped: +1.5 -> +32767, -1.5 -> -32768.
            assert samples.max() == 32767
            assert samples.min() == -32768
            assert samples[0] == -32768
        finally:
            await transport.disconnect()

    @pytest.mark.asyncio
    async def test_disconnect_idempotent(self):
        transport = LocalTransport()
        await transport.disconnect()
        assert not transport.is_connected

    @pytest.mark.asyncio
    async def test_disconnect_retains_only_stream_whose_close_failed_for_retry(self):
        class FakeStream:
            def __init__(self, *, close_errors: list[Exception | None]) -> None:
                self.close_errors = close_errors
                self.stop_calls = 0
                self.close_calls = 0

            def stop(self) -> None:
                self.stop_calls += 1

            def close(self) -> None:
                self.close_calls += 1
                error = self.close_errors.pop(0)
                if error is not None:
                    raise error

        input_stream = FakeStream(close_errors=[RuntimeError("input close failed"), None])
        output_stream = FakeStream(close_errors=[None])
        transport = LocalTransport()
        transport._input_stream = input_stream
        transport._output_stream = output_stream
        transport._connected = True

        with pytest.raises(RuntimeError, match="input close failed"):
            await transport.disconnect()

        assert transport._input_stream is input_stream
        assert transport._output_stream is None
        assert not transport.is_connected
        with pytest.raises(RuntimeError, match="cleanup is incomplete"):
            await transport.connect()

        await transport.disconnect()

        assert transport._input_stream is None
        assert input_stream.close_calls == 2
        assert output_stream.close_calls == 1

    @pytest.mark.asyncio
    async def test_failed_connect_keeps_startup_error_primary_when_rollback_fails(self):
        class FailOnceStream:
            def __init__(self) -> None:
                self.close_calls = 0

            def stop(self) -> None:
                pass

            def close(self) -> None:
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError("rollback close failed")

        startup_error = RuntimeError("output start failed")
        input_stream = FailOnceStream()
        transport = LocalTransport()
        transport._input_stream = input_stream

        with pytest.raises(RuntimeError, match="output start failed") as exc_info:
            await transport._raise_failed_connect_after_cleanup(startup_error)

        assert exc_info.value is startup_error
        assert isinstance(exc_info.value.__cause__, RuntimeError)
        assert "rollback close failed" in str(exc_info.value.__cause__)
        assert transport._input_stream is input_stream

        await transport.disconnect()
        assert transport._input_stream is None

    @pytest.mark.asyncio
    async def test_send_audio_when_not_connected(self):
        """send_audio reports False when the device is not connected."""
        transport = LocalTransport()
        chunk = _make_chunk()
        delivered = await transport.send_audio(chunk)
        assert delivered is False

    @pytest.mark.asyncio
    async def test_send_audio_returns_false_when_output_queue_completely_full(self):
        """send_audio returns False (and enqueues nothing) when no slots are free.

        Marks the transport connected WITHOUT starting sounddevice so no audio
        thread drains ``_out_queue`` mid-test; ``qsize()`` is then deterministic
        on any host (no hardware required).
        """
        transport = LocalTransport(
            LocalTransportConfig(max_pending_out_chunks=1, output_preroll_frames=0)
        )
        transport._connected = True
        sr = transport._audio_format.sample_rate
        frame_bytes = transport._frame_samples * transport._audio_format.frame_size
        # Fill the single slot with one frame.
        one_frame = _make_chunk(frame_bytes, sample_rate=sr)
        assert await transport.send_audio(one_frame) is True
        assert transport._out_queue.qsize() == 1
        # Queue is now completely full (available == 0); the next send must
        # return False and enqueue nothing.
        another = _make_chunk(frame_bytes, sample_rate=sr)
        assert await transport.send_audio(another) is False
        assert transport._out_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_send_audio_partial_fit_when_queue_near_full(self):
        """send_audio enqueues what fits and drops only the overflow tail.

        The frames that fit are still queued (the bot plays as much as it can),
        but ``send_audio`` returns ``False`` because the dropped tail was not
        delivered — so the transport stage records an honest drop.

        No sounddevice stream is started, so nothing drains the queue during the
        assertions (deterministic on any host).
        """
        transport = LocalTransport(
            LocalTransportConfig(max_pending_out_chunks=2, output_preroll_frames=0)
        )
        transport._connected = True
        sr = transport._audio_format.sample_rate
        frame_bytes = transport._frame_samples * transport._audio_format.frame_size
        # A 5-frame chunk with only 2 slots: partial fit → 2 enqueued, False.
        big_chunk = _make_chunk(5 * frame_bytes, sample_rate=sr)
        delivered = await transport.send_audio(big_chunk)
        assert delivered is False  # tail dropped → reported as not delivered
        assert transport._out_queue.qsize() == 2  # but what fit is still queued

    @pytest.mark.asyncio
    async def test_send_audio_available_one_keeps_head_slice(self):
        """available == 1 boundary: exactly the HEAD frame is enqueued.

        Proves ``slices[:available]`` keeps the head of the chunk, not the tail,
        when only a single slot is free on an empty maxsize=1 queue.  The
        truncated send reports ``False`` because the tail was dropped.
        """
        transport = LocalTransport(
            LocalTransportConfig(max_pending_out_chunks=1, output_preroll_frames=0)
        )
        transport._connected = True
        frame_bytes = transport._frame_samples * transport._audio_format.frame_size
        # Three distinct frames so head vs tail are distinguishable.
        head = b"\x01\x02" * (frame_bytes // 2)
        mid = b"\x03\x04" * (frame_bytes // 2)
        tail = b"\x05\x06" * (frame_bytes // 2)
        chunk = AudioChunk(data=head + mid + tail, format=transport._audio_format)
        delivered = await transport.send_audio(chunk)
        assert delivered is False  # tail dropped → reported as not delivered
        assert transport._out_queue.qsize() == 1
        queued = transport._out_queue.get_nowait()
        # The head slice is retained, not the tail.
        assert queued.chunk.data == head

    @pytest.mark.asyncio
    async def test_output_callback_pushes_reference_during_preroll_silence(self):
        """Pre-roll silence still pushes one full-frame reference per callback.

        Keeps the far-end (reference) stream 1:1 with the near-end (mic) stream
        even before the jitter buffer primes.
        """
        np = pytest.importorskip("numpy")
        transport = LocalTransport()
        # A consumer (AudioRouter) has attached: the first drain arms capture.
        transport.drain_aec_reference_frames()
        frame_samples = transport._frame_samples
        frame_bytes = frame_samples * transport._audio_format.frame_size
        outdata = np.ones((frame_samples, 1), dtype=np.float32)
        # No queued audio: every callback emits silence but still pushes a ref.
        for _ in range(3):
            transport._output_callback(np, outdata.copy(), frame_samples, None, None)
        assert not transport._primed  # never primed without queued audio
        frames = transport.drain_aec_reference_frames()
        assert len(frames) == 3
        assert all(len(frame.data) == frame_bytes for frame in frames)
        assert all(frame.format == transport._audio_format for frame in frames)

    @pytest.mark.asyncio
    async def test_output_preroll_depth_is_configurable(self):
        """A one-frame depth begins playback as soon as one frame is queued."""
        np = pytest.importorskip("numpy")
        transport = LocalTransport(LocalTransportConfig(output_preroll_frames=1))
        transport._connected = True
        frame_samples = transport._frame_samples
        pcm = (1000).to_bytes(2, "little", signed=True) * frame_samples
        chunk = AudioChunk(data=pcm, format=transport._audio_format)
        assert await transport.send_audio(chunk) is True
        outdata = np.zeros((frame_samples, 1), dtype=np.float32)

        transport._output_callback(np, outdata, frame_samples, None, None)

        assert transport._primed is True
        assert transport._out_queue.empty()
        assert np.any(outdata)

    @pytest.mark.asyncio
    async def test_output_callback_skips_reference_until_consumer_attached(self):
        """The hot output callback does no per-frame reference work when AEC is
        off (no consumer has drained), then begins capturing once one attaches."""
        np = pytest.importorskip("numpy")
        transport = LocalTransport()
        frame_samples = transport._frame_samples
        outdata = np.ones((frame_samples, 1), dtype=np.float32)

        # No consumer yet: pushes are skipped, nothing buffered.
        transport._output_callback(np, outdata.copy(), frame_samples, None, None)
        assert transport._aec_ref_queue.empty()

        # A consumer attaches by draining; subsequent callbacks capture.
        transport.drain_aec_reference_frames()
        transport._output_callback(np, outdata.copy(), frame_samples, None, None)
        assert not transport._aec_ref_queue.empty()

    @pytest.mark.asyncio
    async def test_clear_audio_keeps_aec_reference_queue(self):
        """Barge-in must keep already-played references whose echo still arrives.

        ``clear_audio()`` drops queued playback and re-primes, but it must NOT
        drain ``_aec_ref_queue`` — the residual echo of the bot's last words is
        still reaching the mic.
        """
        np = pytest.importorskip("numpy")
        transport = LocalTransport()
        # A consumer (AudioRouter) has attached: the first drain arms capture.
        transport.drain_aec_reference_frames()
        frame_samples = transport._frame_samples
        outdata = np.ones((frame_samples, 1), dtype=np.float32)
        transport._output_callback(np, outdata, frame_samples, None, None)
        assert not transport._aec_ref_queue.empty()

        await transport.clear_audio()

        assert not transport._aec_ref_queue.empty()  # references retained
        assert transport._primed is False  # jitter buffer re-armed

    @pytest.mark.asyncio
    async def test_config_defaults(self):
        config = LocalTransportConfig()
        assert config.audio_format == PCM16_MONO_24K
        assert config.frame_duration_ms == 20
        assert config.output_preroll_frames == 3
        assert config.input_device is None
        assert config.output_device is None
        assert LocalTransportConfig(output_preroll_frames=0).output_preroll_frames == 0

    @pytest.mark.parametrize("value", [-1, 1.5, True])
    def test_config_rejects_invalid_output_preroll_frames(self, value: object):
        with pytest.raises(ValueError, match="output_preroll_frames"):
            LocalTransportConfig(output_preroll_frames=value)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [0, -1, 1.5, True])
    def test_config_rejects_invalid_frame_duration(self, value: object):
        with pytest.raises(ValueError, match="frame_duration_ms"):
            LocalTransportConfig(frame_duration_ms=value)  # type: ignore[arg-type]

    @pytest.mark.parametrize("value", [-1, 1.5, True])
    def test_config_rejects_invalid_output_queue_bound(self, value: object):
        with pytest.raises(ValueError, match="max_pending_out_chunks"):
            LocalTransportConfig(max_pending_out_chunks=value)  # type: ignore[arg-type]

    def test_config_rejects_non_pcm16_audio_format(self):
        with pytest.raises(ValueError, match="audio_format must be PCM16"):
            LocalTransportConfig(
                audio_format=AudioFormat(
                    sample_rate=8_000,
                    channels=1,
                    sample_width=1,
                    encoding="mulaw",
                )
            )

    def test_config_rejects_preroll_larger_than_bounded_output_queue(self):
        with pytest.raises(ValueError, match="cannot exceed max_pending_out_chunks"):
            LocalTransportConfig(max_pending_out_chunks=2, output_preroll_frames=3)

    def test_config_allows_any_preroll_depth_with_unbounded_output_queue(self):
        config = LocalTransportConfig(
            max_pending_out_chunks=0,
            output_preroll_frames=1_000,
        )
        assert config.output_preroll_frames == 1_000

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

        # Default: 24kHz, 20ms frames → 480 samples → 960 bytes per frame.
        # send_audio splits by the transport's own frame size, so feed a chunk
        # at the transport's rate.  Send a 4800-byte chunk (typical TTS size).
        big_chunk = _make_chunk(4800, sample_rate=24000)
        await transport.send_audio(big_chunk)

        pieces: list[bytes] = []
        while not transport._out_queue.empty():
            pieces.append(transport._out_queue.get_nowait().chunk.data)

        # 4800 / 960 = 5 → 5 full frames, no remainder.
        assert len(pieces) == 5
        assert all(len(p) == 960 for p in pieces)

        await transport.disconnect()

    @pytest.mark.asyncio
    async def test_send_audio_drops_whole_chunk_when_out_queue_lacks_capacity(self):
        transport = LocalTransport(
            LocalTransportConfig(
                audio_format=PCM16_MONO_24K,
                frame_duration_ms=20,
                max_pending_out_chunks=1,
                output_preroll_frames=0,
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
        from easycat.transports.local import _QueuedOutputChunk

        transport = LocalTransport()
        frame_samples = transport._frame_samples
        preroll_frames = transport._config.output_preroll_frames

        def _fresh_outdata():
            return np.ones((frame_samples, 1), dtype=np.float32)

        # One frame queued is below the pre-roll threshold: still silence.
        loud = AudioChunk(
            data=(1000).to_bytes(2, "little", signed=True) * frame_samples,
            format=transport._audio_format,
        )
        transport._out_queue.put_nowait(_QueuedOutputChunk(chunk=loud))
        assert transport._out_queue.qsize() < preroll_frames

        outdata = _fresh_outdata()
        transport._output_callback(np, outdata, frame_samples, None, None)
        assert not transport._primed
        assert (outdata == 0).all()  # silence before prime
        assert transport._out_queue.qsize() == 1  # frame retained, not drained

        # Fill up to the pre-roll target; the next callback primes and drains.
        while transport._out_queue.qsize() < preroll_frames:
            transport._out_queue.put_nowait(_QueuedOutputChunk(chunk=loud))

        outdata = _fresh_outdata()
        transport._output_callback(np, outdata, frame_samples, None, None)
        assert transport._primed
        assert not (outdata == 0).all()  # real audio after prime
        assert transport._out_queue.qsize() == preroll_frames - 1

    @pytest.mark.asyncio
    async def test_output_callback_does_not_reprime_after_ordinary_underrun(self):
        """A transient empty queue within one stream must not add a new pre-roll."""
        np = pytest.importorskip("numpy")
        from easycat.transports.local import _QueuedOutputChunk

        transport = LocalTransport()
        transport._primed = True
        frame_samples = transport._frame_samples
        outdata = np.ones((frame_samples, 1), dtype=np.float32)

        transport._output_callback(np, outdata, frame_samples, None, None)

        assert transport._primed is True
        assert (outdata == 0).all()

        loud = AudioChunk(
            data=(1000).to_bytes(2, "little", signed=True) * frame_samples,
            format=transport._audio_format,
        )
        transport._out_queue.put_nowait(_QueuedOutputChunk(chunk=loud))
        outdata = np.zeros((frame_samples, 1), dtype=np.float32)

        transport._output_callback(np, outdata, frame_samples, None, None)

        assert np.any(outdata)
        assert transport._out_queue.empty()

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

    @pytest.mark.asyncio
    async def test_late_prior_stream_callbacks_do_not_touch_reconnected_transport(
        self,
        monkeypatch,
    ):
        """A callback queued before close must not cross into the next session.

        PortAudio callbacks run off-loop and can race stream teardown.  Keep the
        old fake callbacks callable after ``stop``/``close`` to model that late
        delivery deterministically without requiring an audio device.
        """
        np = pytest.importorskip("numpy")
        input_callbacks: list[object] = []
        output_callbacks: list[object] = []

        class FakeStream:
            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def close(self) -> None:
                pass

        class FakeSoundDevice:
            @staticmethod
            def InputStream(*, callback: object, **_kwargs: object) -> FakeStream:
                input_callbacks.append(callback)
                return FakeStream()

            @staticmethod
            def OutputStream(*, callback: object, **_kwargs: object) -> FakeStream:
                output_callbacks.append(callback)
                return FakeStream()

        def fake_require_module(module_name: str, **_kwargs: object) -> object:
            if module_name == "sounddevice":
                return FakeSoundDevice()
            return np

        monkeypatch.setattr(local_mod, "require_module", fake_require_module)
        transport = LocalTransport(LocalTransportConfig(output_preroll_frames=0))
        await transport.connect()
        old_input = input_callbacks[0]
        old_output = output_callbacks[0]
        await transport.disconnect()
        await transport.connect()

        try:
            frame_samples = transport._frame_samples
            stale_indata = np.ones((frame_samples, 1), dtype=np.float32)
            old_input(stale_indata, frame_samples, None, None)  # type: ignore[operator]
            for _ in range(3):
                await asyncio.sleep(0)
            assert transport._in_queue.empty()

            frame_data = b"\x01\x00" * frame_samples
            assert await transport.send_audio(
                AudioChunk(data=frame_data, format=transport.audio_format)
            )
            stale_outdata = np.ones((frame_samples, 1), dtype=np.float32)
            old_output(stale_outdata, frame_samples, None, None)  # type: ignore[operator]

            assert (stale_outdata == 0).all()
            assert transport._out_queue.qsize() == 1

            current_outdata = np.zeros((frame_samples, 1), dtype=np.float32)
            output_callbacks[1](current_outdata, frame_samples, None, None)  # type: ignore[operator]
            assert np.any(current_outdata)
            assert transport._out_queue.empty()
        finally:
            await transport.disconnect()
