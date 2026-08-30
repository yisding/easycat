"""Owns transport ingress and outbound audio drain for a Session.

Responsibilities:

- **Ingress.** The transport -> audio-stage -> vad-stage -> stt-stage
  receive loop. Handles auto-turn speech-energy detection (the
  "start a turn from raw audio" path used when VAD is off).
- **Outbound.** Drains the outbound queue to ``transport.send_audio``,
  stamps each chunk with the current turn's byte counters, emits
  playback marks at fixed byte intervals, and observes playback acks
  from transports that report them.
- **Gated replay.** Replays buffered audio events through the
  pipeline after a gated transport unblocks.

The router holds the single outbound queue, the playback-mark
accounting (``bytes_interval``, ``seq``, ``mark_to_bytes``), and the
auto-turn speech-frame counter.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

from easycat import _observability as observability
from easycat._bounded_queue import BoundedAudioQueue
from easycat._concurrency import SurvivorCapacityError
from easycat._env import is_truthy
from easycat._log_context import bind_turn
from easycat.audio_format import AudioChunk
from easycat.events import (
    AudioIn,
    AudioOut,
    Error,
    ErrorStage,
    EventBus,
    PlaybackMarkAck,
    TransportAudioDelivered,
    TransportDegraded,
    VADStopSpeaking,
)
from easycat.providers import Transport
from easycat.runtime.capabilities import (
    PlaybackAcknowledgements,
    drain_aec_reference_frames,
    pending_playout_ms,
    playback_acknowledgements,
    transport_reports_audio_delivery,
)
from easycat.runtime.context import RunContext
from easycat.runtime.scope import (
    RuntimeMemberPolicy,
    RuntimeScope,
    RuntimeTaskAction,
    RuntimeTaskPolicy,
)
from easycat.session._journal_sink import SessionJournalSink
from easycat.session.text import _chunk_has_speech_energy
from easycat.stages.audio import AudioStage
from easycat.stages.base import audio_capture_allowed
from easycat.stages.stt import STTStage
from easycat.stages.transport import TransportStage
from easycat.stages.vad import VADStage
from easycat.teardown_budgets import (
    SESSION_AUDIO_DRAIN_TIMEOUT_S as _AUDIO_DRAIN_TIMEOUT_S,
)
from easycat.teardown_budgets import (
    SESSION_AUDIO_PLAYOUT_MARGIN_S as _AUDIO_PLAYOUT_MARGIN_S,
)
from easycat.teardown_budgets import (
    SESSION_INLINE_SEND_CANCEL_GRACE_TIMEOUT_S,
    SESSION_INLINE_SEND_TIMEOUT_S,
)
from easycat.turn_manager import TurnManager, TurnManagerState

if TYPE_CHECKING:
    from easycat._turn_context import TurnContext
    from easycat.session._wiring import SessionWiringContext

logger = logging.getLogger(__name__)

# AEC reference capture is decimated to roughly one journaled frame per second
# even when opted in, so a debug session keeps the three-track alignment the
# debugger needs without the ~50 writes/sec/session fsync + journal pressure of
# capturing every outbound frame. At 16 kHz / 16-bit / 20 ms frames the
# pipeline produces ~50 frames/sec, so 1-in-50 lands close to 1 Hz; the exact
# rate is approximate because real frames vary in size.
_AEC_REFERENCE_CAPTURE_EVERY_N_FRAMES = 50


def _aec_reference_env_override() -> bool | None:
    """Resolve the ``EASYCAT_CAPTURE_AEC_REFERENCE`` env override.

    Returns ``True``/``False`` when the env var is set to an explicit truthy or
    falsy value, or ``None`` when it is unset so the config-derived default
    wins. The env var lets a developer flip per-frame AEC reference capture on
    (or force it off) without touching code or config — mirroring the
    ``EASYCAT_DEBUGGER_AUTOLAUNCH`` escape hatch.
    """
    raw = os.getenv("EASYCAT_CAPTURE_AEC_REFERENCE")
    if raw is None:
        return None
    return is_truthy(raw)


class _PipelineTornDown(Exception):
    """Sentinel raised by the per-chunk handler once consecutive failures hit
    the fatal threshold.

    The per-chunk handler has *already* emitted the terminating
    :class:`Error` for the offending exception, so this sentinel exists only
    to break out of the receive loop and trigger teardown.  The outer
    ``except`` recognizes it and suppresses the second emit, so the fatal
    frame surfaces exactly one ``Error`` like every other frame.
    """


class AudioRouter:
    """Routes audio between the transport and the pipeline stages.

    Owns the receive loop (transport -> audio/vad/stt stages) and the
    outbound drain loop (queue -> transport.send_audio).  Owns the
    playback-mark accounting, the auto-turn speech-energy counter, and
    the gated-replay book-keeping used when the classification gate
    flushes buffered TTS audio.

    Consecutive per-chunk pipeline errors above
    :attr:`_MAX_CONSECUTIVE_CHUNK_ERRORS` are treated as a genuinely
    broken pipeline and tear the session down; below that threshold a
    single bad frame is logged, surfaced as an :class:`Error`, and
    skipped so one transient backend hiccup never drops a live call.

    Outbound-queue ownership: the single :class:`BoundedAudioQueue`
    lives on :class:`Session`; the router and the
    :class:`TTSSynthesizer` both hold the same reference (drain side
    and write side).  ``Session.start`` is the only place it is
    rebuilt (after a prior teardown) and pushes the new instance to
    both via ``replace_outbound_queue``.  Transport reconnect does
    *not* affect this: e.g. the WebRTC transport resets only its own
    transport-internal outbound source on reconnect — the router
    interacts with the transport solely via ``send_audio`` and never
    holds that internal queue.
    """

    # Number of *consecutive* per-chunk pipeline failures tolerated before
    # the loop gives up and lets the session tear down.  A single bad
    # frame (malformed audio, momentary ONNX/Krisp/VAD glitch, one STT
    # send failure) must not drop the call; a sustained run of failures
    # signals a genuinely broken backend.
    _MAX_CONSECUTIVE_CHUNK_ERRORS = 10
    _INGRESS_TASK_NAME = "audio_ingress_pipeline"
    _OUTBOUND_TASK_NAME = "audio_outbound_drain"
    _INLINE_SEND_TASK_NAME = "audio_inline_send"
    _AEC_DEGRADED_EMIT_TASK_NAME = "aec_reference_degraded_emit"
    # The first-frame fast path preserves an in-progress transport write across
    # caller cancellation so a frame is never half-submitted. Keep that shield
    # bounded: a half-open transport must not make barge-in or force-stop
    # permanently uncancellable.
    _INLINE_SEND_TIMEOUT_S = SESSION_INLINE_SEND_TIMEOUT_S
    _INLINE_SEND_CANCEL_GRACE_S = SESSION_INLINE_SEND_CANCEL_GRACE_TIMEOUT_S

    def __init__(
        self,
        *,
        wiring: SessionWiringContext,
        transport: Transport,
        audio_stage: AudioStage,
        vad_stage: VADStage,
        stt_stage: STTStage,
        transport_stage: TransportStage,
        turn_manager: TurnManager,
        event_bus: EventBus,
        journal_sink: SessionJournalSink,
        runtime_scope: RuntimeScope,
        run_ctx: RunContext,
        no_turn: TurnContext,
        echo_canceller: Any,
        # Outbound queue is constructed by the builder; the router receives
        # the same instance so external supplies and the TTSSynthesizer
        # keep their references valid.
        outbound_queue: BoundedAudioQueue,
        capture_aec_reference: bool = False,
    ) -> None:
        self._transport = transport
        self._transport_send_audio_is_nonblocking = bool(
            getattr(transport, "send_audio_is_nonblocking", False)
        )
        self._audio_stage = audio_stage
        self._vad_stage = vad_stage
        self._stt_stage = stt_stage
        self._transport_stage = transport_stage
        self._turn_manager = turn_manager
        self._event_bus = event_bus
        self._journal_sink = journal_sink
        self._runtime_scope = runtime_scope
        self._inline_send_scope = runtime_scope.create_child("audio-router-inline-send")
        self._inline_send_policy = RuntimeTaskPolicy(
            graceful=RuntimeMemberPolicy(
                cohort="audio-inline-send",
                signal_token=False,
                task_action=RuntimeTaskAction.FINISH,
            ),
            force=RuntimeMemberPolicy(
                cohort="audio-inline-send",
                signal_token=False,
                task_action=RuntimeTaskAction.CANCEL,
                hard_deadline=(self._INLINE_SEND_TIMEOUT_S + 2 * self._INLINE_SEND_CANCEL_GRACE_S),
            ),
        )
        self._run_ctx = run_ctx
        self._no_turn = no_turn
        self._echo_canceller = echo_canceller
        # Latches once the AEC reference feed has raised (e.g. a near/far
        # sample-rate mismatch) so we log the actionable cause exactly once
        # and stop re-attempting a feed that will keep failing for the rest
        # of the session, rather than spamming the log per outbound chunk.
        self._aec_reference_failed: bool = False
        # Latches the observability warning for transports that can only feed
        # AEC at socket-write time. Explicit server-side AEC remains supported
        # for these transports, but operators need one durable signal that the
        # reference is not tied to the remote playout clock.
        self._aec_reference_degraded_reported: bool = False

        # Whether the transport exposes ``drain_aec_reference_frames()`` — a
        # thread-safe queue populated by the output callback at actual playback
        # time.  When True, _process_chunk drains and feeds reference before
        # AudioStage.execute() so AEC3 always sees the far-end signal before the
        # near-end mic frame for the same time window.  _handle_audio_delivery
        # skips feed_reference for these transports to avoid double-feeding.
        self._transport_has_aec_drain: bool = callable(
            getattr(transport, "drain_aec_reference_frames", None)
        )

        # Per-frame AEC reference *journaling* is strictly opt-in (config knob
        # ``capture_aec_reference`` or the
        # ``EASYCAT_CAPTURE_AEC_REFERENCE`` env override). ``debug="full"`` keeps
        # a durable journal but must NOT add ~50 artifact writes/sec/session of
        # fsync + journal pressure to the live audio loop on its own. Feeding
        # the reference into the canceller (which makes AEC work) is unaffected;
        # only the optional debugger-track journaling is gated here.
        env_override = _aec_reference_env_override()
        self._capture_aec_reference: bool = (
            env_override if env_override is not None else bool(capture_aec_reference)
        )
        # Frame counter used to decimate journaled reference frames to ~1/sec
        # even when capture is enabled.
        self._aec_reference_frame_index: int = 0

        # Session-derived late-bound accessors.  The loop body reads live
        # values even when Session mutates the enable_* knobs / turn
        # pointer after construction.
        self._enable_noise_reduction = wiring.enable_noise_reduction
        self._enable_aec = wiring.enable_aec
        self._enable_vad = wiring.enable_vad
        self._auto_turn_from_stt_final = wiring.auto_turn_from_stt_final

        self._emit = wiring.emit
        self._is_running = wiring.is_running
        self._set_running = wiring.set_running
        self._current_turn = wiring.current_turn
        self._correlation_ids = wiring.correlation_ids
        self._is_stt_active = wiring.is_stt_active
        self._with_correlation = wiring.with_correlation

        # Auto-turn speech-energy detector state
        self._auto_turn_speech_frames: int = 0

        # Gated replay
        self._replay_chunks_pending: int = 0

        # Outbound send-failure streak.  A transient ``send_audio`` failure
        # is expected to be swallowed (a turn must still complete after one
        # bad send — see test_failure_paths), so we surface a single
        # bus-level ``Error`` at the *start* of a failure streak rather than
        # one per dropped chunk.  Reset to 0 after any successful send so a
        # later failure surfaces a fresh ``Error``.
        self._outbound_send_failures: int = 0

        # Playback mark accounting
        self._playback_mark_bytes_interval: int = 4_000  # ~125ms at 16kHz/16-bit
        self._playback_mark_seq: int = 0  # session-scoped: never collide across turns
        self._playback_ack_transport: PlaybackAcknowledgements | None = playback_acknowledgements(
            transport
        )
        self._transport_reports_audio_delivery = transport_reports_audio_delivery(transport)

        # Outbound queue (single instance shared with TTS synthesizer)
        self._outbound_queue = outbound_queue

        # Tasks
        self._outbound_task: asyncio.Task[None] | None = None
        self._pipeline_task: asyncio.Task[None] | None = None

        # Outbound drain progress tracking.  ``_outbound_in_flight`` counts
        # chunks that have been dequeued but whose ``transport.send_audio``
        # has not yet returned.  ``_outbound_idle`` is set whenever the
        # queue is empty *and* no send is in flight, so ``await_drain`` can
        # wait on a real event instead of busy-polling, and never returns
        # while the final chunk is still inside the transport.
        self._outbound_in_flight: int = 0
        self._outbound_idle: asyncio.Event = asyncio.Event()
        self._outbound_idle.set()
        self._outbound_send_lock = asyncio.Lock()

    def _update_outbound_idle(self) -> None:
        """Set/clear the idle event based on queue depth and in-flight sends."""
        if self._outbound_in_flight == 0 and self._outbound_queue.empty():
            self._outbound_idle.set()
        else:
            self._outbound_idle.clear()

    # ── Public API ──────────────────────────────────────────────

    @property
    def outbound_queue(self) -> BoundedAudioQueue:
        return self._outbound_queue

    def replace_outbound_queue(self, queue: BoundedAudioQueue) -> None:
        """Swap the outbound queue (used by Session.start when re-creating it)."""
        self._outbound_queue = queue

    @property
    def pipeline_task(self) -> asyncio.Task[None] | None:
        return self._pipeline_task

    @property
    def outbound_task(self) -> asyncio.Task[None] | None:
        return self._outbound_task

    def start_ingress(self) -> asyncio.Task[None]:
        """Start the transport receive loop."""
        self._pipeline_task = self._runtime_scope.create_journaled_task(
            self._run_pipeline(),
            name=self._INGRESS_TASK_NAME,
            journal_sink=self._journal_sink,
        )
        self._pipeline_task.add_done_callback(self._runtime_scope.log_task_exception)
        return self._pipeline_task

    def start_outbound(self) -> asyncio.Task[None]:
        """Start the outbound audio drain task."""
        self._outbound_task = self._runtime_scope.create_journaled_task(
            self._drain_outbound_audio(),
            name=self._OUTBOUND_TASK_NAME,
            journal_sink=self._journal_sink,
        )
        self._outbound_task.add_done_callback(self._runtime_scope.log_task_exception)
        return self._outbound_task

    async def stop_ingress(self) -> None:
        """Cancel the ingress task and wait for it to exit."""
        task = self._pipeline_task
        current = asyncio.current_task()
        if current is not None and task is current:
            self._runtime_scope.discard(task)
        else:
            await self._runtime_scope.cancel_and_drain(self._INGRESS_TASK_NAME)
        self._pipeline_task = None

    async def stop_outbound(self, *, force: bool = False) -> None:
        """Cancel the outbound drain task and wait for it to exit."""
        task = self._outbound_task
        current = asyncio.current_task()
        if current is not None and task is current:
            self._runtime_scope.discard(task)
        else:
            await self._runtime_scope.cancel_and_drain(self._OUTBOUND_TASK_NAME)
        await self._runtime_scope.cancel_and_drain(self._AEC_DEGRADED_EMIT_TASK_NAME)
        # Graceful shutdown joins the transport write before reporting idle.
        # Force shutdown uses the owned task's hard deadline and leaves a
        # cancellation-resistant write parked; never re-await that survivor
        # through the raw, unbounded drain API after its cohort has parked it.
        if force:
            registry = self._inline_send_scope.survivor_registry
            parked = bool(
                registry is not None and registry.survivors(self._inline_send_scope.owner_id)
            )
            if not parked:
                await self._inline_send_scope.drain_cohort(
                    "audio-inline-send",
                    force=True,
                )
        else:
            await self._inline_send_scope.cancel_and_drain()
        self._outbound_task = None

    async def await_drain(self, timeout: float = _AUDIO_DRAIN_TIMEOUT_S) -> None:
        """Wait for outbound audio to fully drain, with a timeout.

        "Drained" means the outbound queue is empty *and* no chunk is
        still in flight inside ``transport.send_audio`` — otherwise turn
        cleanup could clear the turn pointer and emit
        ``bot_stopped_speaking`` while the final chunk is still being
        delivered, truncating the tail of the bot's last utterance.

        The wait is event-driven (``_outbound_idle``) rather than a
        busy-poll on ``sleep(0)``, so a backpressured/slow transport does
        not spin the event loop and compete with the drain task for loop
        time.  If the transport's ``send_audio`` stays blocked (network
        backpressure, stalled connection) the bounded ``timeout`` prevents
        turn cleanup from hanging indefinitely.

        Transports with a local speaker buffer (duck-typed
        ``pending_playout_ms``) are additionally waited on so the queue
        going idle does not report drained while playout is still in
        progress.  Because the local output queue can buffer far more than
        ``timeout`` seconds of audio, the playout wait uses a deadline
        derived from the transport's queued ``pending_playout_ms`` (plus a
        small margin) instead of the fixed ``timeout``, so teardown waits
        for *actual* speaker playout rather than truncating the tail.  The
        local output queue is bounded, so this deadline stays finite.
        Strict no-op for transports lacking the hook.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        if self._outbound_task and not self._outbound_task.done():  # noqa: SIM102 nested branches preserve decision context
            if self._outbound_in_flight != 0 or not self._outbound_queue.empty():
                self._update_outbound_idle()
                try:
                    await asyncio.wait_for(self._outbound_idle.wait(), timeout=timeout)
                except TimeoutError:
                    logger.warning("Outbound queue drain timed out after %.1fs", timeout)
                    return
        # The queue is drained, but a local speaker buffer may still hold
        # seconds of audio.  Extend the playout deadline to cover the
        # transport's reported queued playout (with a ~0.5s margin) so the
        # tail is not cut off; keep at least the original ``timeout``.
        playout_deadline = deadline
        remaining_ms = pending_playout_ms(self._transport)
        if remaining_ms is not None and remaining_ms > 0:
            playout_deadline = max(
                deadline,
                loop.time() + remaining_ms / 1000.0 + _AUDIO_PLAYOUT_MARGIN_S,
            )
        await self._await_playout_drain(playout_deadline)

    async def _await_playout_drain(self, deadline: float) -> None:
        """Wait until the transport's local playout buffer empties or time runs out.

        No-op for transports that do not expose ``pending_playout_ms``.
        """
        loop = asyncio.get_running_loop()
        while True:
            remaining = pending_playout_ms(self._transport)
            if remaining is None or remaining <= 0:
                return
            if loop.time() >= deadline:
                logger.warning("Transport playout drain timed out")
                return
            await asyncio.sleep(0.01)

    async def queue_outbound(self, chunk: AudioChunk) -> None:
        """Enqueue a TTS chunk for the outbound drain loop."""
        await self._outbound_queue.put(chunk)

    async def try_send_first_audio_inline(self, chunk: AudioChunk) -> bool:
        """Send an uncontended first TTS frame without a queue/task handoff."""
        outbound_task = self._outbound_task
        if (
            not self._is_running()
            or outbound_task is None
            or outbound_task.done()
            or self._outbound_in_flight != 0
            or self._outbound_send_lock.locked()
            or not self._outbound_queue.empty()
        ):
            return False
        if self._transport_send_audio_is_nonblocking and self._transport_reports_audio_delivery:
            self._claim_outbound_send()
            try:
                async with self._outbound_send_lock:
                    if not self._can_send_first_audio_inline(outbound_task):
                        return False
                    await self._send_outbound_chunk(chunk, self._current_turn())
                return True
            finally:
                await self._finish_outbound_send(replayed_chunk=False)

        self._claim_outbound_send()
        turn = self._current_turn()
        ownership_started = asyncio.Event()
        try:
            send_task = await self._inline_send_scope.start_owned_task(
                self._INLINE_SEND_TASK_NAME,
                lambda: self._send_first_audio_inline_owned(
                    chunk,
                    outbound_task,
                    turn,
                    ownership_started=ownership_started,
                ),
                policy=self._inline_send_policy,
            )
        except SurvivorCapacityError:
            await self._finish_outbound_send(replayed_chunk=False)
            return False
        except BaseException:
            # Reservation is asynchronous. If cancellation or quota rejection
            # wins before the child starts, the caller still owns the in-flight
            # claim; once the child starts, its ``finally`` owns that release.
            # Avoid double-finish for parked survivors (gh 1040): if ownership not
            # yet started but a task was adopted by the scope, that task will finish.
            if not ownership_started.is_set():
                # Heuristic: if start_owned_task succeeded and returned a task, assume
                # scope adopted it and will handle finish. Only undo claim if no task.
                task_exists = "send_task" in locals() and locals()["send_task"] is not None
                if not task_exists:
                    await self._finish_outbound_send(replayed_chunk=False)
            raise
        return await self._await_non_cancellable_send(
            send_task,
            timeout=self._INLINE_SEND_TIMEOUT_S,
        )

    def _can_send_first_audio_inline(self, outbound_task: asyncio.Task[None]) -> bool:
        """Recheck first-frame eligibility after acquiring the send lock."""
        return (
            self._is_running()
            and self._outbound_task is outbound_task
            and not outbound_task.done()
            and self._outbound_queue.empty()
        )

    async def _send_first_audio_inline_owned(
        self,
        chunk: AudioChunk,
        outbound_task: asyncio.Task[None],
        turn: TurnContext | None,
        *,
        ownership_started: asyncio.Event,
    ) -> bool:
        """Own the send lock and in-flight count for a cancellable inline write."""
        ownership_started.set()
        try:
            async with self._outbound_send_lock:
                if not self._can_send_first_audio_inline(outbound_task):
                    return False
                await self._send_outbound_chunk(chunk, turn)
                return True
        finally:
            await self._finish_outbound_send(replayed_chunk=False)

    async def _await_non_cancellable_send(
        self,
        task: asyncio.Task[bool],
        *,
        timeout: float,
    ) -> bool:
        """Let a healthy send run normally; bound only caller cancellation.

        Before the caller is cancelled there is no router-imposed send
        deadline: ordinary transport backpressure remains healthy. Once
        cancellation arrives, the owned write gets a bounded completion window.
        A write that ignores cancellation triggers transport termination and
        remains scope-owned, locked, and in-flight until it actually exits.
        """
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                current = asyncio.current_task()
                if current is None or not current.cancelling():
                    raise
                cancellation = cancellation or exc
                break

        if cancellation is None:
            return task.result()

        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except TimeoutError:
            task.cancel()
            done, _ = await asyncio.wait(
                {task},
                timeout=self._INLINE_SEND_CANCEL_GRACE_S,
            )
            if not done:
                await self._terminate_stalled_inline_send()
                done, _ = await asyncio.wait(
                    {task},
                    timeout=self._INLINE_SEND_CANCEL_GRACE_S,
                )
            if not done:
                logger.warning("Cancelled inline transport audio send remains lifecycle-owned")

        raise cancellation

    async def _terminate_stalled_inline_send(self) -> None:
        """Bound transport termination used to unblock a cancelled write."""
        try:
            async with asyncio.timeout(self._INLINE_SEND_CANCEL_GRACE_S):
                await self._transport.disconnect()
        except TimeoutError:
            logger.warning("Transport disconnect timed out while cancelling inline audio")
        except Exception:
            logger.exception("Transport disconnect failed while cancelling inline audio")

    def reset_speech_detection(self) -> None:
        """Reset the auto-turn speech-energy counter.

        Plumbed into ``STTCommitter.cancel`` via
        ``on_speech_detection_reset`` so a cancellation while a partial
        speech-energy run was accruing does not start a turn next chunk.
        """
        self._auto_turn_speech_frames = 0

    def reset_replay_chunks(self) -> None:
        """Zero the gated-replay pending counter (Session calls this on turn reset)."""
        self._replay_chunks_pending = 0
        self._replay_enqueue_done = False

    def discard_pending_capture_audio(self) -> None:
        """Discard raw far-end frames queued before capture became allowed."""
        if self._transport_has_aec_drain:
            drain_aec_reference_frames(self._transport)

    async def gated_replay(self, events: list[Any]) -> None:
        """Replay buffered TTS audio chunks through the outbound queue.

        Transitions through BOT_SPEAKING so that caller speech during
        replay is treated as barge-in and the corresponding events fire.
        Called by the classification gate flush callback.
        """
        from easycat.events import TTSAudio

        already_replaying = self._turn_manager.state == TurnManagerState.BOT_SPEAKING
        # Only flush the outbound queue on the first replay call.
        # A second call (for late gate frames) must not drop audio
        # that the first replay enqueued.
        if not already_replaying:
            self._outbound_queue.flush()
        chunks = [ev.chunk for ev in events if isinstance(ev, TTSAudio)]
        if chunks:
            if not already_replaying:
                await self._turn_manager.bot_started_speaking()
            # Increment after the await so tally never counts unqueued chunks (gh 1007).
            self._replay_chunks_pending += len(chunks)
            for chunk in chunks:
                # Tag each replay chunk so the drain loop only decrements
                # ``_replay_chunks_pending`` (and only fires
                # ``bot_stopped_speaking``) when an actual replay chunk
                # drains.  Without the tag the bare counter would be
                # decremented by *any* chunk sharing the outbound queue
                # (e.g. interleaved synthesis or hold audio), which could
                # leave BOT_SPEAKING early and truncate the replayed tail.
                # Guarded: providers are duck-typed, so a foreign chunk class
                # (slots/frozen/NamedTuple) may reject the tag; it then simply
                # doesn't count against the replay tally.
                try:
                    chunk._easycat_replay_chunk = True
                except Exception:
                    logger.debug("Failed to tag replay chunk", exc_info=True)
                await self._outbound_queue.put(chunk)
            self._replay_enqueue_done = True  # type: ignore[attr-defined]

    def on_playback_ack(self, event: PlaybackMarkAck) -> None:
        """Track acknowledged playout byte positions for the active turn."""
        turn = self._current_turn()
        if not turn:
            return
        acked_bytes = turn.playback_mark_to_bytes.pop(event.mark_name, None)
        if acked_bytes is None:
            return
        if turn.playback_ack_log and acked_bytes < turn.playback_ack_log[-1][1]:
            acked_bytes = turn.playback_ack_log[-1][1]
        turn.playback_ack_log.append((event.timestamp, acked_bytes))

    async def on_audio_delivered(self, event: TransportAudioDelivered) -> None:
        """Finalize accounting for buffered transports at their no-clear point."""
        from easycat._turn_context import TurnContext as _TurnCtx

        session_id, _ = self._correlation_ids()
        if (
            event.session_id is not None
            and session_id is not None
            and event.session_id != session_id
        ):
            return

        active = self._current_turn()
        turn = None
        if isinstance(event.turn_ref, _TurnCtx):
            # A turn reference proves ownership only when the transport callback
            # is scoped to this session, or when it is literally this router's
            # current turn.  Shared EventBus deployments otherwise expose every
            # session to every buffered transport callback, and trusting an
            # unscoped foreign TurnContext can leak/re-label another session's
            # audio.
            if event.session_id == session_id or event.turn_ref is active:
                turn = event.turn_ref
            else:
                return
        elif event.session_id == session_id:
            if active is not None and (event.turn_id is None or active.id == event.turn_id):
                turn = active
        elif active is not None and event.turn_id is not None and active.id == event.turn_id:
            turn = active
        elif (
            active is not None
            and event.session_id is None
            and event.turn_id is None
            and event.turn_ref is None
            and self._accept_unscoped_audio_delivery()
        ):
            # Fully-unscoped callback: a custom reporting transport that
            # declares ``reports_audio_delivery = True`` and emits a bare
            # ``TransportAudioDelivered(chunk=...)`` with no ownership
            # metadata.  Only a private single-router bus can safely attribute
            # that callback to this session; shared buses must require stamped
            # session/turn metadata so one delivery cannot be relabeled by every
            # session router subscribed to the bus.
            turn = active
        else:
            return

        turn_id = event.turn_id or (turn.id if turn is not None else None)
        await self._handle_audio_delivery(event.chunk, turn)
        await self._emit(AudioOut(chunk=event.chunk, turn_id=turn_id))

    def _accept_unscoped_audio_delivery(self) -> bool:
        """Return whether a bare delivery callback is attributable to this router.

        ``TransportAudioDelivered`` is an internal bus event, and every Session
        installs one ``AudioRouter.on_audio_delivered`` handler.  A delivery
        event with no session id, turn id, or turn reference has no ownership
        proof, so it is safe to accept only when this router is the sole audio
        delivery router on the bus.  App-level observers do not affect this
        check; a second router means the bus is shared across sessions and bare
        delivery callbacks must be dropped instead of relabeled.
        """
        routers = []
        for handler in self._event_bus.subscribers(TransportAudioDelivered):
            target = getattr(handler, "__wrapped__", handler)
            if getattr(target, "__func__", None) is AudioRouter.on_audio_delivered and isinstance(
                getattr(target, "__self__", None), AudioRouter
            ):
                routers.append(target)
        return len(routers) == 1 and routers[0] == self.on_audio_delivered

    # ── Internal: ingress loop ─────────────────────────────────

    async def _run_pipeline(self) -> None:
        """Main audio receive loop: Transport -> AEC -> Noise Reduction -> VAD -> STT."""
        # Tracks consecutive per-chunk failures.  Reset to 0 after every
        # frame that processes cleanly so only a sustained run trips the
        # fatal threshold.
        consecutive_errors = 0
        try:
            async for chunk in self._transport.receive_audio():
                if not self._is_running():
                    break

                # A failure inside a single frame's stage pipeline (noise
                # reduction, VAD, or STT) must not kill the whole live
                # call — one malformed frame or a momentary backend glitch
                # is logged + surfaced as an Error and the frame is
                # skipped.  Only the outer handler (below) deals with
                # genuinely fatal conditions: transport iterator
                # exhaustion/cancellation, or a sustained run of failures.
                try:
                    await self._process_chunk(chunk)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_errors += 1
                    logger.warning(
                        "Pipeline chunk failed (%d/%d consecutive); skipping frame",
                        consecutive_errors,
                        self._MAX_CONSECUTIVE_CHUNK_ERRORS,
                        exc_info=True,
                    )
                    await self._emit(Error(exception=exc, stage=ErrorStage.PIPELINE))
                    if consecutive_errors >= self._MAX_CONSECUTIVE_CHUNK_ERRORS:
                        logger.error(
                            "Pipeline exceeded %d consecutive chunk errors; tearing down",
                            self._MAX_CONSECUTIVE_CHUNK_ERRORS,
                        )
                        # Break out via a sentinel rather than re-raising
                        # ``exc``: the Error for ``exc`` was already emitted
                        # just above, so re-raising it would make the outer
                        # handler emit a duplicate Error for the same fatal
                        # frame.  The outer handler suppresses this sentinel.
                        raise _PipelineTornDown from exc
                    continue
                else:
                    consecutive_errors = 0

        except asyncio.CancelledError:
            pass
        except _PipelineTornDown:
            # Terminal teardown after sustained per-chunk failures.  The
            # Error was already emitted by the per-chunk handler; do not
            # emit a second one for the same fatal frame.
            pass
        except Exception as exc:
            logger.exception("Pipeline error")
            await self._emit(Error(exception=exc, stage=ErrorStage.PIPELINE))
        finally:
            # When the pipeline exits (transport disconnect, cancellation, or
            # error), the Session needs to know so callers polling
            # ``is_running`` can detect the transport is gone.
            #
            # We do NOT close the outbound queue here — an in-flight turn
            # (agent + TTS) may still be producing audio that needs to drain.
            # Session.stop() handles full cleanup.
            if self._is_running():
                logger.debug("Pipeline exited while session was running; marking session stopped")
                self._set_running(False)

    async def _feed_reference_or_disable(
        self,
        chunk: AudioChunk,
        turn: TurnContext | None,
    ) -> None:
        """Feed one far-end reference frame into AEC, latching off on failure.

        Single owner of the far-end feed, shared by both reference-feed
        paths: the drain path (``_feed_transport_aec_reference``, used by
        delivery-reporting transports that expose
        ``drain_aec_reference_frames()`` — local + webrtc) and the
        non-drain path (``_handle_audio_delivery``).  A feed failure (most
        commonly a near/far sample-rate mismatch, which LiveKitAEC rejects
        with a ValueError) latches ``_aec_reference_failed`` so the
        actionable cause is logged exactly once and no further feeds are
        attempted for the rest of the session.  On a successful feed it
        journals the decimated reference frame when capture is opted in and
        an artifact store is present — the far-end leg is the one AEC track
        the pipeline never journals on its own.
        """
        if self._aec_reference_failed:
            return
        try:
            self._echo_canceller.feed_reference(chunk)
        except Exception:
            self._aec_reference_failed = True
            logger.warning(
                "AEC reference disabled for this session: feed_reference failed "
                "(commonly a near/far sample-rate mismatch). Echo cancellation will "
                "not subtract bot playback. Align the TTS/transport output rate with "
                "the mic rate or resample before AEC.",
                exc_info=True,
            )
            return
        if (
            self._capture_aec_reference
            and self._run_ctx.artifact_store is not None
            and (
                self._run_ctx.audio_capture_enabled is None
                or self._run_ctx.audio_capture_enabled()
            )
        ):
            await self._maybe_record_aec_reference(chunk, turn)

    async def _feed_transport_aec_reference(
        self, mic_chunk: AudioChunk, turn: TurnContext | None
    ) -> None:
        """Drain the transport's AEC reference queue and feed each frame.

        Called from _process_chunk before AudioStage.execute() so the AEC3
        adaptive filter always receives the far-end reference before the
        near-end mic frame for the same time window.  Applies to
        delivery-reporting transports that expose
        ``drain_aec_reference_frames()`` (local + webrtc).  Guards on
        _enable_aec(), _transport_has_aec_drain, and _aec_reference_failed
        so it is safe to call unconditionally when the audio stage runs.
        Each drained frame is fed (and journaled) through
        ``_feed_reference_or_disable``, which latches _aec_reference_failed
        on the first exception so future calls are skipped without log spam.
        """
        if (
            not self._enable_aec()
            or not self._transport_has_aec_drain
            or self._aec_reference_failed
        ):
            return
        frames = drain_aec_reference_frames(self._transport)
        if not frames:
            return
        for reference in frames:
            if self._aec_reference_failed:
                break
            # Typed reference frames preserve the actual far-end format.
            # Legacy third-party transports may still return raw bytes from
            # the original optional hook; retain the prior mic-format fallback
            # for compatibility, while built-in transports never need to guess.
            ref_chunk = (
                reference
                if isinstance(reference, AudioChunk)
                else AudioChunk(data=reference, format=mic_chunk.format)
            )
            await self._feed_reference_or_disable(ref_chunk, turn)

    async def _process_chunk(self, chunk: AudioChunk) -> None:
        """Run a single received frame through the stage pipeline.

        Raises on any stage failure so the caller can apply the
        per-chunk error policy (skip + surface) without conflating it
        with fatal transport-iterator conditions.
        """
        # Decide at ingress so pre-roll and smart-turn buffers cannot later
        # inherit a newly granted consent decision.
        audio_capture_allowed(self._run_ctx, chunk)

        # Snapshot the active turn once so all stage calls operate on the
        # same context.
        turn = self._current_turn() or self._no_turn
        # This runs in the long-lived pipeline task (created at session start,
        # before any turn), so it cannot inherit the turn id from the context
        # that started the turn — bind it here so records emitted while
        # processing this frame (and the loop's error handlers) are correlated
        # (cleared to "-" while idle).
        bind_turn(None if turn is self._no_turn else turn.id)

        with observability.span(
            "easycat.transport.receive",
            {"easycat.surface": "stt"},
        ):
            chunk_bytes = getattr(chunk, "data", None)
            if isinstance(chunk_bytes, (bytes, bytearray)):
                observability.increment_counter(
                    "easycat.audio.bytes.total",
                    value=len(chunk_bytes),
                    attributes={"easycat.surface": "stt"},
                )
                observability.increment_counter(
                    "easycat.audio.frames.total",
                    attributes={"easycat.surface": "stt"},
                )
            await self._emit(AudioIn(chunk=chunk))

        # Stages 1-2: Echo cancellation + Noise reduction via AudioStage.
        # AudioStage wraps both so a single journal record covers the pair
        # as one replay stage.  For delivery-reporting transports that expose
        # a synchronous AEC reference drain queue via
        # drain_aec_reference_frames() (local + webrtc), feed all far-end
        # frames accumulated since the last mic callback *before*
        # AudioStage.execute() so AEC3 always receives the far-end signal
        # before the near-end echo.
        if self._enable_noise_reduction() or self._enable_aec():
            await self._feed_transport_aec_reference(chunk, turn)
            chunk = await self._audio_stage.execute(chunk, self._run_ctx, turn)

        # Stage 3: VAD (optional) via VADStage.
        deferred_vad_events: list[Any] = []
        if self._enable_vad():
            vad_events = await self._vad_stage.execute(chunk, self._run_ctx, turn)
            deferred_vad_events = await self._route_vad_events_before_stt(vad_events)

        # TurnManager always sees raw audio frames for pre-roll buffering
        self._turn_manager.on_audio_frame(chunk)

        # Stage 4: Feed audio to STT (if listening)
        try:
            await self._send_chunk_to_stt(chunk)
        finally:
            # Apply the VAD provider's already-observed state transition even
            # when STT rejects the boundary frame. Once a stop is deferred,
            # replay every later event after the send attempt so the original
            # provider ordering is preserved.
            for vad_event in deferred_vad_events:
                # Open the pause epoch before publishing VADStopSpeaking.
                # STTCommitter subscribes to that event and must capture the
                # exact new pause lease when it creates the delayed commit.
                if isinstance(vad_event, VADStopSpeaking):
                    await self._turn_manager.on_vad_event(vad_event)
                await self._emit(vad_event)
                if not isinstance(vad_event, VADStopSpeaking):
                    await self._turn_manager.on_vad_event(vad_event)

    async def _send_chunk_to_stt(self, chunk: AudioChunk) -> None:
        """Start auto-turn STT when needed and send one active-turn frame."""
        started_turn_from_chunk = False
        if self._auto_turn_from_stt_final() and not self._is_stt_active():
            if self._turn_manager.state == TurnManagerState.IDLE:
                if _chunk_has_speech_energy(chunk):
                    self._auto_turn_speech_frames += 1
                else:
                    self._auto_turn_speech_frames = 0

                if self._auto_turn_speech_frames >= 2:
                    await self._turn_manager.start_turn()
                    self._auto_turn_speech_frames = 0
                    started_turn_from_chunk = self._is_stt_active()
            else:
                self._auto_turn_speech_frames = 0

        if self._is_stt_active() and not started_turn_from_chunk:
            active_turn = self._current_turn()
            if active_turn is not None:
                active_turn.stt_has_uncommitted_audio = True
            await self._stt_stage.execute(
                chunk,
                self._run_ctx,
                active_turn or self._no_turn,
            )

    async def _route_vad_events_before_stt(self, vad_events: list[Any]) -> list[Any]:
        """Emit events before the first stop, then defer the remaining suffix."""
        deferred_events: list[Any] = []
        deferring = False
        for vad_event in vad_events:
            vad_event = self._with_correlation(vad_event)
            # The stop-producing frame must reach STT before a pause can
            # schedule commit_segment() or end_stream(). Otherwise a
            # zero-delay commit/end command can overlap the provider's
            # send_audio() for that final frame.
            if deferring or isinstance(vad_event, VADStopSpeaking):
                deferring = True
                deferred_events.append(vad_event)
                continue
            await self._emit(vad_event)
            await self._turn_manager.on_vad_event(vad_event)
        return deferred_events

    # ── Internal: outbound drain ───────────────────────────────

    async def _drain_outbound_audio(self) -> None:
        """Send queued outbound audio to the transport with backpressure."""
        while True:
            if not self._is_running() and self._outbound_queue.empty():
                break
            try:
                chunk = await self._outbound_queue.get()
            except asyncio.QueueEmpty:
                break
            # A chunk only counts against the replay tally if it was
            # actually tagged as a replay chunk in ``gated_replay`` — not
            # merely because replay chunks are pending.  This keeps the
            # tally correct even if a non-replay chunk shares the outbound
            # queue while replay audio is still draining.
            # ``getattr`` keeps foreign chunk objects (duck-typed providers,
            # app-injected hold audio) from killing the drain task here.
            replayed_chunk = self._replay_chunks_pending > 0 and getattr(
                chunk, "_easycat_replay_chunk", False
            )
            turn = self._current_turn()
            # Claim before waiting for the send lock. Otherwise a contended
            # dequeued chunk disappears from both queue depth and in-flight
            # accounting, allowing await_drain() to report a false idle gap.
            self._claim_outbound_send()
            try:
                async with self._outbound_send_lock:
                    await self._send_outbound_chunk(chunk, turn)
            finally:
                await self._finish_outbound_send(replayed_chunk=replayed_chunk)

        await self.flush_trailing_playback_mark()

    def _claim_outbound_send(self) -> None:
        """Count a dequeued or inline chunk before its first ownership await."""
        self._outbound_in_flight += 1
        self._update_outbound_idle()

    async def _send_outbound_chunk(
        self,
        chunk: AudioChunk,
        turn: TurnContext | None,
    ) -> None:
        """Deliver one claimed chunk and apply shared accounting/error policy."""
        bind_turn(turn.id if turn is not None else None)
        try:
            self._stamp_outbound_chunk(chunk, turn)
            delivered = await self._transport_stage.execute(
                chunk, self._run_ctx, turn or self._no_turn
            )
            self._outbound_send_failures = 0
            if delivered and not self._transport_reports_audio_delivery:
                await self._handle_audio_delivery(chunk, turn)
                await self._emit(
                    AudioOut(chunk=chunk, turn_id=turn.id if turn is not None else None)
                )
        except Exception as exc:
            logger.exception("Failed to send audio to transport")
            self._outbound_send_failures += 1
            if self._outbound_send_failures == 1:
                try:
                    await self._emit(Error(exception=exc, stage=ErrorStage.TTS))
                except Exception:
                    logger.debug("Failed to emit outbound send Error", exc_info=True)

    async def _finish_outbound_send(self, *, replayed_chunk: bool) -> None:
        """Release one claimed chunk after send and delivery accounting finish."""
        self._outbound_in_flight = max(0, self._outbound_in_flight - 1)
        self._update_outbound_idle()
        replay_pending_finished = False
        if replayed_chunk:
            self._replay_chunks_pending = max(0, self._replay_chunks_pending - 1)
            replay_pending_finished = self._replay_chunks_pending == 0
        if (
            self._replay_chunks_pending > 0
            and self._outbound_queue.empty()
            and getattr(self, "_replay_enqueue_done", True)
        ):
            # DROP_OLDEST can evict replay chunks before the drain sees
            # them; once the real queue empties, reconcile the tally.
            self._replay_chunks_pending = 0
            replay_pending_finished = True
        if replay_pending_finished and self._turn_manager.state == TurnManagerState.BOT_SPEAKING:
            await self._turn_manager.bot_stopped_speaking()

    async def flush_trailing_playback_mark(self, turn: TurnContext | None = None) -> None:
        """Emit a playback mark for queued tail bytes that missed the throttle interval."""
        turn = turn or self._current_turn()
        if turn and turn.bytes_since_last_mark > 0 and self._playback_ack_transport is not None:
            turn.bytes_since_last_mark = 0
            await self._send_playback_mark(turn)

    def _stamp_outbound_chunk(self, chunk: AudioChunk, turn: TurnContext | None) -> None:
        """Attach session/turn ownership so buffered transports can report later delivery."""
        session_id, _ = self._correlation_ids()
        # Guarded: a foreign chunk class that rejects the stamp must still be
        # sent (unstamped delivery loses attribution, not audio).
        try:
            chunk._easycat_session_id = session_id
            chunk._easycat_turn_id = turn.id if turn is not None else None
            chunk._easycat_turn_ref = turn
        except Exception:
            logger.debug("Failed to stamp outbound audio chunk metadata", exc_info=True)

    async def _handle_audio_delivery(
        self,
        chunk: AudioChunk,
        turn: TurnContext | None,
    ) -> None:
        # Feeding the far-end reference into AEC is a *side effect* of audio
        # delivery, not part of it.  A reference-feed failure (most commonly a
        # near/far sample-rate mismatch, which LiveKitAEC rejects with a
        # ValueError) must never be attributed to "Failed to send audio to
        # transport" nor suppress the downstream AudioOut emit / playback-mark
        # accounting.  Isolate it here: log the real cause once and continue
        # with the bot's audio still being delivered and tracked.
        #
        # Delivery-reporting transports that expose drain_aec_reference_frames()
        # (local + webrtc) feed the reference in _process_chunk (before the
        # near-end mic frame is processed) so AEC3 receives the far-end signal
        # before the near-end echo for every mic window.  Skip the feed here for
        # those transports to avoid double-feeding the same audio through the
        # adaptive filter; non-drain transports feed (and journal) it here via
        # the shared _feed_reference_or_disable owner.
        skip_feed = self._aec_reference_failed or self._transport_has_aec_drain
        if self._enable_aec() and not skip_feed:
            await self._report_degraded_aec_reference_once()
            await self._feed_reference_or_disable(chunk, turn)

        sent_size = len(chunk.data)
        # Never accrue byte counters on the long-lived _no_turn singleton
        # (it is created once and never replaced).  Real callers always
        # pass current_turn() (real-or-None); this keeps _no_turn inert
        # and consistent with the guards in STTCommitter.
        if turn is None or turn is self._no_turn:
            return

        turn.record_audio_sent(sent_size, chunk.duration_ms)
        if sent_size <= 0 or self._playback_ack_transport is None:
            return

        if turn.bytes_since_last_mark >= self._playback_mark_bytes_interval or (
            turn.bytes_since_last_mark > 0
            and self._turn_manager.state != TurnManagerState.BOT_SPEAKING
            and self._outbound_queue.empty()
        ):
            turn.bytes_since_last_mark = 0
            await self._send_playback_mark(turn)

    async def _report_degraded_aec_reference_once(self) -> None:
        """Record that server-side AEC lacks a playback-timed reference.

        Transports without ``drain_aec_reference_frames`` can preserve the
        explicit AEC feature only by feeding audio at send time. That reference
        has gaps during silence and, for remote transports, precedes actual
        playout by an unknown buffer delay. Emit one durable event before the
        first such feed so bundle readers can distinguish this best-effort mode
        from playback-clocked AEC.
        """
        if self._aec_reference_degraded_reported:
            return
        self._aec_reference_degraded_reported = True
        provider = str(getattr(self._transport, "transport_kind", "unknown"))
        detail = (
            "server-side AEC reference is fed at transport send time and has no "
            "playout-clocked silence frames; prefer endpoint echo cancellation "
            "or a transport with drain_aec_reference_frames()"
        )
        logger.warning("%s AEC reference degraded: %s", provider, detail)
        event = TransportDegraded(
            provider=provider,
            reason="aec_reference_degraded",
            detail=detail,
        )

        async def _emit_degraded() -> None:
            await self._emit(event)

        task: asyncio.Task[None] = self._runtime_scope.create_task(
            self._AEC_DEGRADED_EMIT_TASK_NAME,
            _emit_degraded(),
        )
        task.add_done_callback(self._runtime_scope.log_task_exception)

    async def _maybe_record_aec_reference(
        self,
        chunk: AudioChunk,
        turn: TurnContext | None,
    ) -> None:
        """Journal one decimated AEC far-end reference frame, best-effort.

        Only invoked when capture is opted in and an artifact store is present.
        Decimates to roughly one journaled frame per second so the debugger
        keeps its three-track alignment without adding a per-frame artifact +
        journal write to the live audio loop.  A capture failure must never
        raise, never disable AEC, and never be attributed to the audio send.
        """
        index = self._aec_reference_frame_index
        self._aec_reference_frame_index += 1
        if index % _AEC_REFERENCE_CAPTURE_EVERY_N_FRAMES != 0:
            return
        try:
            await self._audio_stage.record_reference(
                chunk,
                self._run_ctx,
                turn or self._no_turn,
            )
        except Exception:
            logger.debug("Failed to record AEC reference frame", exc_info=True)

    async def _send_playback_mark(self, turn: TurnContext) -> None:
        if self._playback_ack_transport is None:
            return
        # on_playback_ack only ever clears the active turn's dict, never
        # the long-lived _no_turn singleton — marks recorded against it
        # would accumulate for the session's lifetime.
        if turn is self._no_turn:
            return

        self._playback_mark_seq += 1
        requested_mark_name = f"ec_playback_{self._playback_mark_seq}"
        turn.playback_mark_to_bytes[requested_mark_name] = turn.audio_bytes_sent
        try:
            mark_name = await self._playback_ack_transport.send_playback_mark(
                name=requested_mark_name
            )
            if mark_name != requested_mark_name:
                acked_bytes = turn.playback_mark_to_bytes.pop(requested_mark_name, None)
                if acked_bytes is not None:
                    turn.playback_mark_to_bytes[mark_name] = acked_bytes
        except Exception:
            turn.playback_mark_to_bytes.pop(requested_mark_name, None)
            logger.debug("Failed to send playback mark", exc_info=True)


__all__ = ["AudioRouter"]
