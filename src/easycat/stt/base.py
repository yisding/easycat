"""STT provider base class with shared logic."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TypeVar

import httpx

from easycat._audio_utils import pcm_to_wav as _pcm_to_wav
from easycat._concurrency import shielded_cleanup
from easycat.audio_format import AudioChunk, AudioFormat
from easycat.events import STTEvent
from easycat.runtime.scope import (
    RuntimeMemberPolicy,
    RuntimeScope,
    RuntimeTaskAction,
    RuntimeTaskPolicy,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


DEFAULT_MAX_AUDIO_CHUNK_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_AUDIO_BUFFER_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_AUDIO_DURATION_MS = 5 * 60 * 1000.0

_AUDIO_SEND_TASK = "stt_audio_send"
_STT_RUNTIME_CANCEL_POLICY = RuntimeTaskPolicy(
    graceful=RuntimeMemberPolicy(
        cohort="stt-runtime",
        signal_token=False,
        task_action=RuntimeTaskAction.CANCEL,
    ),
    force=RuntimeMemberPolicy(
        cohort="stt-runtime",
        signal_token=False,
        task_action=RuntimeTaskAction.CANCEL,
    ),
)
_STT_RUNTIME_FINISH_POLICY = RuntimeTaskPolicy(
    graceful=RuntimeMemberPolicy(
        cohort="stt-runtime",
        signal_token=False,
        task_action=RuntimeTaskAction.FINISH,
    ),
    force=RuntimeMemberPolicy(
        cohort="stt-runtime",
        signal_token=False,
        task_action=RuntimeTaskAction.FINISH,
    ),
)
_STT_RECEIVE_FINISH_POLICY = RuntimeTaskPolicy(
    graceful=RuntimeMemberPolicy(
        cohort="stt-receive",
        signal_token=False,
        task_action=RuntimeTaskAction.FINISH,
    ),
    force=RuntimeMemberPolicy(
        cohort="stt-receive",
        signal_token=False,
        task_action=RuntimeTaskAction.FINISH,
    ),
)


class AudioBufferLimitExceeded(Exception):
    """Raised when a batch STT buffer reaches its cumulative cap.

    This is distinct from the per-chunk ``ValueError`` checks (malformed or
    impossibly large single frames). It signals that an *otherwise valid*
    stream has simply accumulated more buffered audio (total bytes or
    duration) than the configured cap allows — e.g. a long-talking caller on
    a held-open turn. Providers catch it to gracefully finalize the current
    utterance and start a fresh buffer, so the per-chunk pipeline error policy
    in ``session/_audio_router.py`` never sees it and the live call is not
    torn down.
    """


class STTBase:
    """Concrete base class for STT providers.

    Handles event queue management, audio format validation, and stream
    lifecycle. Subclasses override ``_on_start``, ``_on_audio``, and
    ``_on_end`` to add provider-specific behaviour.
    """

    # Batch-buffering state, set by subclasses that use ``_drain_buffer_to_wav``.
    _buffer: bytearray
    _audio_format: AudioFormat | None

    def __init__(
        self,
        *,
        expected_sample_rate: int | None = None,
        allow_end_during_audio_send: bool = False,
    ) -> None:
        # ``expected_sample_rate`` controls the strict-rate contract enforced
        # by ``_validate_audio``. When set, ``send_audio`` rejects any chunk
        # whose rate differs. When ``None`` (the convention used by all
        # EasyCat-bundled streaming providers), the provider is responsible
        # for resampling mismatched input to its own target rate in
        # ``_on_audio`` so callers can swap providers without crashing.
        self._event_queue: asyncio.Queue[STTEvent | None] = asyncio.Queue()
        self._running = False
        self._expected_sample_rate = expected_sample_rate
        self._allow_end_during_audio_send = allow_end_during_audio_send
        # Serialize queue replacement/closure with provider hooks. Batch providers
        # may emit events after awaiting a cap-triggered transcription in
        # ``send_audio``; without this lock, ``end_stream``/``start_stream`` could
        # close or replace the queue underneath that in-flight emit.
        self._lifecycle_lock = asyncio.Lock()
        # Streaming sockets need ordered writes but must still let end_stream()
        # preempt a reconnect-stalled send. Batch providers keep the historical
        # lifecycle lock across _on_audio so a cap-triggered HTTP transcription
        # cannot emit into a replaced queue.
        self._audio_send_lock = asyncio.Lock()
        self._active_audio_send_task: asyncio.Task[None] | None = None
        self._runtime_scope: RuntimeScope | None = None
        self._owns_runtime_scope = False
        # Distinguish work queued for an old stream from work admitted after
        # a rapid end/start cycle. Without a generation check, an audio send
        # waiting on ``_audio_send_lock`` can leak into the successor stream.
        self._stream_generation = 0
        # A partial provider startup can allocate external resources before it
        # raises. If rollback itself fails, retain that ownership obligation
        # and retry it before allowing another lifecycle operation to proceed.
        self._failed_start_cleanup_pending = False
        self._failed_start_cleanup_error: BaseException | None = None
        # End-of-stream work may finish protocol finalization and then fail
        # while releasing an external resource. Retain that exact cleanup
        # obligation separately so restart can retry cleanup without replaying
        # provider finalization (and potentially duplicating a transcript).
        self._failed_end_cleanup_pending = False
        self._failed_end_cleanup_error: BaseException | None = None

    def set_runtime_scope(self, parent: RuntimeScope, *, name: str) -> None:
        """Attach interruptible provider work to an application lifecycle."""
        if not name:
            raise ValueError("STT RuntimeScope name must be non-empty")
        current = self._runtime_scope
        active_send = self._active_audio_send_task
        if current is not None:
            if current.parent is parent:
                return
            if current.tasks() or (active_send is not None and not active_send.done()):
                raise RuntimeError("Cannot reattach STT runtime work while audio is active")
        self._runtime_scope = parent.create_child(name)
        self._owns_runtime_scope = False

    def _ensure_runtime_scope(self) -> RuntimeScope:
        scope = self._runtime_scope
        if scope is None:
            scope = RuntimeScope(name="stt-provider-runtime")
            self._runtime_scope = scope
            self._owns_runtime_scope = True
        return scope

    async def start_stream(self) -> None:
        """Begin a new STT stream session."""
        async with self._public_lifecycle_operation():
            if self._running:
                raise RuntimeError(
                    "Stream already started; call end_stream() before start_stream()"
                )
            active_send = self._active_audio_send_task
            if active_send is not None:
                if not active_send.done():
                    raise RuntimeError(
                        "Previous STT audio send is still shutting down; cannot start a new stream"
                    )
                self._forget_audio_send_if_done(active_send)
            await self._retry_failed_end_cleanup()
            await self._retry_failed_start_cleanup()
            self._event_queue = asyncio.Queue()
            self._running = True
            try:
                await self._on_start()
            except BaseException as startup_error:
                # Cancellation is a normal startup failure mode (session stop,
                # warmup timeout, task-group shutdown). It must roll back the
                # public lifecycle state and any partially-created provider
                # resources just like an ordinary provider error so the same
                # instance can be retried.
                self._running = False
                self._failed_start_cleanup_pending = True
                try:
                    cancellation = await self._finish_failed_start()
                except BaseException as cleanup_error:
                    self._failed_start_cleanup_error = cleanup_error
                    logger.warning("STT failed-start cleanup failed", exc_info=True)
                    # Keep startup as the primary exception while making the
                    # retained cleanup obligation visible to diagnostics.
                    raise startup_error from cleanup_error
                else:
                    self._clear_failed_start_cleanup()
                if cancellation is not None and not isinstance(
                    startup_error, asyncio.CancelledError
                ):
                    raise cancellation from startup_error
                raise
            self._stream_generation += 1

    async def send_audio(self, chunk: AudioChunk) -> None:
        """Send an audio chunk to the active STT stream."""
        if self._allow_end_during_audio_send:
            stream_generation = self._stream_generation
            async with self._audio_send_lock:
                async with self._lifecycle_lock:
                    if not self._running or stream_generation != self._stream_generation:
                        raise RuntimeError("Stream not started; call start_stream() first")
                    self._validate_audio(chunk)
                    chunk = self._prepare_audio(chunk)
                    if not chunk.data:
                        return
                # Run the provider write in an owned task. end_stream() can
                # cancel that task without cancelling the long-lived audio
                # ingress task that called send_audio().
                scope = self._ensure_runtime_scope()
                send_task = scope.create_task(
                    _AUDIO_SEND_TASK,
                    self._on_audio(chunk),
                    task_name=_AUDIO_SEND_TASK,
                    policy=_STT_RUNTIME_CANCEL_POLICY,
                )
                self._active_audio_send_task = send_task
                current = asyncio.current_task()
                cancellation_requests = current.cancelling() if current is not None else 0
                try:
                    await asyncio.shield(send_task)
                except asyncio.CancelledError:
                    if current is not None and current.cancelling() > cancellation_requests:
                        send_task.cancel()
                        await scope.drain(_AUDIO_SEND_TASK, cancel=True)
                        raise
                    if self._running and stream_generation == self._stream_generation:
                        # Provider-side cancellation unrelated to lifecycle
                        # teardown remains observable to the caller.
                        raise
                    # end_stream() cancelled the owned provider write. Treat
                    # that as an accepted lifecycle cutoff, not cancellation
                    # of the caller's ingress loop.
                finally:
                    self._forget_audio_send_if_done(send_task)
            return

        async with self._lifecycle_lock:
            if not self._running:
                raise RuntimeError("Stream not started; call start_stream() first")
            self._validate_audio(chunk)
            chunk = self._prepare_audio(chunk)
            if not chunk.data:
                return
            await self._on_audio(chunk)

    async def commit_segment(self) -> bool:
        """Finalize the current segment without closing the stream.

        Returns ``True`` when the provider accepted the segment commit request.
        The default implementation returns ``False`` for providers that only
        support whole-stream finalization.
        """
        if self._allow_end_during_audio_send:
            stream_generation = self._stream_generation
            # Segment-control writes share the provider socket with audio.
            # Preserve wire order so a finalize message cannot overtake an
            # append that is still suspended under transport backpressure.
            async with self._audio_send_lock, self._lifecycle_lock:
                if not self._running or stream_generation != self._stream_generation:
                    return False
                return await self._on_commit_segment()
        async with self._lifecycle_lock:
            if not self._running:
                return False
            return await self._on_commit_segment()

    async def end_stream(self) -> None:
        """Signal that no more audio will be sent for the current stream."""
        async with self._public_lifecycle_operation():
            if not self._running:
                await self._reap_retained_audio_send()
                await self._retry_failed_end_cleanup()
                await self._retry_failed_start_cleanup()
                return
            self._running = False
            active_send = self._active_audio_send_task
            if active_send is not None and not active_send.done():
                active_send.cancel()
            try:
                if active_send is not None:
                    try:
                        # Do not finalize or close provider state until the
                        # write has actually stopped. Shielding lets an outer
                        # STT timeout still cancel end_stream() promptly if a
                        # broken provider ignores cancellation.
                        current = asyncio.current_task()
                        cancellation_requests = current.cancelling() if current is not None else 0
                        await asyncio.shield(active_send)
                    except asyncio.CancelledError:
                        if current is not None and current.cancelling() > cancellation_requests:
                            raise
                        # Expected: end_stream() cancelled the owned send.
                    except Exception:
                        logger.debug(
                            "STT audio send failed while ending stream",
                            exc_info=True,
                        )
                    finally:
                        self._forget_audio_send_if_done(active_send)
                await self._on_end()
            except BaseException as end_error:
                # A later close()/start_stream() retries only the cleanup hook,
                # never _on_end(), because provider finalization may already
                # have committed audio or emitted a final transcript.
                self._failed_end_cleanup_pending = True
                self._failed_end_cleanup_error = end_error
                raise
            finally:
                await self._event_queue.put(None)

    async def _reap_retained_audio_send(self) -> None:
        """Finish an audio write retained by an interrupted ``end_stream``."""
        active_send = self._active_audio_send_task
        if active_send is None:
            return
        if active_send is asyncio.current_task():
            raise RuntimeError("STT audio send cannot reap itself during cleanup")
        if not active_send.done():
            active_send.cancel()
        current = asyncio.current_task()
        cancellation_requests = current.cancelling() if current is not None else 0
        try:
            await asyncio.shield(active_send)
        except asyncio.CancelledError:
            if current is not None and current.cancelling() > cancellation_requests:
                # Keep the task handle and failed-end ledger intact. A later
                # close() retry resumes ownership instead of false-succeeding.
                raise
            # The owned provider write accepted cancellation.
        except Exception:
            logger.debug(
                "STT retained audio send failed while retrying cleanup",
                exc_info=True,
            )
        self._forget_audio_send_if_done(active_send)

    def _forget_audio_send_if_done(self, task: asyncio.Task[None]) -> None:
        """Release a settled audio-send handle from both provider owners."""
        if not task.done():
            return
        if self._runtime_scope is not None:
            self._runtime_scope.discard(task)
        if self._active_audio_send_task is task:
            self._active_audio_send_task = None

    async def events(self) -> AsyncIterator[STTEvent]:
        """Return an async iterator of provider-scoped STT events."""
        while True:
            event = await self._event_queue.get()
            if event is None:
                break
            yield event

    async def close(self) -> None:
        """End an active stream, then drain provider-scoped error emissions."""
        try:
            await self.end_stream()
        finally:
            try:
                await self._drain_provider_error_tasks()
            finally:
                await self._close_owned_runtime_scope_if_idle()

    async def _close_owned_runtime_scope_if_idle(self) -> None:
        scope = self._runtime_scope
        if not self._owns_runtime_scope or scope is None or not scope.empty:
            return
        await scope.close()
        if self._runtime_scope is scope:
            self._runtime_scope = None
            self._owns_runtime_scope = False

    # -- Protected helpers for subclasses ----------------------------------

    @asynccontextmanager
    async def _public_lifecycle_operation(self) -> AsyncIterator[None]:
        """Serialize lifecycle work, then join provider Error publication.

        Provider resource rollback and retained-cleanup ledgers must settle
        while the lifecycle lock is held. Error subscribers cannot be joined
        there, though: a subscriber may initiate session teardown and re-enter
        :meth:`close`. Always release the lock before draining those tasks.
        """
        try:
            async with self._lifecycle_lock:
                yield
        finally:
            await self._drain_provider_error_tasks()

    async def _drain_provider_error_tasks(self) -> None:
        """No-op hook overridden by the provider error-emitter mixin."""

    def _emit_event(self, event: STTEvent) -> None:
        """Enqueue an STTEvent for consumers of ``events()``."""
        self._event_queue.put_nowait(event)

    @staticmethod
    def _latch_uniform_format(
        current: AudioFormat | None, chunk: AudioChunk, *, provider_label: str
    ) -> AudioFormat:
        """Latch the first-seen format and reject a mid-stream change.

        Batch STT providers wrap the whole buffered utterance in a single WAV
        header built from the first-seen format, so a mid-stream rate/channel
        change would be silently mislabeled (garbled / wrong-pitch transcript).
        The first chunk latches the format; a later mismatch raises
        ``ValueError`` rather than corrupting the transcript. Bundled
        transports resample inbound audio to a fixed pipeline rate before STT,
        so this only guards custom transports.
        """
        if current is None:
            return chunk.format
        if chunk.format != current:
            raise ValueError(
                f"{provider_label} received a mid-stream audio format change "
                f"({current} -> {chunk.format}); the batch path requires a "
                "uniform format for the whole utterance"
            )
        return current

    @staticmethod
    async def _run_with_bounded_retry(
        attempt: Callable[[], Awaitable[T]],
        *,
        max_retries: int,
        provider_label: str,
    ) -> T:
        """Run ``attempt`` with bounded retries and exponential backoff.

        ``max_retries`` is the total attempt count; it is clamped to at least
        one so a misconfigured ``max_retries=0`` still sends a single request
        rather than raising a causeless "no attempts" error. Only HTTP 429 is
        retried among ``HTTPStatusError``; ``TransportError``/``TimeoutException``
        are always retried until the attempts are exhausted. Backoff is
        ``2**i`` seconds between attempts.
        """
        total_attempts = max(1, max_retries)
        last_exc: Exception | None = None
        for i in range(total_attempts):
            try:
                return await attempt()
            except httpx.HTTPStatusError as exc:
                last_exc = exc
                if exc.response.status_code == 429 and i < total_attempts - 1:
                    await asyncio.sleep(2**i)
                    continue
                raise
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                if i < total_attempts - 1:
                    logger.warning(
                        "%s request failed (attempt %d/%d): %s",
                        provider_label,
                        i + 1,
                        total_attempts,
                        exc,
                    )
                    await asyncio.sleep(2**i)
                    continue
                raise

        # The loop always runs at least once (total_attempts >= 1), so reaching
        # here means every attempt failed without re-raising; last_exc is set.
        raise RuntimeError(
            f"{provider_label}: all {total_attempts} transcription attempt(s) failed"
        ) from last_exc

    @staticmethod
    def _validate_positive_limit(name: str, value: float | None) -> None:
        if value is None:
            return
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or (isinstance(value, float) and not math.isfinite(value))
            or value <= 0
        ):
            raise ValueError(f"{name} must be a positive finite number when set (got {value!r})")

    @staticmethod
    def _extend_limited_audio_buffer(
        buffer: bytearray,
        chunk: AudioChunk,
        *,
        max_chunk_bytes: int | None,
        max_buffer_bytes: int | None,
        max_duration_ms: float | None,
        provider_label: str,
    ) -> None:
        """Append ``chunk`` to ``buffer`` after enforcing batch-audio caps.

        A single chunk that is itself larger than ``max_chunk_bytes`` raises
        ``ValueError`` — that is a malformed/oversized frame, not a normal
        accumulation, so it should surface as a hard validation error. The
        *cumulative* caps (total buffered bytes, total buffered duration)
        raise :class:`AudioBufferLimitExceeded` instead, because a valid
        long-talking caller can reach them legitimately; providers catch that
        to finalize the utterance gracefully rather than tearing down the call.
        """
        chunk_bytes = len(chunk.data)
        if max_chunk_bytes is not None and chunk_bytes > max_chunk_bytes:
            raise ValueError(
                f"{provider_label} audio chunk exceeds the configured limit "
                f"({chunk_bytes} > {max_chunk_bytes} bytes)"
            )

        buffered_bytes = len(buffer) + chunk_bytes
        if max_buffer_bytes is not None and buffered_bytes > max_buffer_bytes:
            raise AudioBufferLimitExceeded(
                f"{provider_label} buffered audio exceeds the configured limit "
                f"({buffered_bytes} > {max_buffer_bytes} bytes)"
            )

        if max_duration_ms is not None:
            bytes_per_second = chunk.format.bytes_per_second
            if bytes_per_second <= 0:
                raise ValueError(
                    f"{provider_label} cannot enforce a duration cap on audio with "
                    f"non-positive byte rate ({bytes_per_second} bytes/s); check the "
                    "chunk's sample_rate/channels/sample_width"
                )
            buffered_duration_ms = (buffered_bytes / bytes_per_second) * 1000
            if buffered_duration_ms > max_duration_ms:
                raise AudioBufferLimitExceeded(
                    f"{provider_label} buffered audio duration exceeds the configured limit "
                    f"({buffered_duration_ms:.0f} > {max_duration_ms:.0f} ms)"
                )

        buffer.extend(chunk.data)

    async def _buffer_batch_audio_or_finalize(
        self,
        buffer: bytearray,
        chunk: AudioChunk,
        *,
        max_chunk_bytes: int | None,
        max_buffer_bytes: int | None,
        max_duration_ms: float | None,
        provider_label: str,
        finalize: Callable[[], Awaitable[None]],
    ) -> None:
        """Buffer ``chunk`` for a batch utterance, finalizing on a cumulative cap.

        Wraps :meth:`_extend_limited_audio_buffer`. On a cumulative-cap hit
        (:class:`AudioBufferLimitExceeded`) the already-buffered audio is
        flushed through ``finalize`` (which transcribes + emits + clears the
        buffer), and the new ``chunk`` then starts a fresh buffer. This keeps
        a long-talking caller's live call running: the current utterance is
        finalized early instead of an error tearing down the pipeline.

        A genuinely oversized *single* chunk still raises ``ValueError`` (it is
        re-raised, not swallowed) because retrying it is futile; and if the new
        chunk alone would re-trip a cumulative cap on the now-empty buffer it is
        dropped (with a warning) rather than looping forever.
        """
        try:
            self._extend_limited_audio_buffer(
                buffer,
                chunk,
                max_chunk_bytes=max_chunk_bytes,
                max_buffer_bytes=max_buffer_bytes,
                max_duration_ms=max_duration_ms,
                provider_label=provider_label,
            )
            return
        except AudioBufferLimitExceeded as exc:
            logger.info(
                "%s reached its batch buffer cap (%s); finalizing the current "
                "utterance and starting a fresh stream",
                provider_label,
                exc,
            )

        # Flush whatever is buffered so the caller's speech so far is not lost.
        await finalize()

        # Start a fresh utterance with the chunk that tripped the cap. If that
        # chunk on its own still exceeds a cumulative cap, drop it instead of
        # looping (the buffer is already empty, so finalizing again is a no-op).
        try:
            self._extend_limited_audio_buffer(
                buffer,
                chunk,
                max_chunk_bytes=max_chunk_bytes,
                max_buffer_bytes=max_buffer_bytes,
                max_duration_ms=max_duration_ms,
                provider_label=provider_label,
            )
        except AudioBufferLimitExceeded as exc:
            logger.warning(
                "%s dropping a single chunk that exceeds the batch buffer cap "
                "on an empty buffer (%s)",
                provider_label,
                exc,
            )

    def _drain_buffer_to_wav(self) -> bytes | None:
        """Wrap the buffered batch PCM into WAV bytes and clear the buffer.

        Shared prologue for the batch ``finalize`` hooks. Returns ``None`` when
        there is nothing to transcribe (empty buffer or no latched format);
        otherwise returns the buffered PCM as a single WAV blob and clears the
        buffer in place. Clearing in place (not a rebind) keeps the buffer
        reference held by the in-progress ``_buffer_batch_audio_or_finalize``
        call the same object, letting the chunk that tripped the cap restart a
        fresh stream. The latched ``_audio_format`` is preserved so the next
        utterance keeps the same first-seen format contract.
        """
        if not self._buffer or self._audio_format is None:
            return None
        wav_data = _pcm_to_wav(bytes(self._buffer), self._audio_format)
        self._buffer.clear()
        return wav_data

    def _validate_audio(self, chunk: AudioChunk) -> None:
        if chunk.format.encoding != "pcm":
            raise ValueError(f"Expected PCM encoding, got '{chunk.format.encoding}'")
        if chunk.format.sample_width != 2:
            raise ValueError(f"Expected 16-bit PCM (sample_width=2), got {chunk.format.sample_width}")
        if chunk.format.channels <= 0:
            raise ValueError(f"Expected positive channel count, got {chunk.format.channels}")
        if (
            self._expected_sample_rate is not None
            and chunk.format.sample_rate != self._expected_sample_rate
        ):
            raise ValueError(
                f"Expected sample rate {self._expected_sample_rate}, "
                f"got {chunk.format.sample_rate}"
            )

    def _prepare_audio(self, chunk: AudioChunk) -> AudioChunk:
        """Normalize a validated chunk before passing it to the provider hook."""
        return chunk

    # -- Hooks for subclasses to override ----------------------------------

    async def _on_start(self) -> None:
        """Called when a new stream starts. Override in subclass."""

    async def _finish_failed_start(self) -> asyncio.CancelledError | None:
        """Run failed-start cleanup to completion despite repeated cancellation."""
        settlement = await shielded_cleanup(self._on_start_failed)
        if settlement.error is not None:
            raise settlement.error
        if settlement.cancellation_requests:
            return asyncio.CancelledError()
        return None

    async def _retry_failed_start_cleanup(self) -> None:
        """Finish a retained partial-start rollback before reusing the provider."""
        if not self._failed_start_cleanup_pending:
            return
        try:
            cancellation = await self._finish_failed_start()
        except BaseException as cleanup_error:
            self._failed_start_cleanup_error = cleanup_error
            raise RuntimeError(
                "Previous STT failed-start cleanup is incomplete; "
                "retry close() or start_stream() after cleanup recovers"
            ) from cleanup_error
        self._clear_failed_start_cleanup()
        if cancellation is not None:
            raise cancellation

    def _clear_failed_start_cleanup(self) -> None:
        self._failed_start_cleanup_pending = False
        self._failed_start_cleanup_error = None

    async def _finish_failed_end_cleanup(self) -> asyncio.CancelledError | None:
        """Run retained end cleanup to completion despite repeated cancellation."""
        settlement = await shielded_cleanup(self._on_end_cleanup)
        if settlement.error is not None:
            raise settlement.error
        if settlement.cancellation_requests:
            return asyncio.CancelledError()
        return None

    async def _retry_failed_end_cleanup(self) -> None:
        """Release retained end resources before replacing provider state."""
        if not self._failed_end_cleanup_pending:
            return
        try:
            cancellation = await self._finish_failed_end_cleanup()
        except BaseException as cleanup_error:
            self._failed_end_cleanup_error = cleanup_error
            raise RuntimeError(
                "Previous STT end cleanup is incomplete; "
                "retry close() or start_stream() after cleanup recovers"
            ) from cleanup_error
        self._failed_end_cleanup_pending = False
        self._failed_end_cleanup_error = None
        if cancellation is not None:
            raise cancellation

    async def _on_start_failed(self) -> None:
        """Roll back resources allocated by a partial ``_on_start``."""
        await self._on_end()

    async def _on_end_cleanup(self) -> None:
        """Release resources after a failed ``_on_end`` without finalizing again."""

    async def _on_audio(self, chunk: AudioChunk) -> None:
        """Called for each audio chunk. Override in subclass."""

    async def _on_commit_segment(self) -> bool:
        """Finalize the current segment without closing the stream."""
        return False

    async def _on_end(self) -> None:
        """Called when the stream ends. Override in subclass."""

    # -- Provider metadata ----------------------------------------------------

    def version_info(self) -> dict[str, str]:
        """Return stable-shape dict identifying this provider.

        Keys: ``provider``, ``model``, ``api_version``, ``sdk_version``.
        Unknown fields are ``"unknown"`` rather than omitted.
        """
        return {
            "provider": "unknown",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": "unknown",
        }
