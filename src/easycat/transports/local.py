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
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from easycat.audio_format import PCM16_MONO_16K, AudioChunk, AudioFormat
from easycat.extras import require_module

logger = logging.getLogger(__name__)

# Frame duration for mic capture chunks (milliseconds).
_DEFAULT_FRAME_MS = 20


@dataclass
class LocalTransportConfig:
    """Configuration for :class:`LocalTransport`."""

    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_16K)
    frame_duration_ms: int = _DEFAULT_FRAME_MS
    input_device: int | str | None = None
    output_device: int | str | None = None
    max_pending_in_chunks: int = 200
    max_pending_out_chunks: int = 200


class LocalTransport:
    """Transport backed by local microphone and speaker via ``sounddevice``.

    Implements the ``Transport`` protocol from :mod:`easycat.providers`.

    Audio is captured from the default (or specified) input device and played
    back on the default (or specified) output device.  Capture runs in a
    background thread managed by ``sounddevice``; chunks are enqueued for the
    async ``receive_audio`` iterator.  ``send_audio`` writes PCM data to an
    ``asyncio.Queue`` that feeds the output stream callback.
    """

    def __init__(self, config: LocalTransportConfig | None = None) -> None:
        self._config = config or LocalTransportConfig()
        self._audio_format = self._config.audio_format

        # Queues bridging the sounddevice callback threads and asyncio.
        self._in_queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue(
            maxsize=self._config.max_pending_in_chunks,
        )
        # Output queue uses stdlib thread-safe queue because the sounddevice
        # output callback runs on a separate audio thread.
        self._out_queue: thread_queue.Queue[bytes | None] = thread_queue.Queue(
            maxsize=self._config.max_pending_out_chunks,
        )

        self._input_stream: object | None = None
        self._output_stream: object | None = None
        self._connected = False

        # Samples per frame for the configured frame duration.
        self._frame_samples = (
            self._audio_format.sample_rate * self._config.frame_duration_ms
        ) // 1000

    # ── Transport protocol ────────────────────────────────────────

    async def connect(self) -> None:
        """Open audio devices and start capture / playback streams."""
        if self._connected:
            return

        # Reinitialize queues to clear any stale sentinels from a previous session.
        self._in_queue = asyncio.Queue(maxsize=self._config.max_pending_in_chunks)
        self._out_queue = thread_queue.Queue(maxsize=self._config.max_pending_out_chunks)

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
            loop.call_soon_threadsafe(self._in_queue.put_nowait, chunk)

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
                pcm = self._out_queue.get_nowait()
            except thread_queue.Empty:
                pcm = None

            if pcm is None:
                outdata[:] = 0  # type: ignore[index]
                return

            num_samples = len(pcm) // 2
            samples = struct.unpack(f"<{num_samples}h", pcm)
            arr = _np_array(samples, np).astype(np.float32) / 32767.0
            # Pad / trim to fit outdata
            out_flat = outdata.reshape(-1)  # type: ignore[union-attr]
            n = min(len(arr), len(out_flat))
            out_flat[:n] = arr[:n]
            if n < len(out_flat):
                out_flat[n:] = 0

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

        # Signal end of stream to any pending receive_audio iterators.
        try:
            self._in_queue.put_nowait(None)
        except asyncio.QueueFull:
            logger.debug("Input queue full when enqueueing sentinel; ignoring")

        self._connected = False

    async def receive_audio(self) -> AsyncIterator[AudioChunk]:
        """Yield microphone audio chunks as they arrive."""
        while self._connected or not self._in_queue.empty():
            chunk = await self._in_queue.get()
            if chunk is None:
                break
            yield chunk

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Queue an audio chunk for speaker playback.

        Chunks larger than one callback frame are split so each enqueued
        piece fits exactly into the output buffer without truncation.
        """
        if not self._connected:
            return
        frame_bytes = self._frame_samples * self._audio_format.frame_size
        data = chunk.data
        offset = 0
        while offset < len(data):
            try:
                self._out_queue.put_nowait(data[offset : offset + frame_bytes])
            except thread_queue.Full:
                logger.warning("Output audio queue full — dropping frame")
            offset += frame_bytes

    # ── Helpers ────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected


def _np_array(samples: tuple[int, ...], np: object) -> object:
    """Create a numpy array from a tuple — isolated for type-checker friendliness."""
    return np.array(samples)  # type: ignore[union-attr]
