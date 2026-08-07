"""WebRTC outbound audio source tests."""

from __future__ import annotations

import asyncio
import time

import pytest

from easycat.audio_format import PCM16_MONO_16K, AudioChunk
from easycat.events import EventBus, TransportAudioDelivered
from easycat.runtime.scope import RuntimeScope, RuntimeSupervisor
from easycat.transports._webrtc_audio import OutboundAudioSource, _background_emit_scope
from easycat.transports.webrtc import WebRTCTransport

from ._webrtc_fakes import (
    _HAS_WEBRTC_DEPS,
    _FakeMediaStreamTrack,
    _install_fake_webrtc_modules,
)


class TestOutboundAudioSource:
    def test_transport_scope_is_shared_with_outbound_delivery_workers(self):
        transport = WebRTCTransport()
        root = RuntimeScope.create_root(
            name="session",
            root_id="test-root:webrtc-outbound",
            supervisor=RuntimeSupervisor(capacity=1),
            survivor_capacity=1,
        )

        transport.set_runtime_scope(root, name="transport-runtime")

        assert transport._emit_scope is not None
        assert transport._outbound._event_tasks.scope is transport._emit_scope
        assert transport._receive_tasks.scope is transport._emit_scope

    def test_create_track_uses_shared_fake_dependency_seam(self, monkeypatch):
        _install_fake_webrtc_modules(monkeypatch)

        track = OutboundAudioSource().create_track()

        assert isinstance(track, _FakeMediaStreamTrack)

    def test_enqueue_and_drain(self):
        source = OutboundAudioSource()
        data = bytes(960 * 2)  # 20ms at 48kHz mono s16
        source.enqueue(data, original_chunk=AudioChunk(data=data, format=PCM16_MONO_16K))
        assert not source._queue.empty()

    def test_enqueue_overflow(self):
        source = OutboundAudioSource()
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
        source = OutboundAudioSource()
        frame = await source._recv()
        assert frame.sample_rate == 48000
        assert frame.samples == 960
        # Frame data should be all zeros (silence).
        data = bytes(frame.planes[0])
        assert data == bytes(960 * 2)

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_recv_returns_enqueued_data(self):
        source = OutboundAudioSource()
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
        source = OutboundAudioSource()
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
        source = OutboundAudioSource()
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
        source = OutboundAudioSource()
        test_data = bytes([0xFF]) * 960 * 2
        source.enqueue(
            test_data,
            original_chunk=AudioChunk(data=test_data, format=PCM16_MONO_16K),
        )
        source.clear()

        frame = await source._recv()
        data = bytes(frame.planes[0])
        assert data == bytes(960 * 2)  # silence


def _disable_pacing(source: OutboundAudioSource) -> None:
    """Backdate the pacing clock so ``_recv`` never sleeps for real time."""
    source._start = time.monotonic() - 1000.0


class TestOutboundAudioAecReference:
    """Issue H: WebRTC captures session-rate AEC far-end reference at playback.

    The outbound source records the delivered (session-rate) chunk at ``_recv``
    playback time, and ``WebRTCTransport.drain_aec_reference_frames()`` exposes
    it as the shared AEC reference capability the AudioRouter feeds *before* the
    near-end mic frame (mirroring the LocalTransport fix).
    """

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_drain_matches_delivered_chunks_and_clears(self):
        source = OutboundAudioSource()
        _disable_pacing(source)
        source.drain_aec_reference_frames()  # arm capture: a consumer has attached
        bus = EventBus()
        delivered: list[TransportAudioDelivered] = []
        bus.subscribe(TransportAudioDelivered, lambda e: delivered.append(e))
        source._event_bus = bus

        frame_bytes = 960 * 2
        # Two distinct chunks spanning multiple frames so order matters.
        chunk_a = bytes([0xAA]) * (frame_bytes + frame_bytes // 2)
        chunk_b = bytes([0xBB]) * (frame_bytes * 2)
        source.enqueue(chunk_a, original_chunk=AudioChunk(data=chunk_a, format=PCM16_MONO_16K))
        source.enqueue(chunk_b, original_chunk=AudioChunk(data=chunk_b, format=PCM16_MONO_16K))

        # Pump until everything queued/pending has been produced.
        for _ in range(6):
            await source._recv()
            if source._queue.empty() and not source._pending:
                break

        # Let the queued TransportAudioDelivered emits run.
        for _ in range(5):
            await asyncio.sleep(0)

        delivered_bytes = b"".join(e.chunk.data for e in delivered)
        ref_frames = source.drain_aec_reference_frames()
        assert isinstance(ref_frames, list)
        assert all(isinstance(frame, AudioChunk) for frame in ref_frames)
        assert all(frame.format == PCM16_MONO_16K for frame in ref_frames)
        # Same order and content as the delivered session-rate audio, plus the
        # final frame's half-frame padding recorded as session-rate silence
        # (960 transport bytes -> 160 samples at 16 kHz).
        assert b"".join(frame.data for frame in ref_frames) == delivered_bytes + bytes(160 * 2)
        assert delivered_bytes == chunk_a + chunk_b

        # Draining clears the queue.
        assert source.drain_aec_reference_frames() == []

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_recv_schedules_delivery_emit_without_blocking(self):
        """_recv must not await EventBus.emit on the RTP pacing path: the
        TransportAudioDelivered event is scheduled as a tracked task and is not
        delivered until the loop yields after _recv returns."""
        source = OutboundAudioSource()
        _disable_pacing(source)
        bus = EventBus()
        delivered: list[TransportAudioDelivered] = []

        async def _handler(e):
            await asyncio.sleep(0)  # yield: recv must not have awaited us
            delivered.append(e)

        bus.subscribe(TransportAudioDelivered, _handler)
        source._event_bus = bus

        frame_bytes = 960 * 2
        played = bytes([0xAA]) * frame_bytes
        source.enqueue(played, original_chunk=AudioChunk(data=played, format=PCM16_MONO_16K))

        await source._recv()

        # Scheduled, not awaited: handler has not completed, but a task exists.
        assert delivered == []
        assert source._emit_tasks
        # Draining the scheduled task delivers the event.
        await asyncio.gather(*list(source._emit_tasks))
        assert [e.chunk.data for e in delivered] == [played]
        assert not source._emit_tasks  # done-callback discarded it

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_delivery_emits_preserve_chunk_order_with_suspending_handler(self):
        """Off-path emission must stay FIFO: a subscriber that suspends
        mid-handler must never observe chunk N+1 before chunk N (the single
        drain worker guarantees this; per-chunk fire-and-forget tasks did
        not)."""
        source = OutboundAudioSource()
        _disable_pacing(source)
        bus = EventBus()
        delivered: list[bytes] = []

        async def _handler(e):
            # Suspend twice so interleaved per-event tasks would reorder.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            delivered.append(e.chunk.data)

        bus.subscribe(TransportAudioDelivered, _handler)
        source._event_bus = bus

        frame_bytes = 960 * 2
        chunk_a = bytes([0x01]) * frame_bytes
        chunk_b = bytes([0x02]) * frame_bytes
        chunk_c = bytes([0x03]) * frame_bytes
        for data in (chunk_a, chunk_b, chunk_c):
            source.enqueue(data, original_chunk=AudioChunk(data=data, format=PCM16_MONO_16K))
        while True:
            await source._recv()
            if source._queue.empty() and not source._pending:
                break

        await source.aclose()
        assert delivered == [chunk_a, chunk_b, chunk_c]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_aclose_drains_scheduled_emit_tasks(self):
        """Teardown must drain the source's off-RTP-path emit tasks so in-flight
        TransportAudioDelivered events are awaited, not cancelled-and-lost at
        loop teardown (mirrors LocalTransport.stop() -> _drain_emit_tasks())."""
        source = OutboundAudioSource()
        _disable_pacing(source)
        bus = EventBus()
        delivered: list[TransportAudioDelivered] = []

        async def _handler(e):
            await asyncio.sleep(0)  # yield so recv/aclose caller cannot inline us
            delivered.append(e)

        bus.subscribe(TransportAudioDelivered, _handler)
        source._event_bus = bus

        frame_bytes = 960 * 2
        played = bytes([0xAA]) * frame_bytes
        source.enqueue(played, original_chunk=AudioChunk(data=played, format=PCM16_MONO_16K))

        await source._recv()
        # Scheduled off the RTP path, not yet delivered.
        assert delivered == []
        assert source._emit_tasks

        # Teardown drains: the in-flight emit is awaited (delivered), not lost.
        await source.aclose()
        assert [e.chunk.data for e in delivered] == [played]
        assert not source._emit_tasks

    @pytest.mark.asyncio
    async def test_aclose_is_noop_without_pending_tasks(self):
        """aclose() on a source that never scheduled an emit is a safe no-op."""
        source = OutboundAudioSource()
        assert not source._emit_tasks
        await source.aclose()
        assert not source._emit_tasks

    @pytest.mark.asyncio
    async def test_delivery_backlog_is_bounded_drop_oldest(self):
        """The delivery-event backlog is bounded: overflow drops the oldest
        events while the retained ones keep FIFO order, so a slow subscriber
        cannot grow memory without limit during sustained playback."""
        source = OutboundAudioSource()
        bus = EventBus()
        delivered: list[bytes] = []
        bus.subscribe(TransportAudioDelivered, lambda e: delivered.append(e.chunk.data))
        source._event_bus = bus

        total = source._EMIT_QUEUE_MAX + 5
        chunks: list[tuple[AudioChunk, str | None, str | None, object | None]] = [
            (AudioChunk(data=i.to_bytes(2, "big"), format=PCM16_MONO_16K), None, None, None)
            for i in range(total)
        ]
        # All events are appended before the drain worker gets to run, so the
        # first five must be dropped to honour the bound.
        source._queue_delivery_events(chunks)
        assert len(source._emit_queue) == source._EMIT_QUEUE_MAX

        await source.aclose()
        assert delivered == [i.to_bytes(2, "big") for i in range(5, total)]

    @pytest.mark.asyncio
    async def test_aclose_cancels_hung_delivery_worker(self, monkeypatch):
        """A subscriber that never returns must not hang transport teardown:
        aclose() waits ``_ACLOSE_TIMEOUT_S`` then cancels the drain worker."""
        monkeypatch.setattr(OutboundAudioSource, "_ACLOSE_TIMEOUT_S", 0.05)
        source = OutboundAudioSource()
        bus = EventBus()
        entered = asyncio.Event()

        async def _handler(e):
            entered.set()
            await asyncio.Event().wait()  # never returns

        bus.subscribe(TransportAudioDelivered, _handler)
        source._event_bus = bus
        chunk = AudioChunk(data=b"\x01\x02", format=PCM16_MONO_16K)
        source._queue_delivery_events([(chunk, None, None, None)])
        await asyncio.wait_for(entered.wait(), timeout=1.0)

        await asyncio.wait_for(source.aclose(), timeout=1.0)
        assert not source._emit_tasks
        assert not source._emit_queue

    @pytest.mark.asyncio
    async def test_aclose_hard_bounds_cancellation_resistant_delivery_subscriber(
        self, monkeypatch
    ):
        monkeypatch.setattr(OutboundAudioSource, "_ACLOSE_TIMEOUT_S", 0.01)
        source = OutboundAudioSource()
        root = RuntimeScope.create_root(
            name="session",
            root_id="test-root:webrtc-stubborn-delivery",
            supervisor=RuntimeSupervisor(capacity=1),
            survivor_capacity=1,
        )
        source._bind_event_scope(root)
        bus = EventBus()
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()

        async def _handler(_event):
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()

        bus.subscribe(TransportAudioDelivered, _handler)
        source._event_bus = bus
        chunk = AudioChunk(data=b"\x00\x00", format=PCM16_MONO_16K)
        source._queue_delivery_events([(chunk, None, None, None)])
        await asyncio.wait_for(entered.wait(), timeout=1)
        signal = root.signal_cohort("transport-events", force=True)

        closing = asyncio.create_task(source.aclose())
        try:
            await asyncio.wait_for(asyncio.shield(closing), timeout=0.1)
            await asyncio.wait_for(root.drain_cohort(signal), timeout=0.1)
            assert cancelled.is_set()
            assert not source._emit_tasks
            assert root.empty
        finally:
            release.set()
            if not closing.done():
                await asyncio.wait_for(asyncio.shield(closing), timeout=0.5)
            for _ in range(5):
                if not _background_emit_scope().tasks():
                    break
                await asyncio.sleep(0)
            assert not _background_emit_scope().tasks()
            await root.close()

    @pytest.mark.asyncio
    async def test_aclose_is_safe_from_delivery_event_subscriber(self):
        """A delivery callback may tear down its owning transport.

        The outbound delivery worker itself invokes EventBus subscribers, so
        ``aclose()`` must not wait for or cancel that same worker when a
        subscriber requests teardown synchronously.
        """
        source = OutboundAudioSource()
        bus = EventBus()
        closed = asyncio.Event()

        async def _handler(_event):
            await source.aclose()
            closed.set()

        bus.subscribe(TransportAudioDelivered, _handler)
        source._event_bus = bus
        chunk = AudioChunk(data=b"\x01\x02", format=PCM16_MONO_16K)
        source._queue_delivery_events([(chunk, None, None, None)])

        await asyncio.wait_for(closed.wait(), timeout=0.2)
        await asyncio.sleep(0)
        assert not source._emit_tasks

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_disconnect_drains_outbound_emit_tasks(self):
        """WebRTCTransport.disconnect() must drain the outbound source's own
        emit-task set (a different set from the transport-level one), so
        scheduled delivery emits are awaited rather than dangling."""
        transport = WebRTCTransport()
        source = transport._outbound
        _disable_pacing(source)
        bus = EventBus()
        delivered: list[TransportAudioDelivered] = []

        async def _handler(e):
            await asyncio.sleep(0)
            delivered.append(e)

        bus.subscribe(TransportAudioDelivered, _handler)
        source._event_bus = bus

        frame_bytes = 960 * 2
        played = bytes([0xBB]) * frame_bytes
        source.enqueue(played, original_chunk=AudioChunk(data=played, format=PCM16_MONO_16K))

        await source._recv()
        assert source._emit_tasks  # scheduled, still pending
        assert delivered == []

        # Mark connected so disconnect() runs its full teardown path.
        transport._connected = True
        await transport.disconnect()

        # Teardown awaited the in-flight emit and cleared the source's set.
        assert not source._emit_tasks
        assert [e.chunk.data for e in delivered] == [played]

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_clear_keeps_reference_but_drops_pending_audio(self):
        source = OutboundAudioSource()
        _disable_pacing(source)
        source._aec_reference_enabled = True  # arm capture: a consumer has attached
        frame_bytes = 960 * 2

        played = bytes([0xAA]) * frame_bytes
        source.enqueue(played, original_chunk=AudioChunk(data=played, format=PCM16_MONO_16K))
        # Produce the played frame so its reference is captured.
        await source._recv()
        assert source.drain_aec_reference_frames.__self__ is source  # bound method sanity

        # Enqueue more audio that has NOT been played yet.
        future = bytes([0xBB]) * frame_bytes
        source.enqueue(future, original_chunk=AudioChunk(data=future, format=PCM16_MONO_16K))

        # Capture the already-played reference before barge-in.
        captured = list(source._aec_ref_queue)
        assert [frame.data for frame in captured] == [played]
        assert [frame.format for frame in captured] == [PCM16_MONO_16K]

        # Barge-in: clear() drops pending outbound audio but keeps the
        # already-played reference whose echo is still arriving at the mic.
        source.clear()

        assert source._queue.empty()
        assert not source._pending
        assert source.drain_aec_reference_frames() == captured

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_silent_recv_after_audio_appends_silence_reference(self):
        """A fully-silent render frame still appends a session-rate silence
        reference so the far/near streams stay 1:1 during pauses (matching the
        LocalTransport per-callback reference)."""
        source = OutboundAudioSource()
        _disable_pacing(source)
        source._aec_reference_enabled = True  # arm capture: a consumer has attached
        # Transport (48k) frame is 1920 bytes; the session-rate (16k) chunk is
        # 640 bytes / 20 ms.
        session_data = bytes([0x11]) * 640
        transport_data = bytes([0x11]) * (960 * 2)
        source.enqueue(
            transport_data,
            original_chunk=AudioChunk(data=session_data, format=PCM16_MONO_16K),
        )
        await source._recv()  # plays real audio -> real reference
        await source._recv()  # queue empty -> silence reference

        frames = source.drain_aec_reference_frames()
        assert len(frames) == 2
        assert frames[0].data == session_data
        assert frames[0].format == PCM16_MONO_16K
        # 20 ms of 16 kHz mono silence = 320 samples * 2 bytes.
        assert frames[-1].data == bytes(320 * 2)
        assert frames[-1].format == PCM16_MONO_16K

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_partial_final_frame_pads_silence_reference(self):
        """A rendered frame that is part audio, part padding must record the
        padded tail as session-rate silence: a final 10 ms chunk plays as a
        20 ms frame, and without the matching silence the reference stream
        would permanently lag real playout."""
        source = OutboundAudioSource()
        _disable_pacing(source)
        source._aec_reference_enabled = True  # arm capture: a consumer has attached
        # 30 ms of transport audio (1.5 frames at 48 kHz); the session-rate
        # original is 30 ms at 16 kHz (480 samples).
        session_data = bytes([0x22]) * (480 * 2)
        transport_data = bytes([0x22]) * (1440 * 2)
        source.enqueue(
            transport_data,
            original_chunk=AudioChunk(data=session_data, format=PCM16_MONO_16K),
        )
        await source._recv()  # full frame of audio, no padding
        await source._recv()  # half audio + half padding

        frames = source.drain_aec_reference_frames()
        # Reference = 30 ms of audio + 10 ms of silence = 40 ms at 16 kHz.
        assert b"".join(frame.data for frame in frames) == session_data + bytes(160 * 2)
        assert all(frame.format == PCM16_MONO_16K for frame in frames)

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_silent_recv_before_any_audio_appends_nothing(self):
        """Before any audio has played there is no echo and no known session
        rate, so silent render frames append no reference."""
        source = OutboundAudioSource()
        _disable_pacing(source)
        source._aec_reference_enabled = True  # arm capture: a consumer has attached
        await source._recv()
        await source._recv()
        assert source.drain_aec_reference_frames() == []

    def test_transport_drain_is_declared_capability(self):
        """The router detects drain via ``getattr`` — it must be a callable
        returning ``list[AudioChunk]`` even with no peer connected."""
        transport = WebRTCTransport()
        drain = getattr(transport, "drain_aec_reference_frames", None)
        assert callable(drain)
        frames = drain()
        assert isinstance(frames, list)
        assert frames == []

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_transport_drain_returns_played_reference(self):
        transport = WebRTCTransport()
        source = transport._outbound
        _disable_pacing(source)
        source._aec_reference_enabled = True  # arm capture: a consumer has attached

        frame_bytes = 960 * 2
        played = bytes([0xCC]) * frame_bytes
        source.enqueue(played, original_chunk=AudioChunk(data=played, format=PCM16_MONO_16K))
        await source._recv()

        frames = transport.drain_aec_reference_frames()
        assert isinstance(frames, list)
        assert all(isinstance(frame, AudioChunk) for frame in frames)
        assert b"".join(frame.data for frame in frames) == played
        assert all(frame.format == PCM16_MONO_16K for frame in frames)
        # Drained.
        assert transport.drain_aec_reference_frames() == []

    @pytest.mark.asyncio
    @pytest.mark.skipif(not _HAS_WEBRTC_DEPS, reason="aiortc/aiohttp not installed")
    async def test_transport_clear_audio_keeps_reference(self):
        transport = WebRTCTransport()
        source = transport._outbound
        _disable_pacing(source)
        source._aec_reference_enabled = True  # arm capture: a consumer has attached

        frame_bytes = 960 * 2
        played = bytes([0xDD]) * frame_bytes
        source.enqueue(played, original_chunk=AudioChunk(data=played, format=PCM16_MONO_16K))
        await source._recv()

        # Queue audio that barge-in will discard.
        future = bytes([0xEE]) * frame_bytes
        source.enqueue(future, original_chunk=AudioChunk(data=future, format=PCM16_MONO_16K))

        await transport.clear_audio()

        assert source._queue.empty()
        assert not source._pending
        # The already-played reference survives barge-in.
        frames = transport.drain_aec_reference_frames()
        assert [frame.data for frame in frames] == [played]
        assert [frame.format for frame in frames] == [PCM16_MONO_16K]
