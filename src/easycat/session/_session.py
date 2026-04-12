"""Session: the core runtime for a single voice conversation.

Manages the voice pipeline lifecycle, wires provider stages together,
and handles turn state and cancellation. Supports both basic and
streaming agent interfaces with incremental TTS synthesis.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

from easycat.bounded_queue import BoundedAudioQueue, DropPolicy
from easycat.cancel import CancelToken
from easycat.echo_cancellation import PassthroughAEC
from easycat.events import (
    AgentDelta,
    AgentFinal,
    AudioIn,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Error,
    ErrorStage,
    EventBus,
    EventHandler,
    Interruption,
    PlaybackMarkAck,
    SessionActionCompleted,
    SessionActionFailed,
    SessionActionRequested,
    SessionActionStarted,
    STTEventType,
    STTFinal,
    STTPartial,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.health_check import PeriodicHealthChecker
from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.integrations.agents._legacy_types import AgentStreamEventType
from easycat.llm_output_processing import (
    LLMOutputProcessor,
    apply_output_processors,
)
from easycat.noise_reduction import PassthroughNoiseReducer
from easycat.providers import PlaybackAckTransport
from easycat.runtime.journal import ExecutionJournal, JournalView
from easycat.runtime.records import JournalRecordKind
from easycat.session._interruption import estimate_and_notify_interruption
from easycat.session._streaming import consume_agent_stream
from easycat.session._text_utils import (
    _chunk_has_speech_energy,
    _replace_last_assistant_text,
)
from easycat.session._tts_helpers import _text_for_estimation_timeline
from easycat.session._turn_context import TurnContext
from easycat.session._types import (
    _TM_TO_TURN_STATE,
    Agent,
    SessionConfig,
    SessionHelper,
    TurnState,
)
from easycat.session.action_executors import CoreSessionActionExecutor
from easycat.session.actions import SessionAction, SessionActionExecutor
from easycat.strip_markdown import strip_markdown
from easycat.stubs import (
    NoopAgent,
    NoopSTT,
    NoopTransport,
    NoopTTS,
    NoopVAD,
)
from easycat.timeouts import (
    AgentTimeoutError,
    STTTimeoutError,
    TTSTimeoutError,
    with_agent_timeout,
)
from easycat.tts.input import TTSInput, strip_ssml_tags
from easycat.tts_synthesizer import TTSSynthesizer
from easycat.turn_manager import TurnManager, TurnManagerState

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ActionDrainOutcome:
    stop_session: bool = False


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
        self._config = cfg

        # Providers (fall back to no-op stubs)
        self.stt = cfg.stt or NoopSTT()
        self.tts = cfg.tts or NoopTTS()
        self.vad = cfg.vad or NoopVAD()
        self.noise_reducer = cfg.noise_reducer or PassthroughNoiseReducer()
        self.echo_canceller = cfg.echo_canceller or PassthroughAEC()
        self.transport = cfg.transport or NoopTransport()
        self.agent: Agent = auto_adapt_agent(cfg.agent) if cfg.agent else NoopAgent()

        # Skip noop validation in text_session mode — audio providers
        # are intentionally noop.
        if cfg.runtime_mode != "text_session":
            noops = []
            if isinstance(self.stt, NoopSTT):
                noops.append("stt")
            if isinstance(self.tts, NoopTTS):
                noops.append("tts")
            if cfg.enable_vad and isinstance(self.vad, NoopVAD):
                noops.append("vad")
            is_passthrough_nr = isinstance(self.noise_reducer, PassthroughNoiseReducer)
            if is_passthrough_nr and cfg.enable_noise_reduction:
                noops.append("noise_reducer")
            if isinstance(self.transport, NoopTransport):
                noops.append("transport")
            if isinstance(self.agent, NoopAgent):
                noops.append("agent")
            if noops:
                raise ValueError(
                    "SessionConfig must provide non-noop implementations for: " + ", ".join(noops)
                )

        # Event system
        self.event_bus = cfg.event_bus or EventBus()

        # Attach event bus to providers that accept it
        self._maybe_attach_event_bus(self.stt)
        self._maybe_attach_event_bus(self.tts)
        self._maybe_attach_event_bus(self.transport)

        # Pipeline flags
        self._enable_noise_reduction = cfg.enable_noise_reduction
        self._enable_aec = cfg.enable_echo_cancellation and not isinstance(
            self.echo_canceller, PassthroughAEC
        )
        self._enable_vad = cfg.enable_vad
        self._auto_turn_from_stt_final = cfg.auto_turn_from_stt_final
        self._interruption_mode = cfg.interruption_mode
        self._interruption_latency_compensation_ms = max(
            0, cfg.interruption_latency_compensation_ms
        )
        self._interruption_ack_stale_ms = max(0, cfg.interruption_ack_stale_ms)
        self._interruption_ack_tail_cap_ms = max(0, cfg.interruption_ack_tail_cap_ms)
        self._strip_markdown = cfg.strip_markdown
        self._output_processors: list[LLMOutputProcessor] = list(cfg.output_processors)

        # Turn manager — single source of truth for turn state
        self._turn_manager = cfg.turn_manager or TurnManager(
            self.event_bus,
            config=cfg.turn_manager_config,
            cancel_turn_callback=self._cancel_for_barge_in,
        )
        self.event_bus.subscribe(TurnStarted, self._on_turn_started)
        self.event_bus.subscribe(TurnEnded, self._schedule_turn_ended)
        self.event_bus.subscribe(PlaybackMarkAck, self._on_playback_mark_ack)

        # Reliability/observability config
        self._timeout_config = cfg.timeout_config or self._default_timeout_config()
        self._metrics = None  # Legacy field, retained for attribute access only
        self._journal = cfg.journal
        self._artifact_store = cfg.artifact_store

        # Wire journal to event bus so session activity is recorded.
        if self._journal is not None:
            self._subscribe_journal_sink(self._journal)

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
        self._tts_synth = TTSSynthesizer(
            tts=self.tts,
            event_bus=self.event_bus,
            outbound_queue=self._outbound_queue,
            timeout_config=self._timeout_config,
            correlation_ids=lambda: (
                self.session_id,
                self._turn.id
                if self._turn and self._turn_manager.state != TurnManagerState.IDLE
                else None,
            ),
            audio_gate=cfg.audio_gate,
        )
        self._audio_gate = cfg.audio_gate
        self._health_checkers: list[PeriodicHealthChecker] = []
        self._telephony_helpers: list[SessionHelper] = list(cfg.telephony_helpers)

        # Agent-initiated session actions
        self._session_actions = cfg.session_actions
        self._action_executors: list[SessionActionExecutor] = [
            *cfg.action_executors,
            CoreSessionActionExecutor(),
        ]

        # State
        self._is_running = False
        self._closed = False
        self._flushed = False
        self._pipeline_task: asyncio.Task[None] | None = None
        self._stt_task: asyncio.Task[None] | None = None
        self._current_tts_task: asyncio.Task[None] | None = None
        self._stt_final_future: asyncio.Future[str] | None = None

        # STT stream started for current turn
        self._stt_active = False
        self._tts_playback_suppressed = False
        self._auto_turn_speech_frames = 0

        # Per-turn state — created fresh at each turn start
        self._turn: TurnContext | None = None
        self._replay_chunks_pending: int = 0
        self._playback_mark_bytes_interval: int = 4_000  # throttle: ~125ms at 16kHz/16-bit
        self._playback_mark_seq: int = 0  # session-scoped so mark names never collide across turns

        self._playback_ack_transport: PlaybackAckTransport | None = None
        if isinstance(self.transport, PlaybackAckTransport):
            self._playback_ack_transport = self.transport

        self.session_id = cfg.session_id or f"session-{uuid4().hex[:12]}"
        self._runtime_mode = cfg.runtime_mode
        self._text_turn_lock = asyncio.Lock()
        self._turn_manager.bind_session(self.session_id)

        # Backfill journal/session-id into bridge adapter shims so that the
        # direct Session(SessionConfig(...)) construction path produces the
        # same observability data as create_session().
        self._backfill_bridge_context()

    @staticmethod
    def _default_timeout_config():
        from easycat.timeouts import TimeoutConfig

        return TimeoutConfig()

    def _backfill_bridge_context(self) -> None:
        """Inject journal/session-id into bridge shims attached to this session.

        ``create_session()`` already does this, but callers using the direct
        ``Session(SessionConfig(...))`` path would otherwise get shims with
        ``_journal=None`` / ``_session_id=""``, producing no bridge-level
        journal records.  This method is idempotent — re-injecting the same
        values is harmless.
        """
        from easycat.integrations.agents._agent_runner import AgentRunner
        from easycat.integrations.agents._bridge_adapter_shim import BridgeAdapterShim

        shim = self.agent
        if isinstance(shim, AgentRunner):
            shim = shim._agent
        if isinstance(shim, BridgeAdapterShim):
            if self._journal is not None:
                shim._journal = self._journal
            if self._artifact_store is not None:
                shim._artifact_store = self._artifact_store
            shim._session_id = self.session_id

    def _with_correlation(self, event: Any) -> Any:
        """Attach session/turn identifiers to events when supported."""
        if not hasattr(event, "session_id") and not hasattr(event, "turn_id"):
            return event
        kwargs: dict[str, Any] = {}
        if hasattr(event, "session_id") and getattr(event, "session_id", None) is None:
            kwargs["session_id"] = self.session_id
        if hasattr(event, "turn_id") and getattr(event, "turn_id", None) is None:
            # Only stamp a turn_id when the turn manager is actively in a
            # turn.  In the gated-TTS path self._turn is kept alive after the
            # turn manager resets to IDLE for playback-mark bookkeeping, but
            # events emitted during that window (AudioIn, VAD, etc.) should
            # not carry the old turn's ID.
            active_turn = (
                self._turn
                if self._turn and self._turn_manager.state != TurnManagerState.IDLE
                else None
            )
            kwargs["turn_id"] = active_turn.id if active_turn else None
        return replace(event, **kwargs) if kwargs else event

    async def _emit(self, event: Any) -> None:
        await self.event_bus.emit(self._with_correlation(event))

    def _subscribe_journal_sink(self, journal: ExecutionJournal) -> None:
        """Subscribe event bus handlers that write session events to the journal."""
        from easycat.runtime.records import ErrorInfo

        session = self
        EVT = JournalRecordKind.EVENT
        CTL = JournalRecordKind.CONTROL

        def _make(kind: JournalRecordKind, name: str):
            def _handler(event: Any) -> None:
                data: dict[str, Any] = {}
                for attr in ("text", "track", "result", "tool_name", "call_id", "delta"):
                    val = getattr(event, attr, None)
                    if val is not None:
                        data[attr] = val
                error = None
                exc = getattr(event, "exception", None)
                if exc is not None:
                    stage = getattr(event, "stage", None)
                    if hasattr(stage, "value"):
                        data["stage"] = stage.value
                    error = ErrorInfo.from_exception(exc)
                journal.append(
                    kind=kind,
                    name=name,
                    session_id=session.session_id,
                    turn_id=getattr(event, "turn_id", None),
                    data=data or None,
                    error=error,
                )

            return _handler

        _sub = self.event_bus.subscribe
        _sub(TurnStarted, _make(EVT, "turn_started"))
        _sub(TurnEnded, _make(EVT, "turn_ended"))
        _sub(VADStartSpeaking, _make(EVT, "vad_start_speaking"))
        _sub(VADStopSpeaking, _make(EVT, "vad_stop_speaking"))
        _sub(STTPartial, _make(EVT, "stt_partial"))
        _sub(STTFinal, _make(EVT, "stt_final"))
        _sub(AgentDelta, _make(EVT, "agent_delta"))
        _sub(AgentFinal, _make(EVT, "agent_final"))
        _sub(BotStartedSpeaking, _make(EVT, "bot_started_speaking"))
        _sub(BotStoppedSpeaking, _make(EVT, "bot_stopped_speaking"))
        _sub(Interruption, _make(CTL, "interruption"))
        _sub(Error, _make(EVT, "error"))
        _sub(ToolCallStarted, _make(EVT, "tool_call_started"))
        _sub(ToolCallDelta, _make(EVT, "tool_call_delta"))
        _sub(ToolCallResult, _make(EVT, "tool_call_result"))

    def _reset_turn_state(self) -> None:
        """Clear turn correlation state and reset the turn manager."""
        self._turn = None
        self._auto_turn_speech_frames = 0
        self._replay_chunks_pending = 0
        self._turn_manager.reset()

    @property
    def _is_gated(self) -> bool:
        """Whether the classification gate is currently buffering TTS audio."""
        return self._audio_gate is not None and self._audio_gate()

    # ── Properties ─────────────────────────────────────────────

    def subscribe_event(self, event_type: type, handler: EventHandler) -> None:
        """Subscribe to a session event via the underlying EventBus."""
        self.event_bus.subscribe(event_type, handler)

    def subscribe_events(
        self, event_types: tuple[type, ...] | list[type], handler: EventHandler
    ) -> list[tuple[type, EventHandler]]:
        """Subscribe a single handler to multiple event types at once.

        Accepts any of the event group tuples from :mod:`easycat.events`
        (e.g. ``ALL_EVENTS``, ``STT_EVENTS``) or an ad-hoc sequence.

        Returns a list of ``(event_type, handler)`` registrations that can be
        passed to :meth:`unsubscribe_handlers`.
        """
        registrations: list[tuple[type, EventHandler]] = []
        for event_type in event_types:
            self.event_bus.subscribe(event_type, handler)
            registrations.append((event_type, handler))
        return registrations

    def unsubscribe_event(self, event_type: type, handler: EventHandler) -> None:
        """Unsubscribe a handler previously attached with ``subscribe_event``."""
        self.event_bus.unsubscribe(event_type, handler)

    def subscribe_agent_events(
        self,
        *,
        on_delta: EventHandler | None = None,
        on_final: EventHandler | None = None,
        on_tool_started: EventHandler | None = None,
        on_tool_delta: EventHandler | None = None,
        on_tool_result: EventHandler | None = None,
    ) -> list[tuple[type, EventHandler]]:
        """Subscribe handlers for agent and tool-call events in one call.

        Returns a list of ``(event_type, handler)`` registrations that can be
        passed to :meth:`unsubscribe_handlers`.
        """
        registrations: list[tuple[type, EventHandler]] = []

        for event_type, handler in (
            (AgentDelta, on_delta),
            (AgentFinal, on_final),
            (ToolCallStarted, on_tool_started),
            (ToolCallDelta, on_tool_delta),
            (ToolCallResult, on_tool_result),
        ):
            if handler is None:
                continue
            self.event_bus.subscribe(event_type, handler)
            registrations.append((event_type, handler))

        return registrations

    def on(
        self,
        *,
        user_started_speaking: Callable[[], Any] | None = None,
        user_stopped_speaking: Callable[[], Any] | None = None,
        user_transcript: Callable[[str], Any] | None = None,
        agent_delta: Callable[[str], Any] | None = None,
        agent_response: Callable[[str], Any] | None = None,
        tool_started: Callable[[str, str], Any] | None = None,
        tool_result: Callable[[str, str], Any] | None = None,
        turn_started: Callable[[], Any] | None = None,
        turn_ended: Callable[[], Any] | None = None,
        bot_started_speaking: Callable[[], Any] | None = None,
        bot_stopped_speaking: Callable[[], Any] | None = None,
        interruption: Callable[[], Any] | None = None,
        error: Callable[[BaseException, str], Any] | None = None,
    ) -> list[tuple[type, EventHandler]]:
        """Subscribe to common session events with simple callbacks.

        Each callback receives only the most useful fields — no event type
        imports needed.  Pass only the callbacks you care about::

            session.on(
                user_transcript=lambda text: print(f"User: {text}"),
                agent_response=lambda text: print(f"Bot: {text}"),
                interruption=lambda: print("Interrupted!"),
            )

        Returns registrations that can be passed to :meth:`unsubscribe_handlers`.
        """
        _mappings: list[tuple[type, Any, Callable[..., EventHandler]]] = [
            (VADStartSpeaking, user_started_speaking, lambda cb: lambda _e: cb()),
            (VADStopSpeaking, user_stopped_speaking, lambda cb: lambda _e: cb()),
            (STTFinal, user_transcript, lambda cb: lambda e: cb(e.text)),
            (AgentDelta, agent_delta, lambda cb: lambda e: cb(e.text)),
            (AgentFinal, agent_response, lambda cb: lambda e: cb(e.text)),
            (ToolCallStarted, tool_started, lambda cb: lambda e: cb(e.tool_name, e.call_id)),
            (ToolCallResult, tool_result, lambda cb: lambda e: cb(e.call_id, e.result)),
            (TurnStarted, turn_started, lambda cb: lambda _e: cb()),
            (TurnEnded, turn_ended, lambda cb: lambda _e: cb()),
            (BotStartedSpeaking, bot_started_speaking, lambda cb: lambda _e: cb()),
            (BotStoppedSpeaking, bot_stopped_speaking, lambda cb: lambda _e: cb()),
            (Interruption, interruption, lambda cb: lambda _e: cb()),
            (
                Error,
                error,
                lambda cb: (
                    lambda e: cb(
                        e.exception,
                        f"{e.stage.value}:{e.provider}" if e.provider else e.stage.value,
                    )
                ),
            ),
        ]

        registrations: list[tuple[type, EventHandler]] = []
        for event_type, cb, wrap in _mappings:
            if cb is None:
                continue
            handler = wrap(cb)
            self.event_bus.subscribe(event_type, handler)
            registrations.append((event_type, handler))
        return registrations

    def unsubscribe_handlers(self, registrations: list[tuple[type, EventHandler]]) -> None:
        """Unsubscribe a batch of event handlers from prior registrations."""
        for event_type, handler in registrations:
            self.event_bus.unsubscribe(event_type, handler)

    def export_debug_bundle(
        self,
        path: str,
        *,
        inline_artifacts: bool = False,
        overwrite: bool = False,
    ) -> None:
        """Export a debug bundle from this session.

        Delegates to :func:`easycat.debug.export.export_debug_bundle`.
        """
        from easycat.debug.export import export_debug_bundle

        export_debug_bundle(
            self,
            path,
            inline_artifacts=inline_artifacts,
            overwrite=overwrite,
        )

    @property
    def turn_state(self) -> TurnState:
        """Session-level turn state, derived from the TurnManager."""
        return _TM_TO_TURN_STATE.get(self._turn_manager.state, TurnState.IDLE)

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def is_speaking(self) -> bool:
        return self._turn_manager.state in (
            TurnManagerState.USER_SPEAKING,
            TurnManagerState.USER_PAUSED,
        )

    @property
    def is_bot_speaking(self) -> bool:
        return self._turn_manager.state == TurnManagerState.BOT_SPEAKING

    @property
    def journal(self) -> JournalView | None:
        """Read-only journal view, or ``None`` when journaling is disabled."""
        if self._journal is None:
            return None
        return JournalView(self._journal)

    @property
    def cancel_token(self) -> CancelToken | None:
        return self._turn.cancel_token if self._turn else None

    async def replay_gated_audio(self, events: list[Any]) -> None:
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

    async def synthesize_bypass(self, text: str) -> None:
        """Synthesize text via TTS, bypassing the classification gate.

        Used for hold audio and screening responses that must reach the
        transport even while the gate is closed.
        """
        await self._tts_synth.synthesize(text, token=None, bypass_gate=True)

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize providers and begin the audio receive loop."""
        if self._runtime_mode == "text_session":
            raise RuntimeError(
                "start() is not supported for text sessions. Use send_text() instead."
            )
        if self._closed:
            raise RuntimeError(
                "Session has been stopped and cannot be restarted. Create a new Session."
            )
        if self._is_running:
            return
        transport_connected = False
        self._health_checkers = []

        try:
            await self.transport.connect()
            transport_connected = True

            if not self._outbound_queue_external:
                self._outbound_queue = BoundedAudioQueue(
                    max_size=self._outbound_queue_max_size,
                    policy=self._outbound_queue_policy,
                    name=self._outbound_queue_name,
                )
                self._tts_synth._outbound_queue = self._outbound_queue

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

            for helper in self._telephony_helpers:
                helper.start()

            self._is_running = True
            self._outbound_task = asyncio.create_task(self._drain_outbound_audio())
            self._pipeline_task = asyncio.create_task(self._run_pipeline())
        except Exception:
            self._is_running = False

            for task_name in ("_pipeline_task", "_outbound_task"):
                task = getattr(self, task_name)
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                setattr(self, task_name, None)

            for checker in self._health_checkers:
                await checker.stop()
            self._health_checkers = []

            self._stop_helpers()
            self._reset_turn_state()

            if transport_connected:
                await self.transport.disconnect()
            raise

    async def stop(self) -> None:
        """Gracefully stop the session: finish current turn, close providers."""
        if self._closed:
            return
        self._closed = True
        self._is_running = False
        current_task = asyncio.current_task()

        if self._turn:
            self._turn.cancel_token.cancel()

        # Always perform cleanup — even when _run_pipeline() already flipped
        # _is_running to False (e.g. after a transport disconnect).  Each step
        # is individually guarded and safe to call when no work was started.
        if (
            self._pipeline_task
            and self._pipeline_task is not current_task
            and not self._pipeline_task.done()
        ):
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
        self._stop_helpers()
        if not self._outbound_queue_external:
            self._outbound_queue.close()
        if self._outbound_task and not self._outbound_task.done():
            self._outbound_task.cancel()
            try:
                await self._outbound_task
            except asyncio.CancelledError:
                pass
        await self.transport.disconnect()
        await self._turn_manager.shutdown()
        if hasattr(self.agent, "aclose"):
            try:
                await self.agent.aclose()
            except Exception:
                pass
        self._turn = None
        self.close()

    async def shutdown(self) -> None:
        """Force-close everything and release resources."""
        if self._closed:
            return
        self._closed = True
        self._is_running = False

        if self._turn:
            self._turn.cancel_token.cancel()

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
        self._stop_helpers()
        if not self._outbound_queue_external:
            self._outbound_queue.close()
        await self.transport.disconnect()
        await self._turn_manager.shutdown()
        if hasattr(self.agent, "aclose"):
            try:
                await self.agent.aclose()
            except Exception:
                pass
        self._turn = None
        self.close()

    def close(self) -> None:
        """Finalize journal and artifact store resources.

        Called automatically by ``stop()`` and ``shutdown()``.
        Safe to call multiple times.  References are preserved so
        callers can still inspect ``session.journal`` and call
        ``session.export_debug_bundle()`` after the session stops.

        Writes the clean-close marker and runs retention but does
        **not** close the underlying backends so that post-stop reads
        (e.g. ``export_debug_bundle``) still work.
        Call :meth:`destroy` to release connections and free memory.
        """
        if self._flushed:
            return
        self._flushed = True
        if self._journal:
            self._journal.finalize()

    def destroy(self) -> None:
        """Close journal and artifact store backends, releasing resources.

        After this call ``export_debug_bundle()`` will no longer work.
        Safe to call multiple times.
        """
        self.close()  # ensure flush happened
        if self._journal:
            self._journal.close()
        if self._artifact_store:
            self._artifact_store.close()

    # ── Cancellation ───────────────────────────────────────────

    async def cancel_turn(self, *, barge_in: bool = False) -> None:
        """Trigger cancel token, abort STT/agent/TTS, reset turn state.

        If barge_in is True, emits an Interruption event.
        """
        if self._turn:
            self._turn.cancel_token.cancel()

        if barge_in:
            if self._turn:
                self._turn.record_barge_in()
            await self._emit(Interruption())

        await self._cancel_stt()
        await self._cancel_tts()
        await self.transport.clear_audio()
        self._outbound_queue.flush_for_new_turn()
        self._replay_chunks_pending = 0

        if not barge_in:
            self._reset_turn_state()

    async def cancel_tts_playback(self) -> None:
        """Stop TTS provider and flush outbound audio.

        Unlike :meth:`cancel_turn`, this does NOT cancel the shared
        ``cancel_token`` so any in-flight agent stream can continue
        producing text (which will simply not be synthesized).

        Importantly, this does NOT cancel ``_current_tts_task`` — that
        task is the entire ``_on_turn_ended`` coroutine which includes
        the agent consumer.  Cancelling it would abort the agent stream.
        """
        self._tts_playback_suppressed = True
        await self._tts_synth.cancel()
        await self.transport.clear_audio()
        self._outbound_queue.flush_for_new_turn()
        self._replay_chunks_pending = 0
        if self._turn_manager.state == TurnManagerState.BOT_SPEAKING:
            self._reset_turn_state()

    async def reset_state(self) -> None:
        """Cancel everything and return to idle/listening state.

        Also clears agent conversation history if the agent supports it.
        """
        if self._turn:
            self._turn.cancel_token.cancel()

        await self._cancel_stt()
        await self._cancel_tts()
        await self.transport.clear_audio()
        self._outbound_queue.flush_for_new_turn()
        self._replay_chunks_pending = 0

        if hasattr(self.agent, "clear_history"):
            self.agent.clear_history()

        self._reset_turn_state()

    # ── Session actions ───────────────────────────────────────

    def register_action_executor(self, executor: SessionActionExecutor) -> None:
        """Register a session action executor.

        Executors are tried in the order they were registered. The first
        executor whose ``supports(...)`` method returns true handles the action.
        """
        self._action_executors.insert(0, executor)

    async def _drain_session_actions(self) -> _ActionDrainOutcome:
        """Execute any session actions queued by agent tools during this turn."""
        outcome = _ActionDrainOutcome()
        if self._session_actions is None or not self._session_actions.has_pending:
            return outcome

        actions = self._session_actions.drain()
        for action in actions:
            await self._emit(SessionActionRequested(action=action))
            executor = self._find_action_executor(action)
            if executor is None:
                error = f"No session action executor for {action.type}"
                logger.warning(error)
                await self._emit(SessionActionFailed(action=action, error=error))
                continue

            executor_name = type(executor).__name__
            await self._emit(SessionActionStarted(action=action, executor=executor_name))
            try:
                result = await executor.execute(self, action)
            except Exception as exc:
                logger.exception("Session action executor failed: %s", action.type)
                await self._emit(
                    SessionActionFailed(
                        action=action,
                        executor=executor_name,
                        error=str(exc),
                    )
                )
                continue

            outcome.stop_session = outcome.stop_session or result.stop_session
            await self._emit(
                SessionActionCompleted(
                    action=action,
                    executor=executor_name,
                    result=result,
                )
            )

        return outcome

    def _find_action_executor(self, action: SessionAction) -> SessionActionExecutor | None:
        for executor in self._action_executors:
            if executor.supports(action):
                return executor
        return None

    # ── Push-to-talk helpers ───────────────────────────────────

    async def start_turn(self) -> None:
        """Manually start a user turn (push-to-talk mode)."""
        await self._turn_manager.start_turn()

    async def end_turn(self) -> None:
        """Manually end the current user turn (push-to-talk mode)."""
        await self._turn_manager.end_turn()

    # ── TurnManager callbacks ──────────────────────────────────

    async def _cancel_for_barge_in(self) -> bool:
        """Cancel current turn due to barge-in (called by TurnManager).

        Returns ``False`` when barge-in is suppressed so the TurnManager
        skips starting a new user turn.

        When a queued session action has ``no_interrupt=True`` (e.g. an
        end-call or transfer announcement), barge-in is
        suppressed so the critical speech plays in full.
        """
        if self._session_actions is not None and self._session_actions.no_interrupt:
            logger.debug("Barge-in suppressed: queued action has no_interrupt=True")
            return False
        await self.cancel_turn(barge_in=True)
        return True

    async def _on_turn_started(self, event: TurnStarted) -> None:
        """Handle TurnStarted from TurnManager: start STT and prime pre-roll."""
        if not self._is_running:
            return

        cancel_token = self._turn_manager.cancel_token or CancelToken()
        self._turn = TurnContext(turn_id=event.turn_id, cancel_token=cancel_token)
        self._auto_turn_speech_frames = 0
        self._tts_playback_suppressed = False

        # Start STT stream
        try:
            await self.stt.start_stream()
            self._stt_active = True
            self._start_stt_event_task()
        except Exception as exc:
            logger.exception("Failed to start STT stream")
            await self._emit(Error(exception=exc, stage=ErrorStage.STT))
            self._stt_active = False
            return

        # Prime STT with pre-roll frames captured by TurnManager
        for chunk in self._turn_manager.turn_audio:
            await self.stt.send_audio(chunk)

    def _stop_helpers(self) -> None:
        """Stop attached helper components that own event subscriptions/state."""
        for helper in self._telephony_helpers:
            try:
                helper.stop()
            except Exception:
                logger.debug("Error stopping session helper", exc_info=True)

    def _schedule_turn_ended(self, event: TurnEnded) -> None:
        """Schedule end-of-turn processing without blocking other handlers."""
        if self._current_tts_task and not self._current_tts_task.done():
            self._current_tts_task.cancel()
        self._current_tts_task = asyncio.create_task(self._on_turn_ended(event))
        self._current_tts_task.add_done_callback(self._log_task_exception)

    async def _on_turn_ended(self, event: TurnEnded) -> None:
        """Handle TurnEnded from TurnManager: finalize STT and run agent/TTS."""
        if self._turn and self._turn.cancel_token.is_cancelled:
            return
        if self._turn_manager.state != TurnManagerState.PROCESSING:
            return
        if self._turn:
            self._turn.end_time = event.timestamp
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
            saw_final = False
            turn = self._turn
            try:
                async for stt_event in self.stt.events():
                    if turn and turn.cancel_token.is_cancelled:
                        break
                    if stt_event.type == STTEventType.PARTIAL:
                        await self._emit(STTPartial(text=stt_event.text, track=stt_event.track))
                    elif stt_event.type == STTEventType.FINAL:
                        saw_final = True
                        await self._emit(STTFinal(text=stt_event.text, track=stt_event.track))
                        if turn:
                            turn.stt_final_time = time.monotonic()
                        if self._stt_final_future and not self._stt_final_future.done():
                            self._stt_final_future.set_result(stt_event.text)
                        if self._auto_turn_from_stt_final:
                            await self._turn_manager.end_turn()
                        break
            except Exception as exc:
                logger.exception("STT event loop error")
                await self._emit(Error(exception=exc, stage=ErrorStage.STT))
                if self._stt_final_future and not self._stt_final_future.done():
                    self._stt_final_future.set_result("")
            finally:
                if self._stt_final_future and not self._stt_final_future.done():
                    self._stt_final_future.set_result("")
                if not saw_final:
                    pass

        self._stt_task = asyncio.create_task(_consume())

    # ── Pipeline ───────────────────────────────────────────────

    async def _run_pipeline(self) -> None:
        """Main audio receive loop: Transport -> Noise Reduction -> AEC -> VAD -> STT."""
        try:
            async for chunk in self.transport.receive_audio():
                if not self._is_running:
                    break

                await self._emit(AudioIn(chunk=chunk))

                # Stage 1: Noise reduction (optional)
                if self._enable_noise_reduction:
                    chunk = await self.noise_reducer.process(chunk)

                # Stage 2: Echo cancellation (optional)
                if self._enable_aec:
                    chunk = await self.echo_canceller.process(chunk)

                # Stage 3: VAD (optional)
                if self._enable_vad:
                    async for vad_event in self.vad.process(chunk):
                        vad_event = self._with_correlation(vad_event)
                        await self._emit(vad_event)
                        await self._turn_manager.on_vad_event(vad_event)

                # TurnManager always sees raw audio frames for pre-roll buffering
                self._turn_manager.on_audio_frame(chunk)

                # Stage 4: Feed audio to STT (if listening)
                started_turn_from_chunk = False
                if self._auto_turn_from_stt_final and not self._stt_active:
                    if self._turn_manager.state == TurnManagerState.IDLE:
                        if _chunk_has_speech_energy(chunk):
                            self._auto_turn_speech_frames += 1
                        else:
                            self._auto_turn_speech_frames = 0

                        if self._auto_turn_speech_frames >= 2:
                            await self._turn_manager.start_turn()
                            self._auto_turn_speech_frames = 0
                            started_turn_from_chunk = self._stt_active
                    else:
                        self._auto_turn_speech_frames = 0

                if self.is_speaking and self._stt_active and not started_turn_from_chunk:
                    await self.stt.send_audio(chunk)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("Pipeline error")
            await self._emit(Error(exception=exc, stage=ErrorStage.PIPELINE))
        finally:
            # When the pipeline exits (transport disconnect, cancellation, or
            # error), mark the session as no longer running so callers polling
            # ``is_running`` can detect the transport is gone.
            #
            # We do NOT close the outbound queue here — an in-flight turn
            # (agent + TTS) may still be producing audio that needs to drain.
            # Instead we just flip the flag; ``stop()`` handles full cleanup.
            if self._is_running:
                logger.debug("Pipeline exited while session was running; marking session stopped")
                self._is_running = False

    async def _handle_end_of_speech(self) -> None:
        """Called when VAD signals end of speech: finalize STT, run agent, synthesize TTS."""
        if self._stt_active:
            await self.stt.end_stream()
            self._stt_active = False

        turn = self._turn
        token = turn.cancel_token if turn else None

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
                await self._emit(Error(exception=err, stage=ErrorStage.STT))
                if self._turn is turn:
                    self._reset_turn_state()
                return
            except Exception:
                transcript = ""
            finally:
                self._stt_final_future = None

        if not transcript or (token and token.is_cancelled):
            if self._turn is turn:
                self._reset_turn_state()
            return

        # Pass the active turn ID to bridge-backed agents so their journal
        # records share the same turn_id as the rest of the session.
        turn_id = turn.id if turn else None
        _set_fn = getattr(self.agent, "set_active_turn_id", None)
        if callable(_set_fn) and turn_id:
            _set_fn(turn_id)

        # Route to streaming or basic agent path
        if hasattr(self.agent, "run_streaming"):
            await self._run_streaming_agent(transcript, token)
        else:
            await self._run_basic_agent(transcript, token)

    # ── Agent invocation helper ────────────────────────────────

    async def _invoke_agent(self, transcript: str) -> str:
        """Invoke the basic agent with optional timeout. Returns the response."""
        if self._timeout_config and self._timeout_config.agent_timeout:
            return await with_agent_timeout(
                self.agent.run(transcript),
                timeout=self._timeout_config.agent_timeout,
                event_bus=self.event_bus,
            )
        return await self.agent.run(transcript)

    # ── Basic agent path ───────────────────────────────────────

    async def _run_basic_agent(self, transcript: str, token: CancelToken | None) -> None:
        """Non-streaming agent path: invoke run(), emit events, synthesize TTS."""
        turn = self._turn
        try:
            agent_response = await self._invoke_agent(transcript)
        except asyncio.CancelledError:
            raise
        except AgentTimeoutError:
            if self._turn is turn:
                self._reset_turn_state()
            return
        except Exception as exc:
            logger.exception("Agent error")
            await self._emit(Error(exception=exc, stage=ErrorStage.AGENT))
            if self._turn is turn:
                self._reset_turn_state()
            return

        if token and token.is_cancelled:
            if self._turn is turn:
                self._reset_turn_state()
            return

        if self._strip_markdown:
            stripped = strip_markdown(agent_response, normalize_code_spans=True)
            if stripped != agent_response:
                agent_response = stripped
                _replace_last_assistant_text(self.agent, stripped)

        await self._emit(AgentDelta(text=agent_response))
        agent_structured = None
        agent_last_output = getattr(self.agent, "last_output", None)
        agent_output_type = getattr(self.agent, "output_type", None)
        if agent_output_type is not None or not isinstance(agent_last_output, str):
            agent_structured = agent_last_output
        await self._emit(AgentFinal(text=agent_response, structured_output=agent_structured))

        action_outcome = await self._synthesize_tts(
            self._prepare_tts_payload(agent_response, is_streaming=False, is_final=True), token
        )
        if action_outcome.stop_session:
            await self.stop()

    # ── Streaming agent path ───────────────────────────────────

    async def _run_streaming_agent(self, transcript: str, token: CancelToken | None) -> None:
        """Streaming agent path with incremental TTS on sentence boundaries.

        Uses :func:`consume_agent_stream` to translate agent events into
        TTS payloads, and runs TTS synthesis concurrently.
        """
        turn = self._turn
        assert turn is not None
        tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()
        tts_playback_started = False
        tts_chunks: list[tuple[str, int, bool]] = []
        tts_action_outcome = _ActionDrainOutcome()

        # ── TTS consumer task ──

        async def _process_tts() -> None:
            nonlocal tts_action_outcome
            nonlocal tts_playback_started
            started = False
            try:
                while True:
                    payload = await tts_queue.get()
                    if payload is None:
                        break
                    if token and token.is_cancelled:
                        tts_chunks.append((_text_for_estimation_timeline(payload), 0, False))
                        break
                    if self._tts_playback_suppressed:
                        tts_chunks.append((_text_for_estimation_timeline(payload), 0, False))
                        break

                    if not started:
                        gated = self._is_gated
                        if not gated:
                            await self._turn_manager.bot_started_speaking()
                            tts_playback_started = True
                        started = True

                    result = await self._tts_synth.synthesize(
                        payload,
                        token,
                        turn_end_time=turn.end_time,
                        is_active=(
                            None
                            if self._is_gated
                            else lambda: self._turn_manager.state == TurnManagerState.BOT_SPEAKING
                        ),
                        record_latency=turn.first_tts_audio_time is None,
                    )
                    tts_chunks.append(
                        (
                            _text_for_estimation_timeline(payload),
                            result.audio_bytes,
                            result.completed,
                        )
                    )
                    if result.first_audio_time is not None and turn.first_tts_audio_time is None:
                        turn.first_tts_audio_time = result.first_audio_time
            except asyncio.CancelledError:
                pass
            except TTSTimeoutError:
                await self._cancel_tts()
            except Exception:
                logger.exception("TTS streaming error")

            while not tts_queue.empty():
                remaining = tts_queue.get_nowait()
                if remaining is not None:
                    tts_chunks.append((_text_for_estimation_timeline(remaining), 0, False))

            if started and self._turn_manager.state == TurnManagerState.BOT_SPEAKING:
                # Drain session actions (end_call, transfer) BEFORE
                # transitioning to IDLE so no new turn can sneak in.
                tts_action_outcome = await self._drain_session_actions()
                if tts_action_outcome.stop_session:
                    await self._wait_outbound_drain()
                    await self._turn_manager.bot_stopped_speaking()
                else:
                    await self._turn_manager.bot_stopped_speaking()
                    # Wait for queued audio to drain so _drain_outbound_audio
                    # can still call turn.record_audio_sent() and emit playback
                    # marks for the tail of this turn's audio.
                    await self._wait_outbound_drain()
                # Only clear if a new turn hasn't started during the drain.
                if self._turn is turn:
                    self._turn = None
            elif started and not tts_playback_started:
                if gated:
                    # Keep self._turn alive for gated replay mark accounting
                    self._auto_turn_speech_frames = 0
                    self._turn_manager.reset()
                else:
                    self._reset_turn_state()

        # ── Run agent stream + TTS concurrently ──

        agent_result = None

        async def _run_agent_consumer() -> None:
            nonlocal agent_result
            agent_result = await consume_agent_stream(
                self.agent,
                transcript,
                token=token,
                tts_queue=tts_queue,
                emit=self._emit,
                prepare_tts_payload=self._prepare_tts_payload,
                strip_md=self._strip_markdown,
                turn=turn,
            )

        agent_task = asyncio.create_task(_run_agent_consumer())
        tts_task = asyncio.create_task(_process_tts())

        caught_exc: Exception | None = None
        try:
            if self._timeout_config and self._timeout_config.agent_timeout:
                await with_agent_timeout(
                    agent_task,
                    timeout=self._timeout_config.agent_timeout,
                    event_bus=self.event_bus,
                )
            else:
                await agent_task
        except asyncio.CancelledError:
            if not agent_task.done():
                agent_task.cancel()
            if not tts_task.done():
                tts_task.cancel()
            for t in (agent_task, tts_task):
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            raise
        except Exception as exc:
            caught_exc = exc
            if not agent_task.done():
                agent_task.cancel()
            if not tts_task.done():
                tts_task.cancel()
        agent_error = agent_result.error if agent_result else caught_exc
        interrupted = agent_result.interrupted if agent_result else False
        accumulated_text = agent_result.text if agent_result else ""
        structured_output = agent_result.structured_output if agent_result else None
        stream_succeeded = agent_error is None and not (token and token.is_cancelled)

        if self._strip_markdown and accumulated_text and stream_succeeded:
            stripped = strip_markdown(accumulated_text, normalize_code_spans=True)
            if stripped != accumulated_text:
                accumulated_text = stripped
                _replace_last_assistant_text(self.agent, stripped)

        if (accumulated_text or structured_output is not None) and stream_succeeded:
            await self._emit(
                AgentFinal(text=accumulated_text, structured_output=structured_output)
            )

        try:
            await tts_task
        except asyncio.CancelledError:
            pass

        # Estimate what the user heard and notify the agent
        estimate_and_notify_interruption(
            self.agent,
            token,
            turn,
            tts_chunks,
            tts_playback_started=tts_playback_started,
            interrupted=interrupted,
            interruption_mode=self._interruption_mode,
            latency_compensation_ms=self._interruption_latency_compensation_ms,
            ack_stale_ms=self._interruption_ack_stale_ms,
            ack_tail_cap_ms=self._interruption_ack_tail_cap_ms,
        )

        if tts_action_outcome.stop_session:
            await self.stop()
            return

        # If a newer turn started (e.g. barge-in), avoid clobbering its state.
        if self._turn is turn:
            if self._turn_manager.state != TurnManagerState.IDLE:
                self._reset_turn_state()

    def _prepare_tts_payload(self, text: str, *, is_streaming: bool, is_final: bool) -> TTSInput:
        payload = TTSInput(text=text, format="plain")
        payload = apply_output_processors(
            payload,
            self._output_processors,
            is_final=is_final,
            is_streaming=is_streaming,
        )
        if payload.format == "ssml" and not getattr(self.tts, "supports_ssml", False):
            return TTSInput(text=strip_ssml_tags(payload.text), format="plain")
        return payload

    # ── TTS synthesis helper ───────────────────────────────────

    async def _synthesize_tts(
        self, payload: TTSInput | str, token: CancelToken | None
    ) -> _ActionDrainOutcome:
        """Synthesize TTS for a complete payload and emit audio events."""
        action_outcome = _ActionDrainOutcome()
        if isinstance(payload, str):
            payload = self._prepare_tts_payload(payload, is_streaming=False, is_final=True)
        turn = self._turn
        gated = self._is_gated
        if not gated:
            await self._turn_manager.bot_started_speaking()
        try:
            result = await self._tts_synth.synthesize(
                payload,
                token,
                turn_end_time=turn.end_time if turn else None,
                is_active=(
                    None
                    if gated
                    else lambda: self._turn_manager.state == TurnManagerState.BOT_SPEAKING
                ),
            )
            if result.first_audio_time is not None and turn:
                turn.first_tts_audio_time = result.first_audio_time
        except (asyncio.CancelledError, TTSTimeoutError):
            pass
        finally:
            if (
                not gated
                and self._turn is turn
                and turn is not None
                and self._turn_manager.state == TurnManagerState.BOT_SPEAKING
            ):
                # Drain session actions (end_call, transfer) BEFORE
                # transitioning to IDLE so no new turn can sneak in.
                action_outcome = await self._drain_session_actions()
                if action_outcome.stop_session:
                    await self._wait_outbound_drain()
                    await self._turn_manager.bot_stopped_speaking()
                else:
                    await self._turn_manager.bot_stopped_speaking()
                    await self._wait_outbound_drain()
                # Only clear if a new turn hasn't started during the drain.
                if self._turn is turn:
                    self._turn = None
            elif gated and self._turn is turn and turn is not None:
                # Gated opener TTS is buffered — reset to IDLE so the
                # callee's speech can start new turns while we wait for
                # classification.  Keep self._turn alive so that when the
                # gate flushes and replays buffered audio,
                # _drain_outbound_audio can still call record_audio_sent()
                # and send playback marks.
                self._auto_turn_speech_frames = 0
                self._turn_manager.reset()
        return action_outcome

    # ── Internal helpers ───────────────────────────────────────

    async def _wait_outbound_drain(self, timeout: float = 2.0) -> None:
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

    async def _drain_outbound_audio(self) -> None:
        """Send queued outbound audio to the transport with backpressure."""
        while True:
            if not self._is_running and self._outbound_queue.empty():
                break
            try:
                chunk = await self._outbound_queue.get()
            except asyncio.QueueEmpty:
                break
            replayed_chunk = self._replay_chunks_pending > 0
            turn = self._turn
            try:
                await self.transport.send_audio(chunk)
                if self._enable_aec:
                    self.echo_canceller.feed_reference(chunk)
                sent_size = len(chunk.data)
                if turn:
                    turn.record_audio_sent(sent_size, chunk.duration_ms)
                    if (
                        sent_size > 0
                        and self._playback_ack_transport is not None
                        and turn.bytes_since_last_mark >= self._playback_mark_bytes_interval
                    ):
                        turn.bytes_since_last_mark = 0
                        await self._send_playback_mark(turn)
                    elif (
                        sent_size > 0
                        and turn.bytes_since_last_mark > 0
                        and self._playback_ack_transport is not None
                        and self._turn_manager.state != TurnManagerState.BOT_SPEAKING
                        and self._outbound_queue.empty()
                    ):
                        turn.bytes_since_last_mark = 0
                        await self._send_playback_mark(turn)
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
        turn = self._turn
        if turn and turn.bytes_since_last_mark > 0 and self._playback_ack_transport is not None:
            turn.bytes_since_last_mark = 0
            await self._send_playback_mark(turn)

    async def _send_playback_mark(self, turn: TurnContext) -> None:
        if self._playback_ack_transport is None:
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

    def _on_playback_mark_ack(self, event: PlaybackMarkAck) -> None:
        """Track acknowledged playout byte positions for the active turn."""
        turn = self._turn
        if not turn:
            return
        acked_bytes = turn.playback_mark_to_bytes.pop(event.mark_name, None)
        if acked_bytes is None:
            return
        if turn.playback_ack_log and acked_bytes < turn.playback_ack_log[-1][1]:
            acked_bytes = turn.playback_ack_log[-1][1]
        turn.playback_ack_log.append((event.timestamp, acked_bytes))

    def _maybe_attach_event_bus(self, provider: Any) -> None:
        """Attach the session EventBus to provider configs that support it."""
        attached = False
        cfg = getattr(provider, "_config", None)
        if cfg is not None and hasattr(cfg, "event_bus") and getattr(cfg, "event_bus") is None:
            try:
                setattr(cfg, "event_bus", self.event_bus)
                attached = True
            except Exception:
                pass
        has_unset_bus = hasattr(provider, "_event_bus") and getattr(provider, "_event_bus") is None
        if not attached and has_unset_bus:
            try:
                setattr(provider, "_event_bus", self.event_bus)
            except Exception:
                pass

    # ── Text mode ──────────────────────────────────────────────

    async def send_text(self, text: str) -> str:
        """Send text input and return the agent response.

        Only available when the session was created with
        ``runtime_mode="text_session"`` (via :func:`create_text_session`).
        Audio pipeline stages are bypassed — this calls the agent directly.

        Parameters
        ----------
        text:
            User message to send to the agent.

        Returns
        -------
        str
            The agent's response text.
        """
        if self._runtime_mode != "text_session":
            raise RuntimeError("send_text() is only available in text_session mode")
        if self._closed:
            raise RuntimeError("Session has been stopped")
        async with self._text_turn_lock:
            turn_id = f"turn-{uuid4().hex[:12]}"
            if self._journal:
                self._journal.append(
                    kind=JournalRecordKind.EVENT,
                    name="turn_started",
                    session_id=self.session_id,
                    turn_id=turn_id,
                    data={"text": text},
                )
            try:
                t0 = time.monotonic()
                # Propagate turn_id to bridge-backed agents so their journal
                # records (framework_transition, etc.) share the same turn.
                _set_fn = getattr(self.agent, "set_active_turn_id", None)
                if callable(_set_fn):
                    _set_fn(turn_id)
                if hasattr(self.agent, "run_streaming"):
                    accumulated = ""
                    async for event in self.agent.run_streaming(text):
                        if hasattr(event, "type") and event.type == AgentStreamEventType.DONE:
                            if hasattr(event, "text") and event.text:
                                accumulated = event.text
                            break
                        if (
                            hasattr(event, "type")
                            and event.type == AgentStreamEventType.TEXT_DELTA
                            and hasattr(event, "text")
                            and event.text
                        ):
                            accumulated += event.text
                    response = accumulated
                else:
                    response = await self.agent.run(text)
                elapsed_ms = (time.monotonic() - t0) * 1000
                if self._journal:
                    self._journal.append(
                        kind=JournalRecordKind.EVENT,
                        name="agent_final",
                        session_id=self.session_id,
                        turn_id=turn_id,
                        data={"text": response},
                    )
                    self._journal.append(
                        kind=JournalRecordKind.METRIC,
                        name="text_turn_latency_ms",
                        session_id=self.session_id,
                        turn_id=turn_id,
                        data={"value": elapsed_ms},
                    )
            except Exception as exc:
                logger.exception("Agent error in text_session send_text")
                if self._journal:
                    from easycat.runtime.records import ErrorInfo

                    self._journal.append(
                        kind=JournalRecordKind.EVENT,
                        name="error",
                        session_id=self.session_id,
                        turn_id=turn_id,
                        error=ErrorInfo.from_exception(exc),
                    )
                raise
            finally:
                if self._journal:
                    self._journal.append(
                        kind=JournalRecordKind.EVENT,
                        name="turn_ended",
                        session_id=self.session_id,
                        turn_id=turn_id,
                    )
            return response

    async def _cancel_stt(self) -> None:
        try:
            await self.stt.end_stream()
        except Exception:
            pass
        self._stt_active = False
        self._auto_turn_speech_frames = 0
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
        await self._tts_synth.cancel()
        current_task = asyncio.current_task()
        if (
            self._current_tts_task
            and self._current_tts_task is not current_task
            and not self._current_tts_task.done()
        ):
            self._current_tts_task.cancel()
            try:
                await self._current_tts_task
            except (asyncio.CancelledError, Exception):
                pass
