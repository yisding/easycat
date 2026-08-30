"""Local transport: microphone capture and speaker playback.

Uses the ``sounddevice`` library for audio capture/playback. If the
dependency is missing, ``connect()`` raises an ImportError with
installation instructions.
"""

from __future__ import annotations

import asyncio
import logging
import queue as thread_queue
from dataclasses import dataclass, field
from functools import partial
from typing import Any, ClassVar, NoReturn

from easycat._audio_utils import validate_pcm16_format
from easycat._extras import require_module
from easycat.audio_format import PCM16_MONO_24K, AudioChunk, AudioFormat
from easycat.events import EventBus, TransportAudioDelivered
from easycat.transports._base import AudioQueueMixin, make_version_info
from easycat.transports._limits import DEFAULT_INBOUND_AUDIO_MAX_BYTES

logger = logging.getLogger(__name__)

# Frame duration for mic capture chunks (milliseconds).
_DEFAULT_FRAME_MS = 20


@dataclass
class _QueuedOutputChunk:
    chunk: AudioChunk
    session_id: str | None = None
    turn_id: str | None = None
    turn_ref: object | None = None


@dataclass
class LocalTransportConfig:
    """Configuration for :class:`LocalTransport`."""

    default_echo_cancellation_enabled: ClassVar[bool] = True

    audio_format: AudioFormat = field(default_factory=lambda: PCM16_MONO_24K)
    frame_duration_ms: int = _DEFAULT_FRAME_MS
    input_device: int | str | None = None
    output_device: int | str | None = None
    max_pending_in_chunks: int = 200
    # ~10 s of speaker buffer at 20 ms/frame.  Sized to absorb faster-than-
    # real-time TTS bursts for typical responses without dropping the tail,
    # while keeping ``await_drain``'s playout wait (which is proportional to the
    # queued backlog) bounded to a sane few seconds rather than the ~40 s a much
    # larger cap would allow.  A genuine overflow now surfaces honestly via the
    # partial-fit drop in ``send_audio`` instead of being hidden behind a giant
    # buffer.
    max_pending_out_chunks: int = 500
    # Output jitter-buffer pre-roll: silence is emitted until this many frames
    # have accumulated so a small burst of late TTS chunks does not underrun
    # the speaker callback. Three frames is at most ~60 ms at the default frame
    # size and often only one callback period when a TTS chunk contains several
    # frames. Set to 0 to disable the pre-roll.
    output_preroll_frames: int = 3
    max_pending_in_bytes: int = DEFAULT_INBOUND_AUDIO_MAX_BYTES

    def __post_init__(self) -> None:
        validate_pcm16_format("audio_format", self.audio_format)
        if (
            not isinstance(self.frame_duration_ms, int)
            or isinstance(self.frame_duration_ms, bool)
            or self.frame_duration_ms <= 0
        ):
            raise ValueError("frame_duration_ms must be a positive integer")
        if self.audio_format.sample_rate * self.frame_duration_ms // 1000 <= 0:
            raise ValueError(
                "frame_duration_ms is too short to produce a sample for the "
                "configured audio_format"
            )
        if (
            not isinstance(self.output_preroll_frames, int)
            or isinstance(self.output_preroll_frames, bool)
            or self.output_preroll_frames < 0
        ):
            raise ValueError("output_preroll_frames must be a non-negative integer")
        if (
            not isinstance(self.max_pending_out_chunks, int)
            or isinstance(self.max_pending_out_chunks, bool)
            or self.max_pending_out_chunks < 0
        ):
            raise ValueError("max_pending_out_chunks must be a non-negative integer")
        if (
            self.max_pending_out_chunks > 0
            and self.output_preroll_frames > self.max_pending_out_chunks
        ):
            raise ValueError(
                "output_preroll_frames cannot exceed max_pending_out_chunks "
                "when the output queue is bounded"
            )


class LocalTransport(AudioQueueMixin):
    """Transport backed by local microphone and speaker via ``sounddevice``.

    Implements the ``Transport`` protocol from :mod:`easycat.providers`.

    Audio is captured from the default (or specified) input device and played
    back on the default (or specified) output device.  Capture runs in a
    background thread managed by ``sounddevice``; chunks are enqueued for the
    async ``receive_audio`` iterator.  ``send_audio`` writes PCM data to an
    ``asyncio.Queue`` that feeds the output stream callback.
    """

    send_audio_is_nonblocking = True
    transport_kind = "local"
    default_echo_cancellation_enabled = True
    reports_audio_delivery = True

    # Maximum number of AEC reference frames to buffer between mic callbacks.
    # At 20 ms/frame this covers ~2 s; excess frames are silently discarded
    # (same outcome as having no reference for that window).
    _AEC_REF_QUEUE_MAX = 100

    def __init__(self, config: LocalTransportConfig | None = None) -> None:
        self._config = config or LocalTransportConfig()
        self._audio_format = self._config.audio_format
        self._init_audio_queue(
            self._config.max_pending_in_chunks,
            self._config.max_pending_in_bytes,
        )

        # Output queue uses stdlib thread-safe queue because the sounddevice
        # output callback runs on a separate audio thread.
        self._out_queue: thread_queue.Queue[_QueuedOutputChunk | None] = thread_queue.Queue(
            maxsize=self._config.max_pending_out_chunks,
        )

        # AEC far-end reference drain queue.  The output callback pushes raw
        # PCM at actual speaker-playback time (not at send_audio enqueue time).
        # The async ingress pipeline drains this queue in _process_chunk *before*
        # AudioStage.execute() so feed_reference() is always called before
        # AEC.process() for the corresponding mic window, which AEC3 requires to
        # converge.  Uses a plain thread_queue.Queue because the output callback
        # runs on the sounddevice audio thread while the drain happens on the
        # asyncio event loop thread.
        self._aec_ref_queue: thread_queue.Queue[bytes] = thread_queue.Queue(
            maxsize=self._AEC_REF_QUEUE_MAX
        )

        # Far-end reference capture is armed only once a consumer (the
        # AudioRouter) first drains via ``drain_aec_reference_frames()``.  Until
        # then the hot output callback skips the per-frame reference push
        # entirely, so a session running without AEC does no per-frame allocation
        # or queue churn on the audio thread.
        self._aec_reference_enabled: bool = False

        self._lifecycle_lock = asyncio.Lock()
        self._event_bus: EventBus | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Sounddevice callbacks run on dedicated audio threads and can already
        # be queued when a stream is stopped.  Bind each stream pair to a
        # generation so a late callback from a previous connection cannot put
        # stale mic audio into, or consume speaker audio from, a later one.
        self._stream_generation = 0

        # Output jitter buffer: the callback emits silence until ``_out_queue``
        # has built up the configured number of pre-roll frames, then drains
        # one frame per callback. Reset to ``False`` on connect and barge-in;
        # ordinary transient underruns stay primed so a streaming utterance
        # does not repeatedly pay the pre-roll delay.
        self._primed: bool = False

        self._input_stream: Any = None
        self._output_stream: Any = None

        # Samples per frame for the configured frame duration.
        self._frame_samples = (
            self._audio_format.sample_rate * self._config.frame_duration_ms
        ) // 1000

    # ── Transport protocol ────────────────────────────────────────

    async def connect(self) -> None:
        """Open audio devices and start capture / playback streams."""
        async with self._lifecycle_lock:
            if self._connected:
                return
            if self._input_stream is not None or self._output_stream is not None:
                raise RuntimeError(
                    "Local transport cleanup is incomplete; call disconnect() before reconnecting"
                )

            self._stream_generation += 1
            stream_generation = self._stream_generation
            self._reset_audio_queue()
            self._out_queue = thread_queue.Queue(maxsize=self._config.max_pending_out_chunks)
            self._aec_ref_queue = thread_queue.Queue(maxsize=self._AEC_REF_QUEUE_MAX)
            # A fresh session starts with capture disarmed; the AudioRouter re-arms
            # it on its first reference drain when AEC is wired.
            self._aec_reference_enabled = False
            self._primed = False
            sd = require_module(
                "sounddevice",
                extra="local",
                purpose="LocalTransport audio I/O",
            )
            np = require_module(
                "numpy",
                extra="local",
                purpose="LocalTransport audio I/O",
            )

            loop = asyncio.get_running_loop()
            self._loop = loop
            frame_size = self._frame_samples

            # --- Input stream (mic) ---
            def _input_callback(indata: Any, frames: int, time_info: object, status: object) -> None:
                arr = indata
                if hasattr(arr, "copy"):
                    arr = arr.copy()
                # Convert float32 [-1, 1] to int16.  Clip before the cast:
                # sounddevice/PortAudio does not hard-clamp float32 capture, so an
                # overdriven input can exceed 1.0, and numpy's int16 cast wraps
                # (sign-flips) rather than saturating.  Mirror the resampler sites.
                pcm = np.clip(arr * 32767.0, -32768, 32767).astype(np.int16).tobytes()
                chunk = AudioChunk(data=pcm, format=self._audio_format)
                # ``_enqueue_chunk`` is sync-safe and emits the canonical
                # ``inbound_queue_full`` TransportDegraded event on overflow, so
                # local mic drops surface in the journal like every other
                # transport.  Schedule it onto the loop from the audio thread.
                # ``call_soon_threadsafe`` takes no kwargs, so bind callback
                # metadata via ``partial``.
                #
                # The sounddevice audio thread can fire one more callback after the
                # loop has been torn down (e.g. an abrupt Ctrl-C that closes the
                # loop before the input stream is stopped).  Scheduling onto a
                # closed loop raises ``RuntimeError: Event loop is closed`` from the
                # cffi callback; guard + swallow it so shutdown stays quiet.
                if loop.is_closed():
                    return
                try:
                    loop.call_soon_threadsafe(
                        partial(
                            self._enqueue_input_chunk,
                            chunk,
                            stream_generation=stream_generation,
                        )
                    )
                except RuntimeError:
                    pass

            try:
                self._input_stream = sd.InputStream(
                    samplerate=self._audio_format.sample_rate,
                    channels=self._audio_format.channels,
                    dtype="float32",
                    blocksize=frame_size,
                    device=self._config.input_device,
                    callback=_input_callback,
                )
                self._input_stream.start()

                # --- Output stream (speaker) ---
                self._output_stream = sd.OutputStream(
                    samplerate=self._audio_format.sample_rate,
                    channels=self._audio_format.channels,
                    dtype="float32",
                    blocksize=frame_size,
                    device=self._config.output_device,
                    callback=partial(
                        self._run_output_callback,
                        np,
                        stream_generation=stream_generation,
                    ),
                )
                self._output_stream.start()
            except BaseException as startup_error:  # noqa: BLE001 - partial acquisition boundary
                # ``_connected`` becomes true only after both devices start, so a
                # failure here must unwind the handles directly rather than rely on
                # the steady-state flag. This also makes callers that do not wrap
                # ``connect()`` in an exit stack safe from partial acquisition.
                await self._raise_failed_connect_after_cleanup(startup_error)
            self._connected = True

    async def _raise_failed_connect_after_cleanup(
        self,
        startup_error: BaseException,
    ) -> NoReturn:
        """Roll back partial device acquisition without losing the primary failure."""
        try:
            await self.disconnect()
        except asyncio.CancelledError as cancellation:
            if isinstance(startup_error, asyncio.CancelledError):
                raise startup_error from cancellation
            raise cancellation from startup_error
        except BaseException as cleanup_error:
            raise startup_error from cleanup_error
        raise startup_error

    def _enqueue_input_chunk(self, chunk: AudioChunk, *, stream_generation: int) -> None:
        """Enqueue mic audio only while its originating stream is still current."""
        if stream_generation != self._stream_generation or not self._connected:
            return
        self._enqueue_chunk(chunk, context="mic")

    def _run_output_callback(
        self,
        np: Any,
        outdata: Any,
        frames: int,
        time_info: object,
        status: object,
        *,
        stream_generation: int,
    ) -> None:
        """Dispatch one speaker callback only while its stream is current."""
        if stream_generation != self._stream_generation or not self._connected:
            # A callback from a stopped stream may arrive after reconnect.
            # It must not consume the current speaker queue or report delivery
            # for a different session.
            outdata[:] = 0
            return
        self._output_callback(
            np,
            outdata,
            frames,
            time_info,
            status,
            stream_generation=stream_generation,
        )

    def _push_aec_reference(self, frame: bytes) -> None:
        """Push one far-end reference frame with a drop-oldest overflow policy.

        Called from the sounddevice audio thread on every output callback so the
        far-end (reference) stream stays sample-aligned 1:1 with the near-end
        (mic) stream.  When the queue is full the oldest frame is evicted so the
        freshest reference is always retained — stale references are useless to
        AEC3, which only cancels echo of audio that is about to arrive.
        """
        try:
            self._aec_ref_queue.put_nowait(frame)
        except thread_queue.Full:
            try:
                self._aec_ref_queue.get_nowait()  # drop oldest, keep freshest
            except thread_queue.Empty:
                pass
            try:
                self._aec_ref_queue.put_nowait(frame)
            except thread_queue.Full:
                pass

    def _output_callback(
        self,
        np: Any,
        outdata: Any,
        frames: int,
        time_info: object,
        status: object,
        *,
        stream_generation: int | None = None,
    ) -> None:
        """Speaker callback: drain one queued frame per call behind a pre-roll.

        ``np`` is bound via ``partial`` at ``connect()`` time so this stays a
        plain method (testable without ``sounddevice``) while sounddevice still
        sees the four-argument callback signature it expects.

        Every return path pushes exactly one full-frame far-end reference (real
        audio, pre-roll silence, or underrun silence) so the AEC reference
        stream stays 1:1 with the mic stream and AEC3 keeps converging.
        """
        frame_bytes = self._frame_samples * self._audio_format.frame_size

        # Jitter-buffer pre-roll: emit silence until enough frames have queued,
        # then drain one frame per callback. Re-primes after ``clear_audio()``
        # and ``connect()``, but not after an ordinary queue underrun.
        if not self._primed:
            if self._out_queue.qsize() < self._config.output_preroll_frames:
                outdata[:] = 0
                if self._aec_reference_enabled:
                    self._push_aec_reference(bytes(frame_bytes))  # silence keeps far/near 1:1
                return
            self._primed = True

        try:
            queued = self._out_queue.get_nowait()
        except thread_queue.Empty:
            queued = None

        if queued is None:
            outdata[:] = 0
            if self._aec_reference_enabled:
                self._push_aec_reference(bytes(frame_bytes))  # silence keeps far/near 1:1
            return

        pcm = queued.chunk.data
        arr = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        arr /= 32767.0
        # Pad / trim to fit outdata
        out_flat = outdata.reshape(-1)
        n = min(len(arr), len(out_flat))
        out_flat[:n] = arr[:n]
        if n < len(out_flat):
            out_flat[n:] = 0

        # Push the far-end reference at actual playback time, padded/trimmed to
        # one full frame so it matches the bytes actually emitted to the speaker
        # (and stays 1:1 with the mic frame).  The async ingress pipeline drains
        # this before processing the mic frame so AEC3 always receives the
        # far-end reference before the near-end signal.  Skipped (no allocation)
        # until an AEC consumer has attached.
        if self._aec_reference_enabled:
            if len(pcm) < frame_bytes:
                ref = pcm + bytes(frame_bytes - len(pcm))
            else:
                ref = pcm[:frame_bytes]
            self._push_aec_reference(ref)

        self._schedule_audio_delivery(queued, stream_generation=stream_generation)

    def _flush_queues(self) -> None:
        """Drain all audio queues synchronously (called from disconnect and teardown)."""
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
        while not self._aec_ref_queue.empty():
            try:
                self._aec_ref_queue.get_nowait()
            except thread_queue.Empty:
                break

    async def disconnect(self) -> None:
        """Close audio devices and release resources."""
        async with self._lifecycle_lock:
            if (
                not self._connected
                and self._input_stream is None
                and self._output_stream is None
                and self._loop is None
            ):
                return

            # Invalidate callback closures before stopping either device.  A native
            # callback may have already queued work onto the event loop; the
            # generation check in that work then drops it instead of affecting a
            # subsequent connection.
            self._stream_generation += 1
            cleanup_errors: list[Exception] = []
            streams = (
                ("input", self._input_stream),
                ("output", self._output_stream),
            )
            for stream_name, stream in streams:
                if stream is not None:
                    try:
                        stream.stop()
                    except Exception:
                        logger.debug("Error stopping audio stream", exc_info=True)
                    try:
                        stream.close()
                    except Exception as exc:
                        logger.debug("Error closing audio stream", exc_info=True)
                        cleanup_errors.append(exc)
                    else:
                        if stream_name == "input" and self._input_stream is stream:
                            self._input_stream = None
                        elif stream_name == "output" and self._output_stream is stream:
                            self._output_stream = None
            self._flush_queues()
            self._enqueue_sentinel()
            self._connected = False
            self._loop = None
            await self._drain_emit_tasks()
            if cleanup_errors:
                raise cleanup_errors[0]

    async def send_audio(self, chunk: AudioChunk) -> bool:
        """Queue an audio chunk for speaker playback.

        Chunks larger than one callback frame are split so each enqueued
        piece fits exactly into the output buffer without truncation.

        Returns ``False`` if the device is disconnected or if the playback
        queue lacks capacity for the full chunk, so callers don't credit the
        caller with hearing audio that was never scheduled.  A partial fit still
        enqueues the frames that fit (so the bot plays as much as possible) but
        also reports ``False``, because the dropped tail was not delivered.
        """
        if not self._connected:
            return False
        frame_bytes = self._frame_samples * self._audio_format.frame_size
        data = chunk.data
        slices = [
            data[offset : offset + frame_bytes] for offset in range(0, len(data), frame_bytes)
        ]
        available = self._out_queue.maxsize - self._out_queue.qsize()
        if self._out_queue.maxsize > 0 and available == 0:
            logger.warning("Output audio queue full — dropped %d frame(s)", len(slices))
            return False
        truncated = False
        if self._out_queue.maxsize > 0 and len(slices) > available:
            # Partial fit: enqueue what fits and drop only the overflow tail so
            # the bot still plays as much as possible — but report the drop
            # honestly by returning ``False`` below.  The tail was never
            # scheduled, so the transport stage must record a drop (not a clean
            # delivery) and callers must not credit unplayed audio.
            logger.warning(
                "Output audio queue near full — dropped %d of %d frame(s)",
                len(slices) - available,
                len(slices),
            )
            slices = slices[:available]
            truncated = True

        session_id = getattr(chunk, "_easycat_session_id", None)
        turn_id = getattr(chunk, "_easycat_turn_id", None)
        turn_ref = getattr(chunk, "_easycat_turn_ref", None)
        for piece in slices:
            self._out_queue.put_nowait(
                _QueuedOutputChunk(
                    chunk=AudioChunk(data=piece, format=chunk.format, timestamp=chunk.timestamp),
                    session_id=session_id,
                    turn_id=turn_id,
                    turn_ref=turn_ref,
                )
            )
        # ``False`` when any frame was dropped (the queue lacked capacity for the
        # full chunk), matching the docstring so the drop metric stays honest.
        return not truncated

    def drain_aec_reference_frames(self) -> list[AudioChunk]:
        """Return all pending AEC far-end reference frames, draining the queue.

        This is the shared AEC reference capability used by both the local and
        webrtc transports: any ``reports_audio_delivery`` transport that exposes
        it has its far-end reference drained and fed by the router before the
        near-end mic frame is processed.

        Called from the async ingress pipeline in AudioRouter._process_chunk
        before AudioStage.execute() so the AEC far-end reference is always fed
        before the corresponding near-end mic frame is processed.

        Frames are returned oldest-first with the transport's playout format.

        Thread-safe: the output callback pushes to this queue from the
        sounddevice audio thread while this method is called from the asyncio
        event loop thread.

        Calling this also *arms* reference capture: the output callback only
        buffers far-end frames once a consumer has started draining, so a
        session without AEC never pays the per-frame push cost.
        """
        self._aec_reference_enabled = True
        frames: list[AudioChunk] = []
        while True:
            try:
                frames.append(
                    AudioChunk(
                        data=self._aec_ref_queue.get_nowait(),
                        format=self._audio_format,
                    )
                )
            except thread_queue.Empty:
                break
        return frames

    async def clear_audio(self) -> None:
        """Discard queued outbound audio awaiting speaker playback."""
        while not self._out_queue.empty():
            try:
                self._out_queue.get_nowait()
            except thread_queue.Empty:
                break
        # Deliberately do NOT drain ``_aec_ref_queue`` here: the already-played
        # reference frames describe the bot's last words, whose echo is still
        # arriving at the mic during the barge-in.  Retaining them lets AEC
        # cancel that residual echo instead of mistaking it for fresh speech.
        # The queue stays bounded by the drop-oldest policy in
        # ``_push_aec_reference``; a full disconnect/_flush_queues clears it.
        # Re-prime the jitter buffer so the post-barge-in utterance builds up
        # its own pre-roll before playback resumes.
        self._primed = False

    def pending_playout_ms(self) -> float:
        """Return the milliseconds of audio still queued for speaker playback.

        Backed by the outbound frame count so ``await_drain`` can avoid
        reporting the bot drained while the local speaker buffer is non-empty.
        """
        return self._out_queue.qsize() * self._config.frame_duration_ms

    def _schedule_audio_delivery(
        self,
        queued: _QueuedOutputChunk,
        *,
        stream_generation: int | None = None,
    ) -> None:
        loop = self._loop
        if self._event_bus is None or loop is None or loop.is_closed():
            return

        def _emit() -> None:
            if stream_generation is not None and (
                stream_generation != self._stream_generation or not self._connected
            ):
                return
            if self._event_bus is None:
                return
            self._create_emit_task(
                self._event_bus.emit(
                    TransportAudioDelivered(
                        chunk=queued.chunk,
                        session_id=queued.session_id,
                        turn_id=queued.turn_id,
                        turn_ref=queued.turn_ref,
                    )
                ),
                task_name="local:audio-delivered-emit",
            )

        try:
            loop.call_soon_threadsafe(_emit)
        except RuntimeError:
            # Like input callbacks, sounddevice can outlive event-loop teardown
            # by one callback. Delivery telemetry is best effort.
            pass

    def version_info(self) -> dict[str, str]:
        return make_version_info("local", "sounddevice")
