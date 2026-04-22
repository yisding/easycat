"""Local transport: microphone capture and speaker playback.

Uses the ``sounddevice`` library for audio capture/playback. If the
dependency is missing, ``connect()`` raises an ImportError with
installation instructions.
"""

from __future__ import annotations

import asyncio
import logging
import queue as thread_queue
import struct
from dataclasses import dataclass, field

from easycat.audio_format import PCM16_MONO_24K, AudioChunk, AudioFormat
from easycat.events import EventBus, TransportAudioDelivered
from easycat.extras import require_module
from easycat.transports._base import _AudioQueueMixin

logger = logging.getLogger(__name__)

# Frame duration for mic capture chunks (milliseconds).
_DEFAULT_FRAME_MS = 20


@dataclass
class _QueuedOutputChunk:
    chunk: AudioChunk
    turn_id: str | None = None
    turn_ref: object | None = None


def _enqueue_or_drop(q: asyncio.Queue[object], item: object) -> None:
    """Best-effort enqueue — silently drops if the queue is full."""
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        logger.debug("Inbound mic audio queue full — dropping frame")


@dataclass
class LocalTransportConfig:
    """Configuration for :class:`LocalTransport`."""

    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_24K)
    frame_duration_ms: int = _DEFAULT_FRAME_MS
    input_device: int | str | None = None
    output_device: int | str | None = None
    max_pending_in_chunks: int = 200
    max_pending_out_chunks: int = 200


class LocalTransport(_AudioQueueMixin):
    """Transport backed by local microphone and speaker via ``sounddevice``.

    Implements the ``Transport`` protocol from :mod:`easycat.providers`.

    Audio is captured from the default (or specified) input device and played
    back on the default (or specified) output device.  Capture runs in a
    background thread managed by ``sounddevice``; chunks are enqueued for the
    async ``receive_audio`` iterator.  ``send_audio`` writes PCM data to an
    ``asyncio.Queue`` that feeds the output stream callback.
    """

    reports_audio_delivery = True

    def __init__(self, config: LocalTransportConfig | None = None) -> None:
        self._config = config or LocalTransportConfig()
        self._audio_format = self._config.audio_format
        self._init_audio_queue(self._config.max_pending_in_chunks)

        # Output queue uses stdlib thread-safe queue because the sounddevice
        # output callback runs on a separate audio thread.
        self._out_queue: thread_queue.Queue[_QueuedOutputChunk | None] = thread_queue.Queue(
            maxsize=self._config.max_pending_out_chunks,
        )
        self._event_bus: EventBus | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

        self._input_stream: object | None = None
        self._output_stream: object | None = None

        # Samples per frame for the configured frame duration.
        self._frame_samples = (
            self._audio_format.sample_rate * self._config.frame_duration_ms
        ) // 1000

    # ── Transport protocol ────────────────────────────────────────

    async def connect(self) -> None:
        """Open audio devices and start capture / playback streams."""
        if self._connected:
            return

        self._reset_audio_queue()
        self._out_queue = thread_queue.Queue(maxsize=self._config.max_pending_out_chunks)
        self._loop = asyncio.get_running_loop()

        sd = require_module(
            "sounddevice",
            extra="local",
            purpose="LocalTransport audio I/O",
        )

        loop = asyncio.get_running_loop()
        frame_size = self._frame_samples

        # --- Input stream (mic) ---
        def _input_callback(
            indata: object, frames: int, time_info: object, status: object
        ) -> None:
            import numpy as np  # type: ignore[import-untyped]

            arr = indata  # type: ignore[assignment]
            if hasattr(arr, "copy"):
                arr = arr.copy()  # type: ignore[union-attr]
            # Convert float32 [-1, 1] to int16
            pcm = (arr * 32767).astype(np.int16).tobytes()  # type: ignore[union-attr]
            chunk = AudioChunk(data=pcm, format=self._audio_format)
            loop.call_soon_threadsafe(_enqueue_or_drop, self._in_queue, chunk)

        self._input_stream = sd.InputStream(
            samplerate=self._audio_format.sample_rate,
            channels=self._audio_format.channels,
            dtype="float32",
            blocksize=frame_size,
            device=self._config.input_device,
            callback=_input_callback,
        )
        self._input_stream.start()  # type: ignore[union-attr]

        # --- Output stream (speaker) ---
        def _output_callback(
            outdata: object, frames: int, time_info: object, status: object
        ) -> None:
            import numpy as np  # type: ignore[import-untyped]

            try:
                queued = self._out_queue.get_nowait()
            except thread_queue.Empty:
                queued = None

            if queued is None:
                outdata[:] = 0  # type: ignore[index]
                return

            pcm = queued.chunk.data
            num_samples = len(pcm) // 2
            samples = struct.unpack(f"<{num_samples}h", pcm)
            arr = _np_array(samples, np).astype(np.float32) / 32767.0
            # Pad / trim to fit outdata
            out_flat = outdata.reshape(-1)  # type: ignore[union-attr]
            n = min(len(arr), len(out_flat))
            out_flat[:n] = arr[:n]
            if n < len(out_flat):
                out_flat[n:] = 0
            self._schedule_audio_delivery(queued)

        self._output_stream = sd.OutputStream(
            samplerate=self._audio_format.sample_rate,
            channels=self._audio_format.channels,
            dtype="float32",
            blocksize=frame_size,
            device=self._config.output_device,
            callback=_output_callback,
        )
        self._output_stream.start()  # type: ignore[union-attr]
        self._connected = True

    async def disconnect(self) -> None:
        """Close audio devices and release resources."""
        if not self._connected:
            return

        for stream in (self._input_stream, self._output_stream):
            if stream is not None:
                try:
                    stream.stop()  # type: ignore[union-attr]
                    stream.close()  # type: ignore[union-attr]
                except Exception:
                    logger.debug("Error closing audio stream", exc_info=True)

        self._input_stream = None
        self._output_stream = None

        # Drain both queues.
        while not self._in_queue.empty():
            try:
                self._in_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        while not self._out_queue.empty():
            try:
                self._out_queue.get_nowait()
            except thread_queue.Empty:
                break

        self._enqueue_sentinel()
        self._connected = False
        self._loop = None

    async def send_audio(self, chunk: AudioChunk) -> bool:
        """Queue an audio chunk for speaker playback.

        Chunks larger than one callback frame are split so each enqueued
        piece fits exactly into the output buffer without truncation.
        """
        if not self._connected:
            return False
        frame_bytes = self._frame_samples * self._audio_format.frame_size
        data = chunk.data
        slices = [
            data[offset : offset + frame_bytes] for offset in range(0, len(data), frame_bytes)
        ]
        available = self._out_queue.maxsize - self._out_queue.qsize()
        if self._out_queue.maxsize > 0 and len(slices) > available:
            logger.warning("Output audio queue full — dropping chunk")
            return False

        turn_id = getattr(chunk, "_easycat_turn_id", None)
        turn_ref = getattr(chunk, "_easycat_turn_ref", None)
        for piece in slices:
            self._out_queue.put_nowait(
                _QueuedOutputChunk(
                    chunk=AudioChunk(data=piece, format=chunk.format, timestamp=chunk.timestamp),
                    turn_id=turn_id,
                    turn_ref=turn_ref,
                )
            )
        return True

    async def clear_audio(self) -> None:
        """Discard queued outbound audio awaiting speaker playback."""
        while not self._out_queue.empty():
            try:
                self._out_queue.get_nowait()
            except thread_queue.Empty:
                break

    def _schedule_audio_delivery(self, queued: _QueuedOutputChunk) -> None:
        loop = self._loop
        if self._event_bus is None or loop is None or loop.is_closed():
            return

        def _emit() -> None:
            if self._event_bus is None:
                return
            loop.create_task(
                self._event_bus.emit(
                    TransportAudioDelivered(
                        chunk=queued.chunk,
                        turn_id=queued.turn_id,
                        turn_ref=queued.turn_ref,
                    )
                )
            )

        loop.call_soon_threadsafe(_emit)

    def version_info(self) -> dict[str, str]:
        try:
            from importlib.metadata import version

            sd_ver = version("sounddevice")
        except Exception:
            sd_ver = "unknown"
        return {
            "provider": "local",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": sd_ver,
        }


def _np_array(samples: tuple[int, ...], np: object) -> object:
    """Create a numpy array from a tuple — isolated for type-checker friendliness."""
    return np.array(samples)  # type: ignore[union-attr]
