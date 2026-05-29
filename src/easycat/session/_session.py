"""Session: the core runtime for a single voice conversation.

Manages the voice pipeline lifecycle, wires provider stages together,
and handles turn state and cancellation.  Drives the agent bridge
through a single streaming path and feeds incremental TTS synthesis on
sentence boundaries for low-latency playback.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any, TypeVar
from uuid import uuid4

from easycat import _observability as observability
from easycat._bounded_queue import BoundedAudioQueue, DropPolicy
from easycat._health_check import PeriodicHealthChecker
from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.echo_cancellation import PassthroughAEC
from easycat.events import (
    AgentDelta,
    AgentFinal,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Error,
    EventBus,
    EventHandler,
    Interruption,
    PlaybackMarkAck,
    SessionActionCompleted,
    SessionActionFailed,
    SessionActionRequested,
    SessionActionStarted,
    STTFinal,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
    TransportAudioDelivered,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.integrations.agents._agent_runner import AgentRunner
from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.integrations.agents.base import ExternalAgentBridge
from easycat.noise_reduction import PassthroughNoiseReducer
from easycat.runtime.artifacts import SnapshotArtifactStore
from easycat.runtime.capabilities import (
    aclose_if_supported,
    clear_audio_if_supported,
    close_if_supported,
    health_checkable,
    is_active_provider,
    is_passthrough_provider,
)
from easycat.runtime.context import RunContext
from easycat.runtime.journal import (
    ExecutionJournal,
    InMemoryRingBuffer,
    JournalView,
    ReadonlySqliteJournal,
)
from easycat.runtime.scope import RuntimeScope
from easycat.session._audio_router import AudioRouter
from easycat.session._cancel_orchestrator import CancelOrchestrator
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._stt_committer import STTCommitter
from easycat.session._tts_scheduler import TTSScheduler
from easycat.session._turn_runner import TurnRunner
from easycat.session._types import (
    _TM_TO_TURN_STATE,
    Agent,
    CallerIdExposure,
    CallIdentity,
    SessionConfig,
    SessionHelper,
    TurnState,
)
from easycat.session.actions import (
    CoreSessionActionExecutor,
    SessionAction,
    SessionActionExecutor,
)
from easycat.stages.agent import AgentStage
from easycat.stages.audio import AudioStage
from easycat.stages.base import (
    InterruptSignal as _InterruptSignal,
)
from easycat.stages.stt import STTStage
from easycat.stages.transport import TransportStage
from easycat.stages.tts import TTSStage
from easycat.stages.turn import TurnStage
from easycat.stages.vad import VADStage
from easycat.stubs import (
    NoopAgent,
    NoopSTT,
    NoopTransport,
    NoopTTS,
    NoopVAD,
)
from easycat.turn_manager import TurnManager, TurnManagerState

logger = logging.getLogger(__name__)

_HelperT = TypeVar("_HelperT")


class _SessionTurnHandle:
    """Tiny adapter exposing :class:`TurnHandle` on a Session.

    Session is the single authority on the active turn pointer and turn
    generation; this adapter forwards :class:`TurnHandle` reads/writes
    onto the Session attributes so TurnRunner can use the protocol
    without holding a Session reference directly.
    """

    __slots__ = ("_session",)

    def __init__(self, session: Session) -> None:
        self._session = session

    @property
    def current(self) -> TurnContext | None:
        return self._session._turn

    @property
    def generation(self) -> int:
        return self._session._turn_generation

    @property
    def no_turn(self) -> TurnContext:
        return self._session._no_turn

    def set(self, turn: TurnContext | None) -> None:
        self._session._turn = turn
        if turn is not None:
            self._session._turn_generation = turn.generation


def _ensure_bridge(agent: Any) -> ExternalAgentBridge:
    """Guarantee ``agent`` implements :class:`ExternalAgentBridge`.

    Bare ``Agent``-protocol objects (``async run(text) -> str``) and the
    no-op stub get wrapped in :class:`AgentRunner` so Session only ever
    speaks the bridge protocol downstream.
    """
    if isinstance(agent, ExternalAgentBridge):
        return agent
    return AgentRunner(agent)


class Session:
    """One voice session (per call / per websocket client).

    Manages the full pipeline: Audio In -> Noise Reduction -> VAD -> STT ->
    Agent -> TTS -> Audio Out. Each stage is a pluggable provider.

    All agents reach Session as :class:`ExternalAgentBridge` instances —
    simple ``Agent``-protocol objects are wrapped in :class:`AgentRunner`
    at construction time.  Session consumes ``AgentBridgeEvent`` text
    deltas incrementally and begins TTS synthesis on sentence boundaries
    for lower latency.
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
        # Back-store for the ``agent`` property so late assignments
        # (``session.agent = X``) keep the AgentStage wrapper in sync.
        # ``auto_adapt_agent`` returns plain ``async run(text)`` agents
        # unchanged so factories can apply ``config.agent_runner`` /
        # ``wrap_agent`` explicitly.  For direct ``Session(...)`` callers
        # and ``wrap_agent=False`` with a plain agent, wrap here as a
        # safety net so the bridge interface Session relies on
        # (``reset``, ``replace_last_assistant_text``) is always present.
        self._agent: Agent = (
            _ensure_bridge(auto_adapt_agent(cfg.agent)) if cfg.agent else NoopAgent()
        )
        # Stashed by create_session/create_text_session so mid-session
        # agent swaps to a URL-backed agent can forward model/key context.
        self._agent_model: str | None = None
        self._remote_agent_api_key: str | None = None
        # Session-wide MCP server list — re-applied to any agent swapped
        # in via ``session.agent = ...`` so tool access survives the swap.
        self._mcp_servers: tuple[str, ...] = tuple(cfg.mcp_servers)
        # Inject MCP servers into the bridge so direct
        # ``Session(SessionConfig(agent=..., mcp_servers=...))`` callers
        # (not going through ``create_session``) also get tool access.
        self._inject_agent_runtime_config(self._agent)

        # Telephony caller / callee identity.  Populated by the
        # transport (inbound: Twilio customParameters) or by the
        # outbound call manager (outbound: the dialed number).
        # ``caller_id_exposure`` governs whether the agent's LLM sees
        # the number or only tool code does.
        self._call_identity: CallIdentity | None = cfg.call_identity
        self._caller_id_exposure: CallerIdExposure = cfg.caller_id_exposure

        # Skip noop validation in text_session mode — audio providers
        # are intentionally noop.
        if cfg.runtime_mode != "text_session":
            noops = []
            if is_passthrough_provider(self.stt):
                noops.append("stt")
            if is_passthrough_provider(self.tts):
                noops.append("tts")
            if cfg.enable_vad and is_passthrough_provider(self.vad):
                noops.append("vad")
            if cfg.enable_noise_reduction and is_passthrough_provider(self.noise_reducer):
                noops.append("noise_reducer")
            if is_passthrough_provider(self.transport):
                noops.append("transport")
            if cfg.agent is None and is_passthrough_provider(self.agent):
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

        # Pipeline flags — auto-enable when a real provider is supplied so
        # that direct SessionConfig users don't silently lose processing.
        self._enable_noise_reduction = cfg.enable_noise_reduction or is_active_provider(
            self.noise_reducer
        )
        self._enable_aec = (
            cfg.enable_echo_cancellation or is_active_provider(self.echo_canceller)
        ) and is_active_provider(self.echo_canceller)
        self._enable_vad = cfg.enable_vad
        self._auto_turn_from_stt_final = cfg.auto_turn_from_stt_final
        # Interruption-config knobs are owned by the CancelOrchestrator
        # (see ``self._cancel`` below).  Session exposes read-only
        # property delegates so external readers keep working.

        # Turn manager — single source of truth for turn state.  The
        # barge-in cancel callback is installed later via
        # :meth:`TurnManager.set_cancel_callback` because it is wired
        # through ``CancelOrchestrator``, constructed below.
        self._turn_manager = cfg.turn_manager or TurnManager(
            self.event_bus,
            config=cfg.turn_manager_config,
        )
        # TurnManager emits a ``turn_state_changed`` journal record on
        # every state transition so bundle readers can answer "why did
        # it go to PROCESSING" from the journal alone.
        self._turn_manager.bind_journal_hook(self._on_turn_state_changed)
        # TurnStarted/TurnEnded subscriptions are deferred until after
        # ``self._turn_runner`` is constructed below, because the
        # handlers live on the runner.  PlaybackMarkAck and
        # TransportAudioDelivered are wired to AudioRouter below — see
        # the audio_router construction site.

        # Opt-out auto-wiring.  Runs on every STT final; on a match it
        # emits ``OptOutDetected``, adds the caller to an attached DNC
        # list, and queues ``EndCallAction(reason="opt_out")`` so the
        # call hangs up after the current agent utterance.  Disable
        # via ``SessionConfig.opt_out_detection=False``.
        self._opt_out_detection = cfg.opt_out_detection
        self._opt_out_phrases: list[str] | None = (
            list(cfg.opt_out_phrases) if cfg.opt_out_phrases is not None else None
        )
        self._dnc_list: Any | None = cfg.dnc_list
        if self._opt_out_detection:
            self.event_bus.subscribe(STTFinal, self._on_stt_final_opt_out)

        # Optional "bot speaks first" greeting synthesized on the first
        # CallAnswered event — works for both inbound (stream start)
        # and outbound (callee picks up).  Only the first occurrence
        # is honored so a warm-transfer style flow with a second
        # CallAnswered doesn't re-greet.
        self._greeting: str | None = cfg.greeting
        self._greeting_spoken: bool = False
        self._greeting_task: asyncio.Task[Any] | None = None
        if self._greeting:
            from easycat.events import CallAnswered as _CallAnsweredEv

            self.event_bus.subscribe(_CallAnsweredEv, self._on_call_answered_greet)
        tm_config = getattr(self._turn_manager, "_config", None)
        stt_segment_silence_ms = max(0, getattr(tm_config, "stt_segment_silence_ms", 0))

        # Reliability/observability config
        self._timeout_config = cfg.timeout_config or self._default_timeout_config()
        self._journal = cfg.journal
        self._journal_view: JournalView | None = (
            JournalView(self._journal) if self._journal is not None else None
        )
        self._artifact_store = cfg.artifact_store

        # Backpressure (outbound audio queue).  The queue is shared
        # between TTSSynthesizer (producer) and AudioRouter (drain
        # consumer); Session constructs it once and hands the same
        # instance to both.  Router takes ownership for drain semantics
        # after construction.
        self._outbound_queue_external = cfg.outbound_queue is not None
        self._outbound_queue_max_size = 200
        self._outbound_queue_policy = DropPolicy.DROP_OLDEST
        self._outbound_queue_name = "outbound_audio"
        self._outbound_queue = cfg.outbound_queue or BoundedAudioQueue(
            max_size=self._outbound_queue_max_size,
            policy=self._outbound_queue_policy,
            name=self._outbound_queue_name,
            on_drop=self._on_queue_drop,
        )
        # TTSSynthesizer is now owned by ``TTSScheduler``, constructed
        # later in __init__ after the AudioRouter is available.
        self._audio_gate = cfg.audio_gate
        self._health_checkers: list[PeriodicHealthChecker] = []
        self._telephony_helpers: list[SessionHelper] = list(cfg.telephony_helpers)
        self._runtime_scope = RuntimeScope()

        # Agent-initiated session actions
        self._session_actions = cfg.session_actions
        self._action_executors: list[SessionActionExecutor] = [
            *cfg.action_executors,
            CoreSessionActionExecutor(),
        ]

        # State
        self._is_running = False
        self._closed = False
        self._stopping = False
        self._flushed = False
        self._observability_active = False
        self._closed_event: asyncio.Event | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        # STT futures live on TurnContext (per-turn so a stale callback
        # from the previous turn cannot resolve a future on the next turn).

        # Per-turn state — created fresh at each turn start.
        # _turn_generation is a monotonic counter that increases each time a
        # new turn starts, used to detect stale callbacks from previous turns.
        # Auto-turn speech-frame counter, gated-replay pending count,
        # playback-mark accounting, and the outbound queue all live on
        # AudioRouter (constructed below).
        self._turn: TurnContext | None = None
        self._turn_generation: int = 0

        self.session_id = cfg.session_id or f"session-{uuid4().hex[:12]}"
        self._runtime_mode = cfg.runtime_mode
        self._turn_manager.bind_session(self.session_id)
        self._journal_sink = SessionJournalSink(
            event_bus=self.event_bus,
            journal=self._journal,
            artifact_store=self._artifact_store,
            session_id=self.session_id,
            current_turn_id=self._journal_turn_id,
        )
        self._journal_sink.subscribe()

        # WS3 T3.10 integration: instantiate every stage wrapper and a
        # shared RunContext so Session can route provider calls through
        # stages for journal + artifact capture.  Stages are the debug /
        # replay surface; Session keeps pipeline orchestration (T3.10).
        self._run_ctx = RunContext(
            run_id=self.session_id,
            session_id=self.session_id,
            runtime_mode=cfg.runtime_mode,
            journal=self._journal,
            artifact_store=self._artifact_store,
        )
        self._no_turn = TurnContext(turn_id="no-turn", cancel_token=CancelToken())
        self._stt_stage = STTStage(self.stt, journal=self._journal)
        self._tts_stage = TTSStage(self.tts, journal=self._journal)
        self._vad_stage = VADStage(self.vad, journal=self._journal)
        self._audio_stage = AudioStage(
            self.noise_reducer,
            echo_canceller=self.echo_canceller if self._enable_aec else None,
            journal=self._journal,
        )
        self._transport_stage = TransportStage(self.transport, journal=self._journal)
        self._agent_stage = AgentStage(
            self.agent,
            journal=self._journal,
            artifact_store=self._artifact_store,
            session_id=self.session_id,
            mcp_servers=tuple(cfg.mcp_servers),
        )
        self._turn_stage = TurnStage(
            self._turn_manager._config.endpoint_detector  # type: ignore[attr-defined]
            if self._turn_manager._config is not None  # type: ignore[attr-defined]
            else None,
            journal=self._journal,
        )

        def _set_running(value: bool) -> None:
            self._is_running = value

        self._audio_router = AudioRouter(
            transport=self.transport,
            audio_stage=self._audio_stage,
            vad_stage=self._vad_stage,
            stt_stage=self._stt_stage,
            transport_stage=self._transport_stage,
            turn_manager=self._turn_manager,
            event_bus=self.event_bus,
            journal_sink=self._journal_sink,
            run_ctx=self._run_ctx,
            no_turn=self._no_turn,
            echo_canceller=self.echo_canceller,
            enable_noise_reduction=lambda: self._enable_noise_reduction,
            enable_aec=lambda: self._enable_aec,
            enable_vad=lambda: self._enable_vad,
            auto_turn_from_stt_final=lambda: self._auto_turn_from_stt_final,
            emit=self._emit,
            is_running=lambda: self._is_running,
            set_running=_set_running,
            current_turn=lambda: self._turn,
            is_stt_active=lambda: self._stt_committer.is_active,
            with_correlation=self._with_correlation,
            outbound_queue=self._outbound_queue,
        )
        self.event_bus.subscribe(PlaybackMarkAck, self._audio_router.on_playback_ack)
        self.event_bus.subscribe(TransportAudioDelivered, self._audio_router.on_audio_delivered)

        self._stt_committer = STTCommitter(
            stt=lambda: self.stt,
            event_bus=self.event_bus,
            journal_sink=self._journal_sink,
            runtime_scope=self._runtime_scope,
            timeout_config=self._timeout_config,
            segment_silence_ms=stt_segment_silence_ms,
            no_turn=self._no_turn,
            current_turn=lambda: self._turn,
            turn_manager=self._turn_manager,
            emit=self._emit,
            auto_turn_from_stt_final=lambda: self._auto_turn_from_stt_final,
            on_speech_detection_reset=self._audio_router.reset_speech_detection,
        )
        self.event_bus.subscribe(VADStopSpeaking, self._stt_committer.schedule)
        self.event_bus.subscribe(VADStartSpeaking, self._stt_committer.cancel_scheduled)

        def _clear_turn() -> None:
            self._turn = None

        self._tts_scheduler = TTSScheduler(
            tts=lambda: self.tts,
            tts_stage=self._tts_stage,
            turn_manager=self._turn_manager,
            event_bus=self.event_bus,
            journal_sink=self._journal_sink,
            run_ctx=self._run_ctx,
            no_turn=self._no_turn,
            audio_router=self._audio_router,
            outbound_queue=self._outbound_queue,
            timeout_config=self._timeout_config,
            correlation_ids=lambda: (
                self.session_id,
                self._turn.id
                if self._turn and self._turn_manager.state != TurnManagerState.IDLE
                else None,
            ),
            audio_gate=cfg.audio_gate,
            output_processors=list(cfg.output_processors),
            strip_markdown_enabled=cfg.strip_markdown,
            current_turn=lambda: self._turn,
            is_gated=lambda: self._is_gated,
            drain_session_actions=self._drain_session_actions,
            clear_turn=_clear_turn,
        )

        # Cancel orchestrator — owns control-signal propagation, barge-in
        # suppression policy, and the interruption-config knobs.  Must be
        # constructed after all 7 stages and the STTCommitter/TTSScheduler
        # exist; it is wired into the TurnManager below.
        self._cancel = CancelOrchestrator(
            transport_stage=self._transport_stage,
            tts_stage=self._tts_stage,
            agent_stage=self._agent_stage,
            turn_stage=self._turn_stage,
            stt_stage=self._stt_stage,
            vad_stage=self._vad_stage,
            audio_stage=self._audio_stage,
            run_ctx=self._run_ctx,
            journal_sink=self._journal_sink,
            interruption_mode=cfg.interruption_mode,
            interruption_latency_compensation_ms=cfg.interruption_latency_compensation_ms,
            interruption_ack_stale_ms=cfg.interruption_ack_stale_ms,
            interruption_ack_tail_cap_ms=cfg.interruption_ack_tail_cap_ms,
            current_turn=lambda: self._turn,
            session_actions=lambda: self._session_actions,
            telephony_helpers_present=lambda: bool(self._telephony_helpers),
            cancel_turn_impl=self.cancel_turn,
        )
        # Install the orchestrator's barge-in callback now that it exists.
        self._turn_manager.set_cancel_callback(self._cancel.for_barge_in)

        # Turn runner — owns the per-turn agent loop.  Depends on every
        # collaborator above (STTCommitter, TTSScheduler, AudioRouter,
        # CancelOrchestrator, AgentStage), so its event subscriptions are
        # deferred until after it is constructed.
        self._turn_runner = TurnRunner(
            stt_committer=self._stt_committer,
            tts_scheduler=self._tts_scheduler,
            audio_router=self._audio_router,
            cancel_orchestrator=self._cancel,
            turn_manager=self._turn_manager,
            agent_stage=self._agent_stage,
            run_ctx=self._run_ctx,
            event_bus=self.event_bus,
            journal_sink=self._journal_sink,
            runtime_scope=self._runtime_scope,
            timeout_config=self._timeout_config,
            turn_handle=_SessionTurnHandle(self),
            stt_stage=self._stt_stage,
            stt_provider=lambda: self.stt,
            is_running=lambda: self._is_running,
            is_gated=lambda: self._is_gated,
            agent=lambda: self.agent,
            drain_session_actions=self._drain_session_actions,
            caller_id_system_message=self._caller_id_system_message,
            # ``stop`` is re-read each call so test patches via
            # ``session.stop = AsyncMock(...)`` are observable to the runner.
            stop=lambda: self.stop(),
            reset_turn_state=self._reset_turn_state,
            emit=self._emit,
            session_id=self.session_id,
            journal_enabled=self._journal is not None,
        )
        # Wire TurnStarted / TurnEnded subscriptions now that the runner
        # exists (its handlers are the subscribers).
        self.event_bus.subscribe(TurnStarted, self._turn_runner.on_turn_started)
        self.event_bus.subscribe(TurnEnded, self._turn_runner.schedule_turn_ended)

        # Plug the TurnStage into the TurnManager's endpoint-detector call
        # so smart-turn decisions go through stage.execute() and produce
        # journal records.
        if self._turn_manager._config is not None:  # type: ignore[attr-defined]
            if self._turn_manager._config.endpoint_detector is not None:  # type: ignore[attr-defined]
                self._turn_manager.bind_endpoint_stage(
                    self._turn_stage,
                    run_ctx_getter=lambda: self._run_ctx,
                    turn_getter=lambda: self._turn or self._no_turn,
                )

    @staticmethod
    def _default_timeout_config():
        from easycat.timeouts import TimeoutConfig

        return TimeoutConfig()

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

    def _journal_turn_id(self, turn_id: str | None = None) -> str | None:
        if turn_id is not None:
            return turn_id
        if self._turn is not None:
            return self._turn.id
        return None

    def _on_turn_state_changed(
        self,
        from_state: Any,
        to_state: Any,
        reason: str,
        turn_id: str | None,
    ) -> None:
        """TurnManager hook — journal each turn-state transition.

        Wired up in ``__init__``.  ``from_state`` / ``to_state`` are
        :class:`TurnManagerState` instances; we record their string
        values so the record is JSON-serialisable without requiring
        replay consumers to know the enum type.
        """
        self._journal_sink.append_record(
            name="turn_state_changed",
            turn_id=turn_id,
            data={
                "from": getattr(from_state, "value", str(from_state)),
                "to": getattr(to_state, "value", str(to_state)),
                "reason": reason,
            },
        )

    async def _emit_heartbeats(self, interval_s: float = 1.0) -> None:
        """Emit a periodic ``pipeline_heartbeat`` record.

        ``loop_lag_ms`` is the measured delta between the scheduled
        wakeup time and the actual wakeup time.  Under healthy load
        this is near zero; a number in the hundreds of ms means a sync
        handler is blocking the asyncio loop and audio processing has
        stalled.  Visible in the journal without live tracing or OS
        profiler.
        """
        loop = asyncio.get_running_loop()
        next_deadline = loop.time() + interval_s
        try:
            while self._is_running:
                await asyncio.sleep(max(0.0, next_deadline - loop.time()))
                now = loop.time()
                loop_lag_ms = max(0.0, (now - next_deadline) * 1000.0)
                self._journal_sink.append_record(
                    name="pipeline_heartbeat",
                    data={
                        "interval_ms": int(interval_s * 1000),
                        "loop_lag_ms": round(loop_lag_ms, 3),
                        "outbound_queue_len": self._outbound_queue.qsize(),
                        "outbound_queue_drops": self._outbound_queue.drops,
                    },
                )
                observability.record_histogram(
                    "easycat.event_loop.lag",
                    loop_lag_ms / 1000.0,
                    {"easycat.stage": "session"},
                )
                observability.observe_gauge(
                    "easycat.queue.depth",
                    self._outbound_queue.qsize(),
                    {"easycat.stage": "audio_queue"},
                )
                observability.observe_gauge(
                    "easycat.journal.degraded",
                    1 if self._journal is not None and self._journal.degraded else 0,
                )
                next_deadline = now + interval_s
        except asyncio.CancelledError:
            pass

    def _on_queue_drop(
        self,
        queue_name: str,
        kind: str,
        queue_len: int,
        total_drops: int,
    ) -> None:
        """BoundedAudioQueue hook — journal every drop.

        Back-pressure / underflow is invisible from the journal
        otherwise; the queue's internal ``drops`` counter can only be
        read live.  One record per drop so bundle readers can correlate
        audio gaps to queue pressure timing.
        """
        self._journal_sink.append_record(
            name="audio_queue_drop",
            data={
                "queue": queue_name,
                "kind": kind,
                "queue_len": queue_len,
                "total_drops": total_drops,
            },
        )

    def _reset_turn_state(self) -> None:
        """Clear turn correlation state and reset the turn manager."""
        turn = self._turn
        self._stt_committer.cancel_scheduled()
        self._stt_committer.cancel_inflight()
        self._stt_committer.resolve_pending(turn, "")
        self._turn = None
        self._audio_router.reset_speech_detection()
        self._audio_router.reset_replay_chunks()
        self._turn_manager.reset()

    @property
    def _is_gated(self) -> bool:
        """Whether the classification gate is currently buffering TTS audio."""
        return self._audio_gate is not None and self._audio_gate()

    # Read-only delegates for the interruption-config knobs owned by
    # :class:`CancelOrchestrator`, so external tests/tools that read
    # these off Session keep working.
    @property
    def _interruption_mode(self) -> str:
        return self._cancel.interruption_mode

    @property
    def _interruption_latency_compensation_ms(self) -> int:
        return self._cancel.latency_compensation_ms

    @property
    def _interruption_ack_stale_ms(self) -> int:
        return self._cancel.ack_stale_ms

    @property
    def _interruption_ack_tail_cap_ms(self) -> int:
        return self._cancel.ack_tail_cap_ms

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

    def get_helper(self, helper_type: type[_HelperT]) -> _HelperT | None:
        """Return the first attached session helper matching *helper_type*.

        Telephony features are lifecycle-managed helpers under the hood.  This
        accessor keeps advanced applications off ``_telephony_helpers`` while
        preserving the lightweight helper model.
        """
        for helper in self._telephony_helpers:
            if isinstance(helper, helper_type):
                return helper
        return None

    def export_debug_bundle(
        self,
        path: str,
        *,
        inline_artifacts: bool = False,
        overwrite: bool = False,
    ) -> None:
        """Export a debug bundle from this running or cleanly stopped session.

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
    def agent(self) -> Agent:
        """Current agent provider.

        Exposed as a property so callers that swap the agent mid-session
        (``session.agent = FailingAgent()``) automatically re-point the
        AgentStage wrapper at the new provider.
        """
        return self._agent

    @agent.setter
    def agent(self, value: Agent) -> None:
        from easycat.integrations.agents._agent_runner import AgentRunner

        previous_agent = getattr(self, "_agent", None)
        previous_runner: AgentRunner | None = (
            previous_agent if isinstance(previous_agent, AgentRunner) else None
        )

        if value is None:
            # Wrap NoopAgent so it satisfies ExternalAgentBridge; AgentStage
            # calls ``bridge.invoke()`` unconditionally and crashes on a bare
            # NoopAgent.
            self._agent = AgentRunner(NoopAgent())
        else:
            adapted = auto_adapt_agent(value, model=self._agent_model)
            if previous_runner is not None and not isinstance(adapted, AgentRunner):
                adapted = AgentRunner(adapted, previous_runner._config)
            elif not isinstance(adapted, ExternalAgentBridge):
                # Plain ``async run(text)`` agent swapped in — wrap so the
                # bridge-facing Session APIs keep working.
                adapted = AgentRunner(adapted)
            self._agent = adapted
            self._inject_agent_runtime_config(self._agent)

        stage = getattr(self, "_agent_stage", None)
        if stage is not None:
            stage._provider = self._agent  # keep the wrapper in sync

    def _inject_agent_runtime_config(self, agent: Any) -> None:
        """Apply session MCP servers, remote model, and API key to ``agent``.

        The framework bridges (``OpenAIAgentsBridge``, ``PydanticAIBridge``)
        install MCP tools from ``self._mcp_servers`` at ``invoke()`` time, so
        the session has to push its list into the bridge whenever the agent
        is created or swapped.  Remote model / API key follow the same
        pattern for :class:`RemoteResponsesAPIBridge`.
        """
        from easycat.config import _inject_agent_runtime

        _inject_agent_runtime(
            agent,
            mcp_servers=self._mcp_servers,
            agent_model=self._agent_model,
            remote_agent_api_key=self._remote_agent_api_key,
        )

    @property
    def transport_kind(self) -> str:
        """Coarse transport class for tool-side branching.

        Returns one of ``"telephony"``, ``"webrtc"``, ``"websocket"``,
        ``"local"``, ``"noop"``, or ``"custom"``.  Tools that need to
        behave differently on a phone call vs a browser session
        (don't reference the screen, avoid long URLs, skip emoji, …)
        read this rather than poking at transport internals.
        """
        transport = self.transport
        explicit = getattr(transport, "transport_kind", None)
        if isinstance(explicit, str) and explicit:
            return explicit

        # Fallback for third-party transports that have not adopted the
        # explicit property yet.
        module = type(transport).__module__
        name = type(transport).__name__.lower()
        if "webrtc" in module or "webrtc" in name:
            return "webrtc"
        if "websocket" in module or "websocket" in name:
            return "websocket"
        if "local" in module or name == "localtransport":
            return "local"
        if "noop" in name or "stubs" in module:
            return "noop"
        return "custom"

    @property
    def outbound_call_manager(self) -> Any | None:
        """Outbound call manager attached to this session, when configured."""
        from easycat.telephony.outbound import OutboundCallManager

        return self.get_helper(OutboundCallManager)

    @property
    def outbound_call_state_machine(self) -> Any | None:
        """Outbound call state machine attached to this session, when configured."""
        from easycat.telephony.call_state import OutboundCallStateMachine

        return self.get_helper(OutboundCallStateMachine)

    @property
    def number_health_monitor(self) -> Any | None:
        """Per-number health monitor attached to this session, when configured."""
        from easycat.telephony.number_health import NumberHealthMonitor

        return self.get_helper(NumberHealthMonitor)

    @property
    def call_disposition_tracker(self) -> Any | None:
        """Call disposition tracker attached to this session, when configured."""
        from easycat.telephony.number_health import CallDispositionTracker

        return self.get_helper(CallDispositionTracker)

    @property
    def dnc_list(self) -> Any | None:
        """Do-Not-Call list consulted by opt-out auto-detection.

        Apps that want opt-out flows to persist across sessions
        assign the same ``DNCList`` instance to every session
        (or wire a shared store behind a DNC-list-compatible object).
        """
        return self._dnc_list

    @dnc_list.setter
    def dnc_list(self, value: Any | None) -> None:
        self._dnc_list = value

    @property
    def call_identity(self) -> CallIdentity | None:
        """Caller / callee identity for this session.

        Populated by telephony transports on connect (Twilio reads
        ``<Stream>`` customParameters) or by
        :meth:`OutboundCallManager.place_call` for outbound calls.
        Tool code (including agent function tools) reads this directly
        unless :attr:`caller_id_exposure` is ``"off"``.  Internal
        telephony policy hooks retain the private value so opt-out
        detection can still update DNC state.
        """
        if self._caller_id_exposure == "off":
            return None
        return self._call_identity

    @call_identity.setter
    def call_identity(self, value: CallIdentity | None) -> None:
        self._call_identity = value

    @property
    def caller_id_exposure(self) -> CallerIdExposure:
        """Exposure policy for :attr:`call_identity`."""
        return self._caller_id_exposure

    @caller_id_exposure.setter
    def caller_id_exposure(self, value: CallerIdExposure) -> None:
        self._caller_id_exposure = value

    def _caller_id_system_message(self) -> str | None:
        """Render the caller-ID system message for the agent, or None.

        Returns ``None`` when the exposure policy hides the caller ID
        from the LLM (``"tools_only"`` / ``"off"``) or when we have no
        identity to share yet.
        """
        if self._caller_id_exposure != "system_message":
            return None
        identity = self._call_identity
        if identity is None:
            return None
        parts: list[str] = []
        if identity.caller_number:
            prefix = "The caller's phone number is"
            if identity.direction == "outbound":
                prefix = "This outbound call is to"
            parts.append(f"{prefix} {identity.caller_number}.")
        if identity.called_number:
            if identity.direction == "outbound":
                parts.append(f"It was placed from {identity.called_number}.")
            else:
                parts.append(f"They dialed {identity.called_number}.")
        if identity.display_name:
            parts.append(f"Caller ID name: {identity.display_name}.")
        return " ".join(parts) if parts else None

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
        """Read-only journal view, including after a clean stop/shutdown.

        Returns a stable view — callers may cache the result and it will
        remain valid even after :meth:`stop` / :meth:`shutdown` replace
        the underlying journal backend with a read-only snapshot.
        """
        return self._journal_view

    @property
    def cancel_token(self) -> CancelToken | None:
        return self._turn.cancel_token if self._turn else None

    async def replay_gated_audio(self, events: list[Any]) -> None:
        """Replay buffered TTS audio chunks through the outbound queue.

        Delegates to :class:`AudioRouter` which owns the outbound queue
        and the gated-replay pending counter.
        """
        await self._audio_router.gated_replay(events)

    async def synthesize_bypass(self, text: str) -> None:
        """Synthesize text via TTS, bypassing the classification gate.

        Used for hold audio and screening responses that must reach the
        transport even while the gate is closed.
        """
        await self._tts_scheduler.synthesize_bypass(text)

    # ── Async context manager ────────────────────────────────────

    async def __aenter__(self) -> Session:
        """Enter an ``async with session:`` block.

        Starts the session when it has not been started already so that
        ``async with create_session(cfg):`` is a one-liner equivalent to
        ``easycat.run()`` for callers who already own an event loop.
        """
        if self._runtime_mode != "text_session" and not self._is_running and not self._closed:
            await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Exit the context manager, tearing the session down cleanly."""
        await self.stop(force=True)

    async def wait_closed(self) -> None:
        """Block until the session has been stopped or shut down.

        Mirrors ``asyncio.Server.wait_closed()`` / ``Queue.join()`` and
        is the idiomatic pair for ``async with session: await
        session.wait_closed()``.  Returns immediately when the session
        is already closed.
        """
        if self._closed:
            return
        event = self._closed_event
        if event is None:
            event = asyncio.Event()
            self._closed_event = event
            if self._closed:
                event.set()
        await event.wait()

    def _mark_closed(self) -> None:
        """Flip the closed flag and wake any `wait_closed()` waiters."""
        self._closed = True
        event = self._closed_event
        if event is not None:
            event.set()

    def _mark_observability_active(self) -> None:
        if self._observability_active:
            return
        observability.session_started()
        self._observability_active = True

    def _mark_observability_inactive(self) -> None:
        if not self._observability_active:
            return
        observability.session_ended()
        self._observability_active = False

    def _on_provider_unhealthy(self, provider_name: str) -> None:
        """React to a provider crossing the consecutive-failure threshold.

        Health checks fire this once on the healthy->unhealthy transition.
        WebSocket-backed providers reconnect internally on the next send/recv,
        so the actionable step here is escalation: the threshold-gated ``Error``
        event (emitted by the checker) lets owners drive teardown/failover, and
        we surface a session-level warning so a persistently stale provider is
        visible without spamming a warning every check interval.
        """
        logger.warning(
            "Provider %r is unhealthy after repeated health checks; "
            "recovery is delegated to provider reconnect / Error subscribers",
            provider_name,
        )

    def _on_provider_recovered(self, provider_name: str) -> None:
        """React to a previously-unhealthy provider passing a health check."""
        logger.info("Provider %r recovered from unhealthy state", provider_name)

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
                    on_drop=self._on_queue_drop,
                )
                self._tts_scheduler.replace_outbound_queue(self._outbound_queue)
                self._audio_router.replace_outbound_queue(self._outbound_queue)

            for name, provider in (
                ("stt", self.stt),
                ("tts", self.tts),
                ("transport", self.transport),
            ):
                health_provider = health_checkable(provider)
                if health_provider is not None:
                    checker = PeriodicHealthChecker(
                        health_provider,
                        provider_name=name,
                        event_bus=self.event_bus,
                        failure_threshold=3,
                        on_unhealthy=self._on_provider_unhealthy,
                        on_recovered=self._on_provider_recovered,
                    )
                    checker.start()
                    self._health_checkers.append(checker)

            for helper in self._telephony_helpers:
                helper.start()

            self._is_running = True
            self._mark_observability_active()
            self._audio_router.start_outbound()
            self._audio_router.start_ingress()
            # Heartbeat task detects asyncio event-loop stalls.  If a
            # sync handler blocks the loop for >heartbeat_interval the
            # gap between heartbeats widens — ``loop_lag_ns`` in the
            # record makes that visible in a bundle without requiring
            # live tracing.
            self._heartbeat_task = self._runtime_scope.create_task(
                "pipeline_heartbeat",
                self._emit_heartbeats(),
            )
        except Exception:
            self._is_running = False
            self._mark_observability_inactive()

            await self._audio_router.stop_ingress()
            await self._audio_router.stop_outbound()
            await self._runtime_scope.cancel_and_drain("pipeline_heartbeat")
            self._heartbeat_task = None

            for checker in self._health_checkers:
                await checker.stop()
            self._health_checkers = []

            self._stop_helpers()
            self._reset_turn_state()

            if transport_connected:
                await self.transport.disconnect()
            raise

    async def stop(self, *, force: bool = False) -> None:
        """Stop the session and release live backend resources.

        The single public teardown verb.  ``force=False`` (the default)
        drains in-flight work gracefully; ``force=True`` aggressively
        cancels the pipeline / TTS / outbound tasks first (the former
        ``shutdown()`` behavior) for when a graceful stop is hung on a
        misbehaving provider.

        Prefer the ``async with session:`` context manager, which calls
        this for you on exit.
        """
        if self._closed or self._stopping:
            return
        self._stopping = True
        self._is_running = False
        current_task = asyncio.current_task()

        try:
            turn = self._turn
            if turn:
                turn.cancel_token.cancel()

            # Cancel any in-flight text turn so it doesn't emit events
            # after the session is torn down.
            text_token = self._turn_runner.text_turn_cancel_token
            if text_token:
                text_token.cancel()
            text_task = self._turn_runner.active_text_turn
            if text_task is not None and not text_task.done():
                text_task.cancel()
                try:
                    await text_task
                except (asyncio.CancelledError, Exception):
                    pass

            if force:
                # Force path: aggressively cancel every pipeline task and
                # signal scoped work before awaiting any handle so the
                # force-cancel ordering is preserved.
                tasks: list[asyncio.Task[Any]] = []
                pipeline_task = self._audio_router.pipeline_task
                if pipeline_task and not pipeline_task.done():
                    pipeline_task.cancel()
                    tasks.append(pipeline_task)
                # STT teardown is delegated to STTCommitter.cancel() below
                # (it cancels the consumer task, ends the stream, and drains
                # scoped commit/pause tasks) — matching 92f8ebf's move away
                # from an ad-hoc stt_task cancel here.
                current_tts_task = self._tts_scheduler.current_task
                if current_tts_task and not current_tts_task.done():
                    current_tts_task.cancel()
                    tasks.append(current_tts_task)
                outbound_task = self._audio_router.outbound_task
                if outbound_task and not outbound_task.done():
                    outbound_task.cancel()
                    tasks.append(outbound_task)

                # Signal scoped work before awaiting other task handles so
                # migrated shutdown work preserves the previous force-cancel
                # ordering. Drain below after every task observed cancellation.
                self._runtime_scope.cancel()
                for task in tasks:
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass
                await self._stt_committer.cancel(turn)
                # RuntimeScope-owned work currently covers heartbeat,
                # greeting, and STT segment commit/pause tasks. These can
                # outlive the pipeline/STT consumer handles above, so the
                # force path drains the scope before provider teardown.
                await self._runtime_scope.cancel_and_drain()
                self._stt_committer.clear_task_handles()
                self._greeting_task = None
                self._heartbeat_task = None
            else:
                # Graceful path: always perform cleanup — even when the
                # ingress loop already flipped ``_is_running`` to False
                # (e.g. after a transport disconnect).  Each step is
                # individually guarded and safe to call when no work was
                # started.
                pipeline_task = self._audio_router.pipeline_task
                if (
                    pipeline_task
                    and pipeline_task is not current_task
                    and not pipeline_task.done()
                ):
                    pipeline_task.cancel()
                    try:
                        await pipeline_task
                    except asyncio.CancelledError:
                        logger.debug(
                            "TTS processing task was cancelled; ensuring"
                            " BotStoppedSpeaking is emitted if needed."
                        )

                await self._cancel_greeting_task()
                await self._stt_committer.cancel(turn)
                await self._tts_scheduler.cancel()

            for checker in self._health_checkers:
                await checker.stop()
            self._health_checkers = []
            self._stop_helpers()
            if not self._outbound_queue_external:
                self._outbound_queue.close()
            # Cancel the outbound drain task BEFORE disconnecting the
            # transport — otherwise the task may hang on send_audio()
            # with a disconnected transport.  (The force path already
            # cancelled it above; stop_outbound is idempotent.)
            await self._audio_router.stop_outbound()
            await self._runtime_scope.cancel_and_drain("pipeline_heartbeat")
            self._heartbeat_task = None
            await self.transport.disconnect()
            await self._turn_manager.shutdown()
            try:
                await aclose_if_supported(self.agent)
            except Exception:
                pass
            await self._close_audio_providers()
            self._turn = None
            self._destroy()
            self._mark_closed()
        finally:
            self._mark_observability_inactive()
            self._stopping = False

    async def shutdown(self) -> None:
        """Force-cancel in-flight work, then release backend resources.

        Thin alias for ``stop(force=True)`` kept so existing callers
        (and the ``async with`` exit path) need not change.  New code
        should prefer ``async with session:`` or ``stop(force=...)``.
        """
        await self.stop(force=True)

    def _close(self) -> None:
        """Finalize the session journal without tearing down backends.

        Writes the clean-close marker so the journal is marked as
        properly shut down. This is the logical end-of-session marker,
        not the physical resource teardown step.

        Internal: the public teardown path is ``async with session:`` or
        :meth:`stop`, which call :meth:`_destroy` (and hence this) for
        you while preserving a read-only post-stop debug surface.  Safe
        to call multiple times.
        """
        if self._flushed:
            return
        self._flushed = True
        if self._journal:
            self._journal.finalize()

    def _destroy(self) -> None:
        """Release live debug backends while keeping post-stop inspection working.

        This closes backend resources such as SQLite connections,
        Litestream sidecars, libSQL sync threads, and in-memory artifact
        stores. The session retains a read-only postmortem view, so
        ``session.journal.read()`` and ``export_debug_bundle()`` continue
        to work after :meth:`stop`.

        Internal: invoked by :meth:`stop` (and the ``async with`` exit
        path).  Safe to call multiple times.
        """
        self._close()  # ensure the clean-close marker is written first

        if self._journal:
            live_journal = self._journal
            replacement = self._preserve_journal_after_destroy(live_journal)
            live_journal.close()
            self._journal = replacement
            # Update the cached JournalView so previously-cached references
            # (e.g. ``view = session.journal``) transparently delegate to
            # the read-only snapshot instead of the now-closed backend.
            if self._journal_view is not None:
                self._journal_view._journal = replacement

        if self._artifact_store:
            live_store = self._artifact_store
            replacement_store = self._preserve_artifacts_after_destroy(live_store)
            live_store.close()
            self._artifact_store = replacement_store

        self._journal_sink.replace_backends(
            journal=self._journal,
            artifact_store=self._artifact_store,
        )

    def _preserve_journal_after_destroy(self, journal: ExecutionJournal) -> ExecutionJournal:
        db_path = getattr(journal, "db_path", None)
        if db_path is not None:
            return ReadonlySqliteJournal(db_path, degraded=journal.degraded)
        if isinstance(journal, InMemoryRingBuffer):
            return journal.snapshot()
        return journal

    def _preserve_artifacts_after_destroy(self, artifact_store: Any) -> Any:
        store = getattr(artifact_store, "_store", None)
        if isinstance(store, dict):
            return SnapshotArtifactStore(store)
        return artifact_store

    # ── Cancellation ───────────────────────────────────────────

    async def cancel_turn(self, *, barge_in: bool = False) -> None:
        """Trigger cancel token, abort STT/agent/TTS, reset turn state.

        If barge_in is True, emits an Interruption event and delegates
        upstream ``InterruptSignal`` propagation to the
        :class:`CancelOrchestrator` so every stage records its own
        ``ControlSignalRecord`` (WS3 T3.8 dual-path coexistence: signal
        flow runs alongside the legacy cancel token).
        """
        turn = self._turn
        if turn:
            turn.cancel_token.cancel()

        if barge_in:
            if turn:
                turn.record_barge_in()
            await self._emit(Interruption())
            await self._cancel.propagate_signal(
                _InterruptSignal(signal_id=f"barge-in-{uuid4().hex[:8]}"),
                cause="barge_in",
            )

        await self._stt_committer.cancel(turn)
        await self._tts_scheduler.cancel()
        await clear_audio_if_supported(self.transport)
        self._outbound_queue.flush_for_new_turn()
        self._audio_router.reset_replay_chunks()

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
        self._tts_scheduler.set_playback_suppressed(True)
        await self._tts_scheduler.synthesizer.cancel()
        await clear_audio_if_supported(self.transport)
        self._outbound_queue.flush_for_new_turn()
        self._audio_router.reset_replay_chunks()
        if self._turn_manager.state == TurnManagerState.BOT_SPEAKING:
            self._reset_turn_state()

    async def reset_state(self) -> None:
        """Cancel everything and return to idle/listening state.

        Also clears agent conversation history if the agent supports it.
        """
        turn = self._turn
        if turn:
            turn.cancel_token.cancel()

        await self._stt_committer.cancel(turn)
        await self._tts_scheduler.cancel()
        await clear_audio_if_supported(self.transport)
        self._outbound_queue.flush_for_new_turn()
        self._audio_router.reset_replay_chunks()

        self.agent.reset()

        self._reset_turn_state()

    # ── Session actions ───────────────────────────────────────

    def register_action_executor(self, executor: SessionActionExecutor) -> None:
        """Register a session action executor.

        Executors are tried in the order they were registered. The first
        executor whose ``supports(...)`` method returns true handles the action.
        """
        self._action_executors.insert(0, executor)

    async def _drain_session_actions(self) -> bool:
        """Execute any session actions queued by agent tools during this turn.

        Returns ``True`` if any executor signalled that the session should stop.
        """
        should_stop = False
        if self._session_actions is None or not self._session_actions.has_pending:
            return should_stop

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

            should_stop = should_stop or result.stop_session
            await self._emit(
                SessionActionCompleted(
                    action=action,
                    executor=executor_name,
                    result=result,
                )
            )

        return should_stop

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

    def _on_call_answered_greet(self, event: Any) -> None:
        """Schedule the configured greeting once per call.

        Wires :attr:`_greeting` into the first
        :class:`~easycat.events.CallAnswered`.  The actual TTS work is
        detached from event dispatch so outbound status callbacks can
        complete lifecycle/AMD processing without waiting on synthesis.
        Uses the
        ``synthesize_bypass`` path so the greeting plays even when a
        classification gate is still buffering (outbound answering
        machine window).  Subsequent ``CallAnswered`` events — e.g. a
        warm-transfer re-answer — are ignored.
        """
        if self._greeting_spoken or not self._greeting:
            return
        if self._greeting_task is not None and not self._greeting_task.done():
            return
        task = self._runtime_scope.create_journaled_task(
            self._deliver_call_answered_greeting(self._greeting),
            name="call_answered_greeting",
            journal_sink=self._journal_sink,
        )
        self._greeting_task = task
        task.add_done_callback(self._clear_greeting_task)

    def _clear_greeting_task(self, task: asyncio.Task[Any]) -> None:
        if self._greeting_task is task:
            self._greeting_task = None

    async def _deliver_call_answered_greeting(self, greeting: str) -> None:
        await asyncio.sleep(0)
        try:
            await self.synthesize_bypass(greeting)
            self._greeting_spoken = True
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Failed to synthesize greeting", exc_info=True)

    async def _cancel_greeting_task(self) -> None:
        task = self._greeting_task
        if task is not None and not task.done():
            await self._runtime_scope.cancel_and_drain("call_answered_greeting")
        self._greeting_task = None

    async def _on_stt_final_opt_out(self, event: STTFinal) -> None:
        """Detect TCPA opt-out phrases in every STT final and react.

        Skipped when the caller disabled ``opt_out_detection`` or when
        the text is empty.  On match: emits
        :class:`~easycat.events.OptOutDetected`, adds the caller to
        ``session.dnc_list`` if set, and enqueues a
        :class:`EndCallAction` so the call terminates after the
        current agent utterance.
        """
        from easycat.events import OptOutDetected
        from easycat.telephony.compliance import match_opt_out_phrase

        if not event.text:
            return
        phrase = match_opt_out_phrase(event.text, self._opt_out_phrases)
        if phrase is None:
            return

        number = self._call_identity.caller_number if self._call_identity else ""
        if self._dnc_list is not None and number:
            try:
                self._dnc_list.add(number)
            except Exception:
                logger.debug("dnc_list.add raised for opt-out", exc_info=True)

        await self._emit(OptOutDetected(number=number, phrase=phrase, text=event.text))

        # Queue a hangup after the current agent utterance so the
        # session drains cleanly (saying "understood, goodbye" first
        # is the agent's job; we just schedule the end).  When no
        # action queue is present we fall back to firing
        # ``Session.stop`` — opt-out must terminate the call.
        if self._session_actions is not None:
            self._session_actions.end_call(reason="opt_out")
        else:
            self._runtime_scope.create_journaled_task(
                self.stop(),
                name="opt_out_stop",
                journal_sink=self._journal_sink,
            )

    def _stop_helpers(self) -> None:
        """Stop attached helper components that own event subscriptions/state."""
        for helper in self._telephony_helpers:
            try:
                helper.stop()
            except Exception:
                logger.debug("Error stopping session helper", exc_info=True)

    async def _close_audio_providers(self) -> None:
        """Release optional resources owned by audio providers."""
        providers = (
            ("stt", self.stt),
            ("tts", self.tts),
            ("vad", self.vad),
            ("noise_reducer", self.noise_reducer),
            ("echo_canceller", self.echo_canceller),
        )
        closed: set[int] = set()
        for name, provider in providers:
            provider_id = id(provider)
            if provider_id in closed:
                continue
            closed.add(provider_id)
            try:
                await close_if_supported(provider)
            except Exception:
                logger.debug("Error closing %s provider", name, exc_info=True)

    # ── Test-compat shims ──────────────────────────────────────
    #
    # The turn-loop logic lives on :class:`TurnRunner`.  These thin
    # delegates exist only so tests that poke Session's private turn
    # surface keep working — don't add logic here.

    async def _on_turn_started(self, event: TurnStarted) -> None:
        await self._turn_runner.on_turn_started(event)

    def _schedule_turn_ended(self, event: TurnEnded) -> None:
        self._turn_runner.schedule_turn_ended(event)

    async def _on_turn_ended(
        self,
        event: TurnEnded,
        generation: int,
        turn: TurnContext | None = None,
    ) -> None:
        await self._turn_runner.on_turn_ended(event, generation, turn=turn)

    async def _handle_end_of_speech(self, turn: TurnContext | None = None) -> None:
        await self._turn_runner.handle_end_of_speech(turn=turn)

    async def _run_streaming_agent(
        self,
        transcript: str,
        token: CancelToken | None,
        *,
        turn: TurnContext | None = None,
    ) -> None:
        await self._turn_runner.run_streaming_agent(transcript, token, turn=turn)

    async def _execute_text_turn(self, text: str, cancel_token: CancelToken | None = None) -> str:
        return await self._turn_runner._execute_text_turn(text, cancel_token)

    # ── Internal helpers ───────────────────────────────────────

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
        self._mark_observability_active()
        try:
            with observability.span("easycat.session", {"easycat.surface": "agent_bridge"}):
                return await self._turn_runner.send_text(text)
        finally:
            self._mark_observability_inactive()
