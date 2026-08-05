"""WebRTC PCM frame conversion and outbound playout source."""

from __future__ import annotations

import asyncio
import fractions
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, ClassVar
from weakref import ReferenceType, WeakKeyDictionary, ref

from easycat._extras import require_module
from easycat.audio_format import AudioChunk, AudioFormat
from easycat.events import EventBus, TransportAudioDelivered
from easycat.runtime._event_tasks import RuntimeEventTaskScope
from easycat.runtime.scope import RuntimeScope
from easycat.teardown_budgets import WEBRTC_AUDIO_ACLOSE_TIMEOUT_S

logger = logging.getLogger(__name__)

WEBRTC_SAMPLE_RATE = 48_000
_FRAME_DURATION_MS = 20
_FRAME_SAMPLES = (WEBRTC_SAMPLE_RATE * _FRAME_DURATION_MS) // 1_000


def audio_frame_pcm16_bytes(frame: Any) -> tuple[bytes, int, int]:
    """Extract valid interleaved PCM16 bytes from an ``av.AudioFrame``.

    ``bytes(frame.planes[0])`` can include PyAV line padding. Slice by frame
    metadata instead of ``to_ndarray()`` so the WebRTC extra does not require
    NumPy.
    """
    frame_rate = int(getattr(frame, "sample_rate", None) or WEBRTC_SAMPLE_RATE)
    layout = getattr(frame, "layout", None)
    channels = len(getattr(layout, "channels", ()) or ()) or 1
    frame_format = getattr(frame, "format", None)
    sample_width = int(getattr(frame_format, "bytes", 2) or 2)
    samples = int(getattr(frame, "samples", 0) or 0)
    planes = list(getattr(frame, "planes", ()))
    if not planes:
        return b"", frame_rate, channels

    is_planar = bool(getattr(frame_format, "is_planar", False))
    if is_planar and channels > 1 and len(planes) >= channels and samples > 0:
        raw = _interleave_audio_planes(
            planes,
            samples=samples,
            channels=channels,
            sample_width=sample_width,
        )
    else:
        raw = bytes(planes[0])
        valid_bytes = samples * channels * sample_width
        if valid_bytes > 0:
            raw = raw[:valid_bytes]
    return raw, frame_rate, channels


def _interleave_audio_planes(
    planes: list[Any],
    *,
    samples: int,
    channels: int,
    sample_width: int,
) -> bytes:
    """Return interleaved bytes for planar PCM frames."""
    plane_bytes = []
    valid_plane_bytes = samples * sample_width
    for plane in planes[:channels]:
        data = bytes(plane)[:valid_plane_bytes]
        if len(data) < valid_plane_bytes:
            data += bytes(valid_plane_bytes - len(data))
        plane_bytes.append(data)

    interleaved = bytearray(samples * channels * sample_width)
    offset = 0
    for sample in range(samples):
        start = sample * sample_width
        end = start + sample_width
        for channel in plane_bytes:
            interleaved[offset : offset + sample_width] = channel[start:end]
            offset += sample_width
    return bytes(interleaved)


@dataclass
class _QueuedOutboundChunk:
    transport_data: bytes
    original_chunk: AudioChunk
    session_id: str | None = None
    turn_id: str | None = None
    turn_ref: object | None = None
    transport_offset: int = 0
    original_reported: int = 0


_DeliveredChunk = tuple[AudioChunk, str | None, str | None, object | None]


_DELIVERY_EVENT_TASK_NAME = "webrtc_delivery_emit"
_DELIVERY_EVENT_COHORT = "transport-events"
# Delivery subscribers are application-owned and can suppress cancellation.
# A worker that exceeds the reviewed aclose bound transfers here until it
# settles, keeping a durable strong owner without a module-level task set.
_BACKGROUND_EMIT_SCOPES: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    ReferenceType[RuntimeEventTaskScope],
] = WeakKeyDictionary()


def _background_emit_scope() -> RuntimeEventTaskScope:
    """Return the detached-worker owner for the current event loop."""
    loop = asyncio.get_running_loop()
    scope_ref = _BACKGROUND_EMIT_SCOPES.get(loop)
    scope = None if scope_ref is None else scope_ref()
    if scope is None:
        scope = RuntimeEventTaskScope(
            owner_label="webrtc-background-delivery",
            member_name=_DELIVERY_EVENT_TASK_NAME,
            cohort=_DELIVERY_EVENT_COHORT,
            logger=logger,
            failure_message="Detached WebRTC delivery event worker failed",
        )
        _BACKGROUND_EMIT_SCOPES[loop] = ref(scope)
    return scope


class OutboundAudioSource:
    """Queue-backed source that produces paced 20 ms Opus-compatible frames."""

    _AEC_REF_QUEUE_MAX: ClassVar[int] = 100
    # Backlog cap for not-yet-emitted delivery events: a slow EventBus
    # subscriber must not grow memory without bound during sustained playback.
    # Overflow drops the oldest events; the retained ones stay FIFO.
    _EMIT_QUEUE_MAX: ClassVar[int] = 256
    # Teardown budget for draining in-flight delivery events before the drain
    # worker is cancelled outright.
    _ACLOSE_TIMEOUT_S: ClassVar[float] = WEBRTC_AUDIO_ACLOSE_TIMEOUT_S

    def __init__(self) -> None:
        self._queue: asyncio.Queue[_QueuedOutboundChunk] = asyncio.Queue(maxsize=100)
        self._pending: deque[_QueuedOutboundChunk] = deque()
        self._pts = 0
        self._start: float | None = None
        self._event_bus: EventBus | None = None
        # Delivery events must not block RTP pacing. A single tracked worker
        # drains them FIFO and ``aclose`` awaits it (bounded) during transport
        # teardown.
        self._emit_queue: deque[TransportAudioDelivered] = deque(maxlen=self._EMIT_QUEUE_MAX)
        self._emit_worker: asyncio.Task[None] | None = None
        self._event_tasks = RuntimeEventTaskScope(
            owner_label="webrtc-outbound-delivery",
            member_name=_DELIVERY_EVENT_TASK_NAME,
            cohort=_DELIVERY_EVENT_COHORT,
            logger=logger,
            failure_message="WebRTC delivery event worker failed",
        )
        self._AudioFrame: type | None = None
        self._aec_ref_queue: deque[AudioChunk] = deque(maxlen=self._AEC_REF_QUEUE_MAX)
        self._ref_format: AudioFormat | None = None
        self._aec_reference_enabled = False

    @property
    def _emit_tasks(self) -> set[asyncio.Task[Any]]:
        """Compatibility inspection of scope-owned delivery workers."""
        return set(self._event_tasks.tasks())

    def _bind_event_scope(self, scope: RuntimeScope) -> None:
        """Attach delivery workers to the owning transport's runtime child."""
        self._event_tasks.bind(scope)

    def create_track(self) -> Any:
        """Return an aiortc ``MediaStreamTrack`` wrapping this source."""
        transport_src = self
        aiortc: Any = require_module("aiortc", extra="webrtc", purpose="WebRTC transport")

        class _Track(aiortc.MediaStreamTrack):
            kind = "audio"

            async def recv(self_track) -> Any:
                return await transport_src._recv()

        return _Track()

    def enqueue(
        self,
        pcm_s16_48k: bytes,
        *,
        original_chunk: AudioChunk,
        session_id: str | None = None,
        turn_id: str | None = None,
        turn_ref: object | None = None,
    ) -> bool:
        """Enqueue PCM16 mono audio, returning false when the queue is full."""
        if not pcm_s16_48k:
            return True
        try:
            self._queue.put_nowait(
                _QueuedOutboundChunk(
                    transport_data=pcm_s16_48k,
                    original_chunk=original_chunk,
                    session_id=session_id,
                    turn_id=turn_id,
                    turn_ref=turn_ref,
                )
            )
        except asyncio.QueueFull:
            logger.debug("Outbound WebRTC audio queue full — dropping frame")
            return False
        return True

    async def _recv(self) -> Any:
        """Produce the next paced 20 ms audio frame for aiortc."""
        self._load_audio_frame_type()
        await self._pace()

        pcm_data, delivered_chunks, padded_bytes = self._build_pcm_frame(_FRAME_SAMPLES * 2)
        self._record_silence_reference(padded_bytes)
        frame = self._make_audio_frame(pcm_data)
        self._queue_delivery_events(delivered_chunks)
        return frame

    def _load_audio_frame_type(self) -> None:
        if self._AudioFrame is None:
            av = require_module("av", extra="webrtc", purpose="WebRTC audio frames")
            self._AudioFrame = av.AudioFrame

    async def _pace(self) -> None:
        if self._start is None:
            self._start = time.monotonic()
        expected = self._start + (self._pts / WEBRTC_SAMPLE_RATE)
        wait = expected - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    def _build_pcm_frame(self, frame_bytes: int) -> tuple[bytes, list[_DeliveredChunk], int]:
        buf = bytearray()
        delivered_chunks: list[_DeliveredChunk] = []
        while len(buf) < frame_bytes:
            result = self._take_audio_slice(frame_bytes - len(buf))
            if result is None:
                break
            audio_slice, delivered_chunk = result
            buf.extend(audio_slice)
            if delivered_chunk is not None:
                delivered_chunks.append(delivered_chunk)

        padded_bytes = frame_bytes - len(buf)
        if padded_bytes:
            buf.extend(bytes(padded_bytes))
        return bytes(buf), delivered_chunks, padded_bytes

    def _take_audio_slice(self, max_bytes: int) -> tuple[bytes, _DeliveredChunk | None] | None:
        queued = self._next_pending_chunk()
        if queued is None:
            return None

        remaining = queued.transport_data[queued.transport_offset :]
        take = min(max_bytes, len(remaining))
        audio_slice = remaining[:take]
        queued.transport_offset += take
        delivered_chunk = self._advance_original_delivery(queued)

        if queued.transport_offset >= len(queued.transport_data):
            self._pending.popleft()
        return audio_slice, delivered_chunk

    def _next_pending_chunk(self) -> _QueuedOutboundChunk | None:
        while True:
            if not self._pending:
                try:
                    self._pending.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    return None
            queued = self._pending[0]
            if queued.transport_offset < len(queued.transport_data):
                return queued
            self._pending.popleft()

    def _advance_original_delivery(self, queued: _QueuedOutboundChunk) -> _DeliveredChunk | None:
        original_size = len(queued.original_chunk.data)
        if queued.transport_offset >= len(queued.transport_data):
            reported = original_size
        else:
            reported = min(
                original_size,
                int((queued.transport_offset / len(queued.transport_data)) * original_size),
            )
        if reported <= queued.original_reported:
            return None

        delivered_data = queued.original_chunk.data[queued.original_reported : reported]
        queued.original_reported = reported
        delivered_chunk = AudioChunk(
            data=delivered_data,
            format=queued.original_chunk.format,
            timestamp=queued.original_chunk.timestamp,
        )
        if delivered_data and self._aec_reference_enabled:
            # Keep the bytes and their true pre-resample format together.  A
            # bare-byte queue forced AudioRouter to guess the format from the
            # near-end mic chunk, silently defeating AEC's rate-mismatch guard
            # whenever advanced callers disabled TTS/transport alignment.
            self._aec_ref_queue.append(delivered_chunk)
            self._ref_format = delivered_chunk.format
        return (
            delivered_chunk,
            queued.session_id,
            queued.turn_id,
            queued.turn_ref,
        )

    def _record_silence_reference(self, padded_bytes: int) -> None:
        """Mirror playout padding into the AEC reference as session-rate silence.

        Padding is silence the far end actually hears — both fully silent
        frames and the tail of a partial final chunk — so it must be recorded
        or the reference stream permanently lags real playout.
        """
        if padded_bytes <= 0 or not self._aec_reference_enabled or self._ref_format is None:
            return
        padded_samples = padded_bytes // 2  # transport frames are PCM16 mono
        silence_samples = self._ref_format.sample_rate * padded_samples // WEBRTC_SAMPLE_RATE
        if silence_samples > 0:
            self._aec_ref_queue.append(
                AudioChunk(
                    data=bytes(silence_samples * self._ref_format.frame_size),
                    format=self._ref_format,
                )
            )

    def _make_audio_frame(self, pcm_data: bytes) -> Any:
        assert self._AudioFrame is not None
        frame = self._AudioFrame(format="s16", layout="mono", samples=_FRAME_SAMPLES)
        frame.sample_rate = WEBRTC_SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, WEBRTC_SAMPLE_RATE)
        frame.planes[0].update(pcm_data)
        self._pts += _FRAME_SAMPLES
        return frame

    def _queue_delivery_events(self, delivered_chunks: list[_DeliveredChunk]) -> None:
        if self._event_bus is None:
            return
        for delivered_chunk, session_id, turn_id, turn_ref in delivered_chunks:
            if delivered_chunk.data:
                if len(self._emit_queue) == self._EMIT_QUEUE_MAX:
                    logger.debug("Delivery-event backlog full — dropping oldest event")
                self._emit_queue.append(
                    TransportAudioDelivered(
                        chunk=delivered_chunk,
                        session_id=session_id,
                        turn_id=turn_id,
                        turn_ref=turn_ref,
                    )
                )
        if self._emit_queue and (self._emit_worker is None or self._emit_worker.done()):
            worker = self._event_tasks.create_task(
                self._drain_emit_queue(),
                task_name="webrtc:delivery-emit",
            )
            if worker is None:
                self._emit_queue.clear()
                return
            self._emit_worker = worker

    async def _drain_emit_queue(self) -> None:
        """Emit queued delivery events in playback order."""
        assert self._event_bus is not None
        while self._emit_queue:
            event = self._emit_queue.popleft()
            try:
                await self._event_bus.emit(event)
            except Exception:
                logger.exception("TransportAudioDelivered emit failed")

    def drain_aec_reference_frames(self) -> list[AudioChunk]:
        """Arm AEC capture and return typed far-end frames oldest first."""
        self._aec_reference_enabled = True
        frames = list(self._aec_ref_queue)
        self._aec_ref_queue.clear()
        return frames

    def clear(self) -> None:
        """Discard queued audio while retaining already-played AEC references."""
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._pending.clear()

    def stop(self) -> None:
        """No-op; peer teardown owns the outbound track lifecycle."""

    async def aclose(self) -> None:
        """Await in-flight delivery event work during transport teardown.

        Bounded: a subscriber that never returns cannot hang teardown — after
        ``_ACLOSE_TIMEOUT_S`` the drain worker is cancelled instead of awaited.
        """
        current = asyncio.current_task()
        # EventBus subscribers run inside the delivery worker. A subscriber is
        # allowed to initiate transport teardown, which re-enters here; never
        # wait for or cancel that same task or it would deadlock until the
        # timeout and then abort its own event dispatch.
        tasks = [task for task in self._emit_tasks if task is not current]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=self._ACLOSE_TIMEOUT_S)
            if pending:
                logger.warning(
                    "Delivery-event drain exceeded %.1fs during teardown — cancelling",
                    self._ACLOSE_TIMEOUT_S,
                )
                for task in pending:
                    task.cancel()
                    self._event_tasks.discard_task(task)
                    _background_emit_scope().adopt_task(task)
                # Let cooperative workers observe cancellation without awaiting a
                # subscriber that deliberately suppresses it.
                await asyncio.sleep(0)
            for task in done:
                if not task.cancelled():
                    try:
                        task.exception()
                    except Exception:  # noqa: BLE001, S110  # pragma: no cover - defensive teardown
                        pass
        self._emit_queue.clear()
        await self._event_tasks.release_standalone_if_empty()
