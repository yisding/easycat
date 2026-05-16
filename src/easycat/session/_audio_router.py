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
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from easycat.audio_format import AudioChunk
from easycat.bounded_queue import BoundedAudioQueue
from easycat.events import (
    AudioIn,
    AudioOut,
    Error,
    ErrorStage,
    EventBus,
    PlaybackMarkAck,
    TransportAudioDelivered,
)
from easycat.providers import Transport
from easycat.runtime.capabilities import (
    PlaybackAcknowledgements,
    playback_acknowledgements,
    transport_reports_audio_delivery,
)
from easycat.runtime.context import RunContext
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._text import _chunk_has_speech_energy
from easycat.stages.audio import AudioStage
from easycat.stages.stt import STTStage
from easycat.stages.transport import TransportStage
from easycat.stages.vad import VADStage
from easycat.turn_manager import TurnManager, TurnManagerState

if TYPE_CHECKING:
    from easycat.session._turn_context import TurnContext

logger = logging.getLogger(__name__)


class AudioRouter:
    """Routes audio between the transport and the pipeline stages.

    Owns the receive loop (transport -> audio/vad/stt stages) and the
    outbound drain loop (queue -> transport.send_audio).  Owns the
    playback-mark accounting, the auto-turn speech-energy counter, and
    the gated-replay book-keeping used when the classification gate
    flushes buffered TTS audio.

    Known limitation: the WebRTC transport's ``_handle_offer`` swaps
    its outbound queue on reconnect; the router takes the queue by
    reference at construction and does not handle that swap.
    """

    def __init__(
        self,
        *,
        transport: Transport,
        audio_stage: AudioStage,
        vad_stage: VADStage,
        stt_stage: STTStage,
        transport_stage: TransportStage,
        turn_manager: TurnManager,
        event_bus: EventBus,
        journal_sink: SessionJournalSink,
        run_ctx: RunContext,
        no_turn: TurnContext,
        echo_canceller: Any,
        # Capability flags as callables so the loop body reads live
        # values even when Session mutates them after construction.
        enable_noise_reduction: Callable[[], bool],
        enable_aec: Callable[[], bool],
        enable_vad: Callable[[], bool],
        auto_turn_from_stt_final: Callable[[], bool],
        # Callbacks
        emit: Callable[[Any], Awaitable[None]],
        is_running: Callable[[], bool],
        set_running: Callable[[bool], None],
        current_turn: Callable[[], TurnContext | None],
        is_stt_active: Callable[[], bool],
        with_correlation: Callable[[Any], Any] | None = None,
        # Outbound queue is constructed by Session; the router receives
        # the same instance so external supplies and the TTSSynthesizer
        # keep their references valid.
        outbound_queue: BoundedAudioQueue,
    ) -> None:
        self._transport = transport
        self._audio_stage = audio_stage
        self._vad_stage = vad_stage
        self._stt_stage = stt_stage
        self._transport_stage = transport_stage
        self._turn_manager = turn_manager
        self._event_bus = event_bus
        self._journal_sink = journal_sink
        self._run_ctx = run_ctx
        self._no_turn = no_turn
        self._echo_canceller = echo_canceller

        self._enable_noise_reduction = enable_noise_reduction
        self._enable_aec = enable_aec
        self._enable_vad = enable_vad
        self._auto_turn_from_stt_final = auto_turn_from_stt_final

        self._emit = emit
        self._is_running = is_running
        self._set_running = set_running
        self._current_turn = current_turn
        self._is_stt_active = is_stt_active
        self._with_correlation = with_correlation or (lambda evt: evt)

        # Auto-turn speech-energy detector state
        self._auto_turn_speech_frames: int = 0

        # Gated replay
        self._replay_chunks_pending: int = 0

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
        self._pipeline_task = asyncio.create_task(self._run_pipeline())
        return self._pipeline_task

    def start_outbound(self) -> asyncio.Task[None]:
        """Start the outbound audio drain task."""
        self._outbound_task = asyncio.create_task(self._drain_outbound_audio())
        return self._outbound_task

    async def stop_ingress(self) -> None:
        """Cancel the ingress task and wait for it to exit."""
        task = self._pipeline_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._pipeline_task = None

    async def stop_outbound(self) -> None:
        """Cancel the outbound drain task and wait for it to exit."""
        task = self._outbound_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._outbound_task = None

    async def await_drain(self, timeout: float = 2.0) -> None:
        """Wait for the outbound queue to empty, with a timeout.

        If the transport's ``send_audio`` is blocked (network backpressure,
        stalled connection), the outbound worker cannot make progress and the
        queue never empties.  A bounded wait prevents turn cleanup from
        hanging indefinitely in that scenario.
        """
        if not self._outbound_task or self._outbound_task.done():
            return
        deadline = time.monotonic() + timeout
        while not self._outbound_queue.empty():
            if time.monotonic() >= deadline:
                logger.warning("Outbound queue drain timed out after %.1fs", timeout)
                break
            await asyncio.sleep(0)

    async def queue_outbound(self, chunk: AudioChunk) -> None:
        """Enqueue a TTS chunk for the outbound drain loop."""
        await self._outbound_queue.put(chunk)

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
            self._replay_chunks_pending += len(chunks)
            if not already_replaying:
                await self._turn_manager.bot_started_speaking()
            for chunk in chunks:
                await self._outbound_queue.put(chunk)

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
        from easycat.session._turn_context import TurnContext as _TurnCtx

        turn = event.turn_ref if isinstance(event.turn_ref, _TurnCtx) else None
        if turn is None:
            active = self._current_turn()
            if active is not None and (event.turn_id is None or active.id == event.turn_id):
                turn = active

        turn_id = event.turn_id or (turn.id if turn is not None else None)
        await self._handle_audio_delivery(event.chunk, turn)
        await self._emit(AudioOut(chunk=event.chunk, turn_id=turn_id))

    # ── Internal: ingress loop ─────────────────────────────────

    async def _run_pipeline(self) -> None:
        """Main audio receive loop: Transport -> Noise Reduction -> AEC -> VAD -> STT."""
        try:
            async for chunk in self._transport.receive_audio():
                if not self._is_running():
                    break

                # Snapshot the active turn once per iteration so all stage
                # calls inside the loop body operate on the same context.
                turn = self._current_turn() or self._no_turn

                await self._emit(AudioIn(chunk=chunk))

                # Stages 1-2: Noise reduction + Echo cancellation via AudioStage.
                # AudioStage wraps both so a single journal record covers
                # the pair — matches WS3 T3.10's intent that Audio is
                # one stage for replay purposes.
                if self._enable_noise_reduction() or self._enable_aec():
                    chunk = await self._audio_stage.execute(chunk, self._run_ctx, turn)

                # Stage 3: VAD (optional) via VADStage.
                if self._enable_vad():
                    vad_events = await self._vad_stage.execute(chunk, self._run_ctx, turn)
                    for vad_event in vad_events:
                        vad_event = self._with_correlation(vad_event)
                        await self._emit(vad_event)
                        await self._turn_manager.on_vad_event(vad_event)

                # TurnManager always sees raw audio frames for pre-roll buffering
                self._turn_manager.on_audio_frame(chunk)

                # Stage 4: Feed audio to STT (if listening)
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

                        # Re-snapshot in case start_turn() installed a new turn
                        # that the subsequent STT feed should target.
                        turn = self._current_turn() or self._no_turn
                    else:
                        self._auto_turn_speech_frames = 0

                if self._is_stt_active() and not started_turn_from_chunk:
                    active_turn = self._current_turn()
                    if active_turn is not None:
                        active_turn.stt_has_uncommitted_audio = True
                    await self._stt_stage.execute(
                        chunk, self._run_ctx, active_turn or self._no_turn
                    )

        except asyncio.CancelledError:
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
            # Session's stop()/shutdown() handles full cleanup.
            if self._is_running():
                logger.debug("Pipeline exited while session was running; marking session stopped")
                self._set_running(False)

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
            replayed_chunk = self._replay_chunks_pending > 0
            turn = self._current_turn()
            try:
                self._stamp_outbound_chunk(chunk, turn)
                delivered = await self._transport_stage.execute(
                    chunk, self._run_ctx, turn or self._no_turn
                )
                if delivered and not self._transport_reports_audio_delivery:
                    # Stamp turn_id from current_turn() at dequeue time
                    # (captured before send_audio awaits) so a slow send
                    # under backpressure doesn't inherit a newer turn's id.
                    await self._handle_audio_delivery(chunk, turn)
                    await self._emit(
                        AudioOut(chunk=chunk, turn_id=turn.id if turn is not None else None)
                    )
            except Exception:
                logger.exception("Failed to send audio to transport")
            finally:
                if replayed_chunk:
                    self._replay_chunks_pending = max(0, self._replay_chunks_pending - 1)
                    if (
                        self._replay_chunks_pending == 0
                        and self._turn_manager.state == TurnManagerState.BOT_SPEAKING
                    ):
                        await self._turn_manager.bot_stopped_speaking()

        # Send a final mark for any trailing bytes
        turn = self._current_turn()
        if turn and turn.bytes_since_last_mark > 0 and self._playback_ack_transport is not None:
            turn.bytes_since_last_mark = 0
            await self._send_playback_mark(turn)

    def _stamp_outbound_chunk(self, chunk: AudioChunk, turn: TurnContext | None) -> None:
        """Attach turn ownership so buffered transports can report later delivery."""
        try:
            setattr(chunk, "_easycat_turn_id", turn.id if turn is not None else None)
            setattr(chunk, "_easycat_turn_ref", turn)
        except Exception:
            logger.debug("Failed to stamp outbound audio chunk metadata", exc_info=True)

    async def _handle_audio_delivery(
        self,
        chunk: AudioChunk,
        turn: TurnContext | None,
    ) -> None:
        if self._enable_aec():
            self._echo_canceller.feed_reference(chunk)

        sent_size = len(chunk.data)
        if turn is None:
            return

        turn.record_audio_sent(sent_size, chunk.duration_ms)
        if sent_size <= 0 or self._playback_ack_transport is None:
            return

        if turn.bytes_since_last_mark >= self._playback_mark_bytes_interval:
            turn.bytes_since_last_mark = 0
            await self._send_playback_mark(turn)
        elif (
            turn.bytes_since_last_mark > 0
            and self._turn_manager.state != TurnManagerState.BOT_SPEAKING
            and self._outbound_queue.empty()
        ):
            turn.bytes_since_last_mark = 0
            await self._send_playback_mark(turn)

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
