"""Session: the core runtime for a single voice conversation.

Manages the voice pipeline lifecycle, wires provider stages together,
and handles turn state and cancellation. Supports both basic and
streaming agent interfaces with incremental TTS synthesis.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from easycat.agent_runner import AgentStreamEventType
from easycat.bounded_queue import BoundedAudioQueue, DropPolicy
from easycat.cancel import CancelToken
from easycat.events import (
    AgentDelta,
    AgentFinal,
    AudioIn,
    Error,
    EventBus,
    Interruption,
    ReconnectSuccess,
    STTEventType,
    STTFinal,
    STTPartial,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
    TTSAudio,
    TTSEventType,
    TTSMarkers,
    TurnEnded,
    TurnStarted,
)
from easycat.health_check import PeriodicHealthChecker
from easycat.metrics import (
    AGENT_LATENCY,
    ERRORS,
    INTERRUPTIONS,
    RECONNECTS,
    STT_LATENCY,
    TTS_TTFB,
    TURN_E2E,
    MetricsCollector,
)
from easycat.stubs import (
    NoopAgent,
    NoopNoiseReducer,
    NoopSTT,
    NoopTransport,
    NoopTTS,
    NoopVAD,
)
from easycat.timeouts import (
    AgentTimeoutError,
    STTTimeoutError,
    TimeoutConfig,
    TTSTimeoutError,
    with_agent_timeout,
    with_tts_timeout,
)
from easycat.tracing import SpanStatus, TraceContext, Tracer
from easycat.turn_manager import TurnManager, TurnManagerConfig

logger = logging.getLogger(__name__)

# Sentence boundary regex: matches whitespace after sentence-ending punctuation
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")


# ── Agent protocol (lightweight — WS7 provides real implementations) ──


@runtime_checkable
class Agent(Protocol):
    """Minimal agent interface: receive text, produce text."""

    async def run(self, text: str) -> str: ...


# ── Turn state ─────────────────────────────────────────────────────


class TurnState(enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    BOT_SPEAKING = "bot_speaking"


# ── Session configuration ─────────────────────────────────────────


@dataclass
class SessionConfig:
    """Configuration for a Session."""

    stt: Any = None
    tts: Any = None
    vad: Any = None
    noise_reducer: Any = None
    transport: Any = None
    agent: Any = None
    event_bus: EventBus | None = None
    turn_manager: TurnManager | None = None
    turn_manager_config: TurnManagerConfig | None = None
    timeout_config: TimeoutConfig | None = None
    metrics: MetricsCollector | None = None
    tracer: Tracer | None = None
    outbound_queue: BoundedAudioQueue | None = None

    # Pipeline flags
    enable_noise_reduction: bool = True
    enable_vad: bool = True


# ── Helpers ────────────────────────────────────────────────────────


def _split_at_sentence_boundaries(text: str) -> tuple[str, str]:
    """Split text at the last sentence boundary.

    Returns (ready_text, remaining_buffer). ``ready_text`` contains complete
    sentences to send to TTS; ``remaining_buffer`` holds any trailing text
    that hasn't reached a sentence boundary yet.
    """
    matches = list(_SENTENCE_END_RE.finditer(text))
    if not matches:
        return "", text
    last_match = matches[-1]
    return text[: last_match.start()], text[last_match.end() :]


# ── Session ────────────────────────────────────────────────────────


class Session:
    """One voice session (per call / per websocket client).

    Manages the full pipeline: Audio In -> Noise Reduction -> VAD -> STT ->
    Agent -> TTS -> Audio Out. Each stage is a pluggable provider.

    When the configured agent supports streaming (has a ``run_streaming``
    method), the session consumes text deltas incrementally and begins
    TTS synthesis on sentence boundaries for lower latency.
    """

    def __init__(self, config: SessionConfig | None = None) -> None:
        cfg = config or SessionConfig()

        # Providers (fall back to no-op stubs)
        self.stt = cfg.stt or NoopSTT()
        self.tts = cfg.tts or NoopTTS()
        self.vad = cfg.vad or NoopVAD()
        self.noise_reducer = cfg.noise_reducer or NoopNoiseReducer()
        self.transport = cfg.transport or NoopTransport()
        self.agent: Agent = cfg.agent or NoopAgent()

        # Event system
        self.event_bus = cfg.event_bus or EventBus()

        # Attach event bus to providers that accept it
        self._maybe_attach_event_bus(self.stt)
        self._maybe_attach_event_bus(self.tts)
        self._maybe_attach_event_bus(self.transport)

        # Pipeline flags
        self._enable_noise_reduction = cfg.enable_noise_reduction
        self._enable_vad = cfg.enable_vad

        # Turn manager
        self._turn_manager = cfg.turn_manager or TurnManager(
            self.event_bus,
            config=cfg.turn_manager_config,
            cancel_turn_callback=self._cancel_for_barge_in,
        )
        self.event_bus.subscribe(TurnStarted, self._on_turn_started)
        self.event_bus.subscribe(TurnEnded, self._schedule_turn_ended)

        # Reliability/observability config
        self._timeout_config = cfg.timeout_config or TimeoutConfig()
        self._metrics = cfg.metrics
        self._tracer = cfg.tracer
        self._trace_context: TraceContext | None = None
        self._turn_span = None
        self._stt_span = None
        self._agent_span = None
        self._tts_span = None

        # Backpressure (outbound audio queue)
        self._outbound_queue_external = cfg.outbound_queue is not None
        self._outbound_queue_max_size = 200
        self._outbound_queue_policy = DropPolicy.DROP_OLDEST
        self._outbound_queue_name = "outbound_audio"
        self._outbound_queue = cfg.outbound_queue or BoundedAudioQueue(
            max_size=self._outbound_queue_max_size,
            policy=self._outbound_queue_policy,
            name=self._outbound_queue_name,
        )
        self._outbound_task: asyncio.Task[None] | None = None
        self._health_checkers: list[PeriodicHealthChecker] = []

        # Metrics counters
        if self._metrics:
            self.event_bus.subscribe(
                Interruption, lambda e: self._metrics.increment_counter(INTERRUPTIONS)
            )
            self.event_bus.subscribe(
                ReconnectSuccess, lambda e: self._metrics.increment_counter(RECONNECTS)
            )
            self.event_bus.subscribe(
                Error, lambda e: self._metrics.increment_counter(ERRORS)
            )

        # State
        self._turn_state = TurnState.IDLE
        self._is_running = False
        self._pipeline_task: asyncio.Task[None] | None = None
        self._stt_task: asyncio.Task[None] | None = None
        self._current_tts_task: asyncio.Task[None] | None = None
        self._stt_final_future: asyncio.Future[str] | None = None

        # Cooperative cancellation: one token per turn
        self._cancel_token: CancelToken | None = None

        # STT stream started for current turn
        self._stt_active = False

        # Timing markers for metrics
        self._turn_end_time: float | None = None
        self._stt_final_time: float | None = None
        self._first_agent_time: float | None = None
        self._first_tts_audio_time: float | None = None

    # ── Properties ─────────────────────────────────────────────

    @property
    def turn_state(self) -> TurnState:
        return self._turn_state

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def is_speaking(self) -> bool:
        return self._turn_state == TurnState.LISTENING

    @property
    def is_bot_speaking(self) -> bool:
        return self._turn_state == TurnState.BOT_SPEAKING

    @property
    def cancel_token(self) -> CancelToken | None:
        return self._cancel_token

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize providers and begin the audio receive loop."""
        if self._is_running:
            return
        self._is_running = True
        self._turn_state = TurnState.IDLE

        await self.transport.connect()
        if not self._outbound_queue_external:
            self._outbound_queue = BoundedAudioQueue(
                max_size=self._outbound_queue_max_size,
                policy=self._outbound_queue_policy,
                name=self._outbound_queue_name,
            )
        # Start periodic health checks for providers that support it
        self._health_checkers = []
        for name, provider in (
            ("stt", self.stt),
            ("tts", self.tts),
            ("transport", self.transport),
        ):
            if hasattr(provider, "health_check"):
                checker = PeriodicHealthChecker(
                    provider,
                    provider_name=name,
                    event_bus=self.event_bus,
                )
                checker.start()
                self._health_checkers.append(checker)
        self._outbound_task = asyncio.create_task(self._drain_outbound_audio())
        self._pipeline_task = asyncio.create_task(self._run_pipeline())

    async def stop(self) -> None:
        """Gracefully stop the session: finish current turn, close providers."""
        if not self._is_running:
            return
        self._is_running = False

        if self._cancel_token:
            self._cancel_token.cancel()

        if self._pipeline_task and not self._pipeline_task.done():
            self._pipeline_task.cancel()
            try:
                await self._pipeline_task
            except asyncio.CancelledError:
                logger.debug(
                    "TTS processing task was cancelled; ensuring"
                    " BotStoppedSpeaking is emitted if needed."
                )

        await self._cancel_stt()
        await self._cancel_tts()
        for checker in self._health_checkers:
            await checker.stop()
        self._health_checkers = []
        if self._tracer and self._turn_span:
            self._tracer.finish_span(self._turn_span, SpanStatus.CANCELLED)
            self._turn_span = None
        if self._tracer and self._stt_span:
            self._tracer.finish_span(self._stt_span, SpanStatus.CANCELLED)
            self._stt_span = None
        if self._tracer and self._agent_span:
            self._tracer.finish_span(self._agent_span, SpanStatus.CANCELLED)
            self._agent_span = None
        self._outbound_queue.close()
        if self._outbound_task and not self._outbound_task.done():
            self._outbound_task.cancel()
            try:
                await self._outbound_task
            except asyncio.CancelledError:
                pass
        await self.transport.disconnect()
        self._turn_state = TurnState.IDLE

    async def shutdown(self) -> None:
        """Force-close everything and release resources."""
        self._is_running = False

        if self._cancel_token:
            self._cancel_token.cancel()

        tasks: list[asyncio.Task[Any]] = []
        if self._pipeline_task and not self._pipeline_task.done():
            self._pipeline_task.cancel()
            tasks.append(self._pipeline_task)
        if self._stt_task and not self._stt_task.done():
            self._stt_task.cancel()
            tasks.append(self._stt_task)
        if self._current_tts_task and not self._current_tts_task.done():
            self._current_tts_task.cancel()
            tasks.append(self._current_tts_task)
        if self._outbound_task and not self._outbound_task.done():
            self._outbound_task.cancel()
            tasks.append(self._outbound_task)

        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        for checker in self._health_checkers:
            await checker.stop()
        self._health_checkers = []
        if self._tracer and self._turn_span:
            self._tracer.finish_span(self._turn_span, SpanStatus.CANCELLED)
            self._turn_span = None
        if self._tracer and self._stt_span:
            self._tracer.finish_span(self._stt_span, SpanStatus.CANCELLED)
            self._stt_span = None
        if self._tracer and self._agent_span:
            self._tracer.finish_span(self._agent_span, SpanStatus.CANCELLED)
            self._agent_span = None
        self._outbound_queue.close()
        await self.transport.disconnect()
        self._turn_state = TurnState.IDLE

    # ── Cancellation ───────────────────────────────────────────

    async def cancel_turn(self, *, barge_in: bool = False) -> None:
        """Trigger cancel token, abort STT/agent/TTS, reset turn state.

        If barge_in is True, emits an Interruption event.
        """
        if self._cancel_token:
            self._cancel_token.cancel()

        if barge_in:
            await self.event_bus.emit(Interruption())

        await self._cancel_stt()
        await self._cancel_tts()
        self._outbound_queue.flush_for_new_turn()
        self._turn_state = TurnState.IDLE

        if not barge_in:
            self._turn_manager.reset()

        if self._tracer and self._turn_span:
            self._tracer.finish_span(self._turn_span, SpanStatus.CANCELLED)
            self._turn_span = None
        if self._tracer and self._stt_span:
            self._tracer.finish_span(self._stt_span, SpanStatus.CANCELLED)
            self._stt_span = None
        if self._tracer and self._agent_span:
            self._tracer.finish_span(self._agent_span, SpanStatus.CANCELLED)
            self._agent_span = None

    async def cancel_tts_playback(self) -> None:
        """Stop TTS provider and flush outbound audio."""
        if self._cancel_token:
            self._cancel_token.cancel()

        await self._cancel_tts()
        self._outbound_queue.flush_for_new_turn()
        if self._turn_state == TurnState.BOT_SPEAKING:
            self._turn_state = TurnState.IDLE

    async def reset_state(self) -> None:
        """Cancel everything and return to idle/listening state.

        Also clears agent conversation history if the agent supports it.
        """
        if self._cancel_token:
            self._cancel_token.cancel()

        await self._cancel_stt()
        await self._cancel_tts()
        self._outbound_queue.flush_for_new_turn()

        # Clear agent history if supported (e.g., AgentRunner)
        if hasattr(self.agent, "clear_history"):
            self.agent.clear_history()

        self._turn_state = TurnState.IDLE

        # Reset turn manager state
        self._turn_manager.reset()

        if self._tracer and self._turn_span:
            self._tracer.finish_span(self._turn_span, SpanStatus.CANCELLED)
            self._turn_span = None
        if self._tracer and self._stt_span:
            self._tracer.finish_span(self._stt_span, SpanStatus.CANCELLED)
            self._stt_span = None
        if self._tracer and self._agent_span:
            self._tracer.finish_span(self._agent_span, SpanStatus.CANCELLED)
            self._agent_span = None

    # ── Push-to-talk helpers ───────────────────────────────────

    async def start_turn(self) -> None:
        """Manually start a user turn (push-to-talk mode)."""
        await self._turn_manager.start_turn()

    async def end_turn(self) -> None:
        """Manually end the current user turn (push-to-talk mode)."""
        await self._turn_manager.end_turn()

    # ── TurnManager callbacks ──────────────────────────────────

    async def _cancel_for_barge_in(self) -> None:
        """Cancel current turn due to barge-in (called by TurnManager)."""
        await self.cancel_turn(barge_in=True)

    async def _on_turn_started(self, event: TurnStarted) -> None:
        """Handle TurnStarted from TurnManager: start STT and prime pre-roll."""
        if not self._is_running:
            return

        # Reset timing markers for this turn
        self._turn_end_time = None
        self._stt_final_time = None
        self._first_agent_time = None
        self._first_tts_audio_time = None

        # Initialize tracing context/span for this turn
        if self._tracer:
            self._trace_context = TraceContext()
            self._turn_span = self._tracer.start_span("turn", self._trace_context)
            self._trace_context.root_span_id = self._turn_span.span_id

        # Establish a new cancel token from TurnManager
        self._cancel_token = self._turn_manager.cancel_token or CancelToken()

        # Start STT stream
        try:
            await self.stt.start_stream()
            self._stt_active = True
            self._start_stt_event_task()
        except Exception as exc:
            logger.exception("Failed to start STT stream")
            await self.event_bus.emit(Error(exception=exc, context="stt_start"))
            self._stt_active = False
            return

        # Prime STT with pre-roll frames captured by TurnManager
        for chunk in self._turn_manager.turn_audio:
            await self.stt.send_audio(chunk)

        self._turn_state = TurnState.LISTENING

    def _schedule_turn_ended(self, event: TurnEnded) -> None:
        """Schedule end-of-turn processing without blocking other handlers."""
        if self._current_tts_task and not self._current_tts_task.done():
            self._current_tts_task.cancel()
        self._current_tts_task = asyncio.create_task(self._on_turn_ended(event))
        self._current_tts_task.add_done_callback(self._log_task_exception)

    async def _on_turn_ended(self, event: TurnEnded) -> None:
        """Handle TurnEnded from TurnManager: finalize STT and run agent/TTS."""
        if not self._is_running:
            return
        if self._turn_state != TurnState.LISTENING:
            return
        self._turn_end_time = event.timestamp
        if self._tracer and self._trace_context:
            self._stt_span = self._tracer.start_span(
                Tracer.STT, self._trace_context
            )
        await self._handle_end_of_speech()

    @staticmethod
    def _log_task_exception(task: asyncio.Task[object]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Background task failed")

    def _start_stt_event_task(self) -> None:
        """Start background consumption of provider-scoped STT events."""
        if self._stt_task and not self._stt_task.done():
            self._stt_task.cancel()
        loop = asyncio.get_running_loop()
        self._stt_final_future = loop.create_future()

        async def _consume() -> None:
            try:
                async for stt_event in self.stt.events():
                    if self._cancel_token and self._cancel_token.is_cancelled:
                        break
                    if stt_event.type == STTEventType.PARTIAL:
                        await self.event_bus.emit(STTPartial(text=stt_event.text))
                    elif stt_event.type == STTEventType.FINAL:
                        await self.event_bus.emit(STTFinal(text=stt_event.text))
                        self._stt_final_time = time.monotonic()
                        if self._metrics and self._turn_end_time is not None:
                            self._metrics.record_latency(
                                STT_LATENCY,
                                (self._stt_final_time - self._turn_end_time) * 1000,
                            )
                        if self._tracer and self._stt_span:
                            self._tracer.finish_span(self._stt_span)
                            self._stt_span = None
                        if self._stt_final_future and not self._stt_final_future.done():
                            self._stt_final_future.set_result(stt_event.text)
                        break
            except Exception as exc:
                logger.exception("STT event loop error")
                await self.event_bus.emit(Error(exception=exc, context="stt_events"))
                if self._stt_final_future and not self._stt_final_future.done():
                    self._stt_final_future.set_result("")
            finally:
                if self._stt_final_future and not self._stt_final_future.done():
                    self._stt_final_future.set_result("")
                if self._tracer and self._stt_span:
                    self._tracer.finish_span(self._stt_span, SpanStatus.CANCELLED)
                    self._stt_span = None

        self._stt_task = asyncio.create_task(_consume())

    # ── Pipeline ───────────────────────────────────────────────

    async def _run_pipeline(self) -> None:
        """Main audio receive loop: Transport -> Noise Reduction -> VAD -> STT.

        On STT final -> Agent -> TTS -> Transport audio out.
        """
        try:
            async for chunk in self.transport.receive_audio():
                if not self._is_running:
                    break

                await self.event_bus.emit(AudioIn(chunk=chunk))

                # Stage 1: Noise reduction (optional)
                if self._enable_noise_reduction:
                    if self._tracer and self._trace_context:
                        async with self._tracer.trace(
                            Tracer.NOISE_REDUCTION, self._trace_context
                        ):
                            chunk = await self.noise_reducer.process(chunk)
                    else:
                        chunk = await self.noise_reducer.process(chunk)

                # Stage 2: VAD (optional)
                if self._enable_vad:
                    if self._tracer and self._trace_context:
                        async with self._tracer.trace(Tracer.VAD, self._trace_context):
                            async for vad_event in self.vad.process(chunk):
                                await self.event_bus.emit(vad_event)
                                await self._turn_manager.on_vad_event(vad_event)
                    else:
                        async for vad_event in self.vad.process(chunk):
                            await self.event_bus.emit(vad_event)
                            await self._turn_manager.on_vad_event(vad_event)

                # TurnManager always sees raw audio frames for pre-roll buffering
                self._turn_manager.on_audio_frame(chunk)

                # Stage 3: Feed audio to STT (if listening)
                if self._turn_state == TurnState.LISTENING and self._stt_active:
                    await self.stt.send_audio(chunk)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("Pipeline error")
            await self.event_bus.emit(Error(exception=exc, context="pipeline"))

    async def _handle_end_of_speech(self) -> None:
        """Called when VAD signals end of speech: finalize STT, run agent, synthesize TTS."""
        self._turn_state = TurnState.PROCESSING
        if self._stt_active:
            await self.stt.end_stream()
            self._stt_active = False

        token = self._cancel_token

        transcript = ""
        if self._stt_final_future is not None:
            try:
                if self._timeout_config and self._timeout_config.stt_timeout:
                    transcript = await asyncio.wait_for(
                        self._stt_final_future,
                        timeout=self._timeout_config.stt_timeout,
                    )
                else:
                    transcript = await self._stt_final_future
            except TimeoutError:
                err = STTTimeoutError("stt", self._timeout_config.stt_timeout)
                await self.event_bus.emit(Error(exception=err, context="stt_timeout"))
                if self._tracer and self._stt_span:
                    self._stt_span.set_error(err)
                    self._tracer.finish_span(self._stt_span, SpanStatus.ERROR)
                    self._stt_span = None
                self._turn_state = TurnState.IDLE
                return
            except Exception:
                transcript = ""
            finally:
                self._stt_final_future = None

        if not transcript or (token and token.is_cancelled):
            self._turn_state = TurnState.IDLE
            if self._tracer and self._turn_span:
                self._tracer.finish_span(self._turn_span, SpanStatus.CANCELLED)
                self._turn_span = None
            return

        # Route to streaming or basic agent path
        if hasattr(self.agent, "run_streaming"):
            await self._run_streaming_agent(transcript, token)
        else:
            await self._run_basic_agent(transcript, token)

    # ── Basic agent path ───────────────────────────────────────

    async def _run_basic_agent(self, transcript: str, token: CancelToken | None) -> None:
        """Non-streaming agent path: invoke run(), emit events, synthesize TTS."""
        try:
            if self._tracer and self._trace_context:
                async with self._tracer.trace(Tracer.AGENT, self._trace_context):
                    if self._timeout_config and self._timeout_config.agent_timeout:
                        agent_response = await with_agent_timeout(
                            self.agent.run(transcript),
                            timeout=self._timeout_config.agent_timeout,
                            event_bus=self.event_bus,
                        )
                    else:
                        agent_response = await self.agent.run(transcript)
            else:
                if self._timeout_config and self._timeout_config.agent_timeout:
                    agent_response = await with_agent_timeout(
                        self.agent.run(transcript),
                        timeout=self._timeout_config.agent_timeout,
                        event_bus=self.event_bus,
                    )
                else:
                    agent_response = await self.agent.run(transcript)
        except AgentTimeoutError:
            self._turn_state = TurnState.IDLE
            if self._tracer and self._turn_span:
                self._tracer.finish_span(self._turn_span, SpanStatus.ERROR)
                self._turn_span = None
            return
        except Exception as exc:
            logger.exception("Agent error")
            await self.event_bus.emit(Error(exception=exc, context="agent"))
            self._turn_state = TurnState.IDLE
            if self._tracer and self._turn_span:
                self._turn_span.set_error(exc)
                self._tracer.finish_span(self._turn_span, SpanStatus.ERROR)
                self._turn_span = None
            return

        if token and token.is_cancelled:
            self._turn_state = TurnState.IDLE
            if self._tracer and self._turn_span:
                self._tracer.finish_span(self._turn_span, SpanStatus.CANCELLED)
                self._turn_span = None
            return

        await self.event_bus.emit(AgentDelta(text=agent_response))
        # Expose structured output from adapters that support it, but avoid
        # duplicating plain-text responses in `structured_output`.
        agent_structured = None
        agent_last_output = getattr(self.agent, "last_output", None)
        agent_output_type = getattr(self.agent, "output_type", None)
        if agent_output_type is not None or not isinstance(agent_last_output, str):
            agent_structured = agent_last_output
        await self.event_bus.emit(
            AgentFinal(text=agent_response, structured_output=agent_structured)
        )

        if self._metrics and self._stt_final_time is not None:
            self._metrics.record_latency(
                AGENT_LATENCY,
                (time.monotonic() - self._stt_final_time) * 1000,
            )

        await self._synthesize_tts(agent_response, token)

    # ── Streaming agent path ───────────────────────────────────

    async def _run_streaming_agent(self, transcript: str, token: CancelToken | None) -> None:
        """Streaming agent path with incremental TTS on sentence boundaries.

        Runs agent stream consumption and TTS synthesis concurrently:
        - Agent task: consumes stream events, emits EasyCat events, and queues
          complete sentences for TTS synthesis.
        - TTS task: dequeues text chunks and synthesizes them sequentially.
        """
        tts_queue: asyncio.Queue[str | None] = asyncio.Queue()
        accumulated_text = ""
        structured_output: Any = None
        agent_error: BaseException | None = None
        if self._tracer and self._trace_context:
            self._agent_span = self._tracer.start_span(Tracer.AGENT, self._trace_context)

        async def _consume_agent() -> None:
            nonlocal accumulated_text, structured_output, agent_error
            text_buffer = ""
            try:
                async for event in self.agent.run_streaming(transcript, cancel_token=token):
                    if token and token.is_cancelled:
                        break

                    if event.type == AgentStreamEventType.TEXT_DELTA:
                        accumulated_text += event.text
                        text_buffer += event.text
                        await self.event_bus.emit(AgentDelta(text=event.text))
                        if self._first_agent_time is None:
                            self._first_agent_time = time.monotonic()
                            if self._metrics and self._stt_final_time is not None:
                                self._metrics.record_latency(
                                    AGENT_LATENCY,
                                    (self._first_agent_time - self._stt_final_time) * 1000,
                                )

                        # Flush complete sentences to TTS queue
                        ready, text_buffer = _split_at_sentence_boundaries(text_buffer)
                        if ready:
                            await tts_queue.put(ready)

                    elif event.type == AgentStreamEventType.TOOL_STARTED:
                        await self.event_bus.emit(
                            ToolCallStarted(tool_name=event.tool_name, call_id=event.call_id)
                        )
                    elif event.type == AgentStreamEventType.TOOL_DELTA:
                        await self.event_bus.emit(
                            ToolCallDelta(call_id=event.call_id, delta=event.text)
                        )
                    elif event.type == AgentStreamEventType.TOOL_RESULT:
                        await self.event_bus.emit(
                            ToolCallResult(call_id=event.call_id, result=event.result)
                        )
                    elif event.type == AgentStreamEventType.DONE:
                        if event.text:
                            accumulated_text = event.text
                        if event.structured_output is not None:
                            structured_output = event.structured_output

            except Exception as exc:
                agent_error = exc
                logger.exception("Agent streaming error")
                await self.event_bus.emit(Error(exception=exc, context="agent"))
            finally:
                # Flush any remaining buffered text
                if text_buffer.strip():
                    await tts_queue.put(text_buffer.strip())
                await tts_queue.put(None)  # sentinel to stop TTS task

        async def _process_tts() -> None:
            started = False
            try:
                while True:
                    text = await tts_queue.get()
                    if text is None:
                        break
                    if token and token.is_cancelled:
                        break

                    if not started:
                        self._turn_state = TurnState.BOT_SPEAKING
                        await self._turn_manager.bot_started_speaking()
                        started = True

                    tts_start = time.monotonic()
                    tts_span = None
                    if self._tracer and self._trace_context:
                        tts_span = self._tracer.start_span(Tracer.TTS, self._trace_context)
                    tts_iter = self.tts.synthesize(text)
                    if self._timeout_config and self._timeout_config.tts_first_byte_timeout:
                        tts_iter = with_tts_timeout(
                            tts_iter,
                            timeout=self._timeout_config.tts_first_byte_timeout,
                            provider_name="tts",
                            event_bus=self.event_bus,
                        )
                    try:
                        async for tts_event in tts_iter:
                            if token and token.is_cancelled:
                                break
                            if self._turn_state != TurnState.BOT_SPEAKING:
                                break
                            if tts_event.type == TTSEventType.AUDIO and tts_event.audio:
                                await self.event_bus.emit(TTSAudio(chunk=tts_event.audio))
                                if self._first_tts_audio_time is None:
                                    self._first_tts_audio_time = time.monotonic()
                                    if self._metrics:
                                        self._metrics.record_latency(
                                            TTS_TTFB,
                                            (self._first_tts_audio_time - tts_start) * 1000,
                                        )
                                        if self._turn_end_time is not None:
                                            self._metrics.record_latency(
                                                TURN_E2E,
                                                (self._first_tts_audio_time - self._turn_end_time)
                                                * 1000,
                                            )
                                await self._outbound_queue.put(tts_event.audio)
                            elif tts_event.type == TTSEventType.MARKERS and tts_event.markers:
                                await self.event_bus.emit(TTSMarkers(markers=tts_event.markers))
                    finally:
                        if tts_span and self._tracer:
                            self._tracer.finish_span(tts_span)
            except asyncio.CancelledError:
                pass
            except TTSTimeoutError:
                await self._cancel_tts()
            except Exception:
                logger.exception("TTS streaming error")

            if started and self._turn_state == TurnState.BOT_SPEAKING:
                await self._turn_manager.bot_stopped_speaking()
                self._turn_state = TurnState.IDLE
                if self._tracer and self._turn_span:
                    self._tracer.finish_span(self._turn_span)
                    self._turn_span = None

        # Run agent consumption and TTS synthesis concurrently
        agent_task = asyncio.create_task(_consume_agent())
        tts_task = asyncio.create_task(_process_tts())

        try:
            if self._timeout_config and self._timeout_config.agent_timeout:
                await with_agent_timeout(
                    agent_task,
                    timeout=self._timeout_config.agent_timeout,
                    event_bus=self.event_bus,
                )
            else:
                await agent_task
        except Exception as exc:
            agent_error = exc
            if not agent_task.done():
                agent_task.cancel()
            if not tts_task.done():
                tts_task.cancel()
        finally:
            if self._tracer and self._agent_span:
                if agent_error:
                    self._agent_span.set_error(agent_error)
                    self._tracer.finish_span(self._agent_span, SpanStatus.ERROR)
                else:
                    self._tracer.finish_span(self._agent_span, SpanStatus.OK)
                self._agent_span = None

        # Emit AgentFinal after agent stream is fully consumed
        if accumulated_text and agent_error is None and not (token and token.is_cancelled):
            await self.event_bus.emit(
                AgentFinal(text=accumulated_text, structured_output=structured_output)
            )

        try:
            await tts_task
        except asyncio.CancelledError:
            pass

        # If agent errored or was cancelled with no TTS started, ensure IDLE
        if self._turn_state != TurnState.IDLE:
            self._turn_state = TurnState.IDLE
        if self._tracer and self._turn_span:
            status = SpanStatus.ERROR if agent_error else SpanStatus.OK
            self._tracer.finish_span(self._turn_span, status)
            self._turn_span = None

    # ── TTS synthesis helper ───────────────────────────────────

    async def _synthesize_tts(self, text: str, token: CancelToken | None) -> None:
        """Synthesize TTS for a complete text and emit audio events."""
        self._turn_state = TurnState.BOT_SPEAKING
        await self._turn_manager.bot_started_speaking()
        tts_span = None
        tts_status = SpanStatus.OK
        if self._tracer and self._trace_context:
            tts_span = self._tracer.start_span(Tracer.TTS, self._trace_context)
        try:
            tts_start = time.monotonic()
            tts_iter = self.tts.synthesize(text)
            if self._timeout_config and self._timeout_config.tts_first_byte_timeout:
                tts_iter = with_tts_timeout(
                    tts_iter,
                    timeout=self._timeout_config.tts_first_byte_timeout,
                    provider_name="tts",
                    event_bus=self.event_bus,
                )
            async for tts_event in tts_iter:
                if token and token.is_cancelled:
                    break
                if self._turn_state != TurnState.BOT_SPEAKING:
                    break
                if tts_event.type == TTSEventType.AUDIO and tts_event.audio:
                    await self.event_bus.emit(TTSAudio(chunk=tts_event.audio))
                    if self._first_tts_audio_time is None:
                        self._first_tts_audio_time = time.monotonic()
                        if self._metrics:
                            self._metrics.record_latency(
                                TTS_TTFB,
                                (self._first_tts_audio_time - tts_start) * 1000,
                            )
                            if self._turn_end_time is not None:
                                self._metrics.record_latency(
                                    TURN_E2E,
                                    (self._first_tts_audio_time - self._turn_end_time) * 1000,
                                )
                    await self._outbound_queue.put(tts_event.audio)
                elif tts_event.type == TTSEventType.MARKERS and tts_event.markers:
                    await self.event_bus.emit(TTSMarkers(markers=tts_event.markers))
        except asyncio.CancelledError:
            pass
        except TTSTimeoutError:
            await self._cancel_tts()
            if tts_span:
                timeout = self._timeout_config.tts_first_byte_timeout
                tts_span.set_error(TTSTimeoutError("tts", timeout))
                tts_status = SpanStatus.ERROR
        except Exception as exc:
            if tts_span:
                tts_span.set_error(exc)
                tts_status = SpanStatus.ERROR
            raise
        finally:
            if tts_span and self._tracer:
                self._tracer.finish_span(tts_span, tts_status)

        if self._turn_state == TurnState.BOT_SPEAKING:
            await self._turn_manager.bot_stopped_speaking()
            self._turn_state = TurnState.IDLE
            if self._tracer and self._turn_span:
                self._tracer.finish_span(self._turn_span)
                self._turn_span = None

    # ── Internal helpers ───────────────────────────────────────

    async def _drain_outbound_audio(self) -> None:
        """Send queued outbound audio to the transport with backpressure."""
        while True:
            if not self._is_running and self._outbound_queue.empty():
                break
            try:
                chunk = await self._outbound_queue.get()
            except asyncio.QueueEmpty:
                break
            try:
                await self.transport.send_audio(chunk)
            except Exception:
                logger.exception("Failed to send audio to transport")

    def _maybe_attach_event_bus(self, provider: Any) -> None:
        """Attach the session EventBus to provider configs that support it."""
        cfg = getattr(provider, "_config", None)
        if cfg is None:
            if hasattr(provider, "_event_bus") and getattr(provider, "_event_bus") is None:
                try:
                    setattr(provider, "_event_bus", self.event_bus)
                except Exception:
                    pass
            return
        if hasattr(cfg, "event_bus") and getattr(cfg, "event_bus") is None:
            try:
                setattr(cfg, "event_bus", self.event_bus)
            except Exception:
                pass

    async def _cancel_stt(self) -> None:
        try:
            await self.stt.end_stream()
        except Exception:
            pass
        self._stt_active = False
        if self._stt_task and not self._stt_task.done():
            self._stt_task.cancel()
            try:
                await self._stt_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._stt_final_future and not self._stt_final_future.done():
            self._stt_final_future.set_result("")
        self._stt_final_future = None

    async def _cancel_tts(self) -> None:
        try:
            await self.tts.cancel()
        except Exception:
            pass
        if self._current_tts_task and not self._current_tts_task.done():
            self._current_tts_task.cancel()
            try:
                await self._current_tts_task
            except (asyncio.CancelledError, Exception):
                pass
