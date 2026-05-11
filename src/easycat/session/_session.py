"""Session: the core runtime for a single voice conversation.

Manages the voice pipeline lifecycle, wires provider stages together,
and handles turn state and cancellation.  Drives the agent bridge
through a single streaming path and feeds incremental TTS synthesis on
sentence boundaries for low-latency playback.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any, TypeVar
from uuid import uuid4

from easycat.audio_format import AudioChunk
from easycat.bounded_queue import BoundedAudioQueue, DropPolicy
from easycat.cancel import CancelToken
from easycat.echo_cancellation import PassthroughAEC
from easycat.events import (
    AgentDelta,
    AgentFinal,
    AgentRequestStarted,
    AudioIn,
    AudioOut,
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
    TransportAudioDelivered,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.health_check import PeriodicHealthChecker
from easycat.integrations.agents._agent_runner import AgentRunner
from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.integrations.agents.base import ExternalAgentBridge
from easycat.llm_output_processing import (
    LLMOutputProcessor,
    apply_output_processors,
)
from easycat.noise_reduction import PassthroughNoiseReducer
from easycat.runtime.artifacts import SnapshotArtifactStore
from easycat.runtime.capabilities import (
    PlaybackAcknowledgements,
    aclose_if_supported,
    clear_audio_if_supported,
    close_if_supported,
    health_checkable,
    is_active_provider,
    is_passthrough_provider,
    playback_acknowledgements,
    transport_reports_audio_delivery,
)
from easycat.runtime.context import RunContext
from easycat.runtime.journal import (
    ExecutionJournal,
    InMemoryRingBuffer,
    JournalView,
    ReadonlySqliteJournal,
)
from easycat.runtime.records import JournalRecordKind
from easycat.runtime.scope import RuntimeScope
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._streaming import consume_agent_stream
from easycat.session._text import (
    _chunk_has_speech_energy,
    _text_for_estimation_timeline,
)
from easycat.session._turn_context import TurnContext
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
from easycat.session.interruption import (
    estimate_and_notify_interruption,
)
from easycat.session.interruption import (
    notify_bridge_interruption as _notify_bridge_interruption,
)
from easycat.stages.agent import AgentStage
from easycat.stages.audio import AudioStage
from easycat.stages.base import (
    ControlSignal as _ControlSignal,
)
from easycat.stages.base import (
    InterruptSignal as _InterruptSignal,
)
from easycat.stages.base import journal_append_control_signal as _journal_control_signal
from easycat.stages.stt import STTStage
from easycat.stages.transport import TransportStage
from easycat.stages.tts import TTSStage
from easycat.stages.turn import TurnStage
from easycat.stages.vad import VADStage
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

_HelperT = TypeVar("_HelperT")


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
        # TurnManager emits a ``turn_state_changed`` journal record on
        # every state transition so bundle readers can answer "why did
        # it go to PROCESSING" from the journal alone.
        self._turn_manager.bind_journal_hook(self._on_turn_state_changed)
        self.event_bus.subscribe(TurnStarted, self._on_turn_started)
        self.event_bus.subscribe(TurnEnded, self._schedule_turn_ended)
        self.event_bus.subscribe(VADStopSpeaking, self._schedule_stt_segment_commit)
        self.event_bus.subscribe(VADStartSpeaking, self._cancel_scheduled_stt_segment_commit)
        self.event_bus.subscribe(PlaybackMarkAck, self._on_playback_mark_ack)
        self.event_bus.subscribe(TransportAudioDelivered, self._on_transport_audio_delivered)

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
        self._stt_segment_silence_ms = max(0, getattr(tm_config, "stt_segment_silence_ms", 0))

        # Reliability/observability config
        self._timeout_config = cfg.timeout_config or self._default_timeout_config()
        self._journal = cfg.journal
        self._journal_view: JournalView | None = (
            JournalView(self._journal) if self._journal is not None else None
        )
        self._artifact_store = cfg.artifact_store

        # Backpressure (outbound audio queue)
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
        self._closed_event: asyncio.Event | None = None
        self._pipeline_task: asyncio.Task[None] | None = None
        self._stt_task: asyncio.Task[None] | None = None
        self._current_tts_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._stt_final_future: asyncio.Future[str] | None = None
        self._stt_pending_segment_futures: list[asyncio.Future[str]] = []
        self._stt_pause_commit_task: asyncio.Task[None] | None = None
        self._stt_segment_commit_task: asyncio.Task[None] | None = None

        # STT stream started for current turn
        self._stt_active = False
        self._tts_playback_suppressed = False
        self._auto_turn_speech_frames = 0

        # Per-turn state — created fresh at each turn start.
        # _turn_generation is a monotonic counter that increases each time a
        # new turn starts, used to detect stale callbacks from previous turns.
        self._turn: TurnContext | None = None
        self._turn_generation: int = 0
        self._replay_chunks_pending: int = 0
        self._playback_mark_bytes_interval: int = 4_000  # throttle: ~125ms at 16kHz/16-bit
        self._playback_mark_seq: int = 0  # session-scoped so mark names never collide across turns

        self._playback_ack_transport: PlaybackAcknowledgements | None = playback_acknowledgements(
            self.transport
        )
        self._transport_reports_audio_delivery = transport_reports_audio_delivery(self.transport)

        self.session_id = cfg.session_id or f"session-{uuid4().hex[:12]}"
        self._runtime_mode = cfg.runtime_mode
        self._active_text_turn: asyncio.Task[str] | None = None
        self._text_turn_cancel_token: CancelToken | None = None
        self._text_turn_accumulated: str = ""
        self._text_turn_lock = asyncio.Lock()
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
        # Hand the TTS stage to the synthesizer so the existing iteration
        # loop goes through stage.execute() instead of tts.synthesize()
        # directly.  The synthesizer needs ctx + turn at call time and
        # pulls them from these accessors rather than holding references
        # that could go stale across turn resets.
        self._tts_synth.bind_stage(
            self._tts_stage,
            run_ctx_getter=lambda: self._run_ctx,
            turn_getter=lambda: self._turn or self._no_turn,
        )
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

    def _journaled_task(
        self,
        coro: Any,
        *,
        name: str,
        turn_id: str | None = None,
    ) -> asyncio.Task[Any]:
        """Create an asyncio task that journals its full lifecycle.

        Emits ``task_scheduled`` at creation, then one of
        ``task_completed`` / ``task_cancelled`` / ``task_raised`` when
        the task finishes.  A bundle reader can reconstruct a Gantt
        chart of concurrent awaits — enough to diagnose races like the
        plan-7 STT-commit-vs-end-stream interleave without re-running
        the live providers.

        *name* is the stable label that survives replay (e.g.
        ``"stt_pause_commit"``, ``"tts_synth"``, ``"on_turn_ended"``).
        Use one per logical task — don't baseline it on Python object
        ids, which don't survive serialisation.
        """
        resolved_turn = self._journal_turn_id(turn_id)
        self._journal_sink.append_record(
            name="task_scheduled",
            turn_id=resolved_turn,
            data={"task_name": name},
        )
        task = asyncio.create_task(coro, name=name)

        def _on_done(
            t: asyncio.Task[Any],
            label: str = name,
            tid: str | None = resolved_turn,
        ) -> None:
            # Pick the right terminal record kind.  We look at exception
            # first so a task that's both cancelled *and* had raised in
            # finally-cleanup reports the raise (more actionable).
            try:
                if t.cancelled():
                    self._journal_sink.append_record(
                        name="task_cancelled", turn_id=tid, data={"task_name": label}
                    )
                    return
                exc = t.exception()
            except asyncio.CancelledError:
                self._journal_sink.append_record(
                    name="task_cancelled", turn_id=tid, data={"task_name": label}
                )
                return
            if exc is not None:
                self._journal_sink.append_record(
                    name="task_raised",
                    turn_id=tid,
                    data={"task_name": label, "exc_type": type(exc).__name__},
                )
            else:
                self._journal_sink.append_record(
                    name="task_completed", turn_id=tid, data={"task_name": label}
                )

        task.add_done_callback(_on_done)
        return task

    async def _propagate_upstream_signal(
        self,
        signal: _ControlSignal,
        *,
        cause: str | None = None,
    ) -> None:
        """Walk the upstream signal through every stage, late → early.

        WS3 T3.8: control signals propagate from late stages (TTS,
        Transport) back toward early stages (VAD, STT) so each one can
        observe the event in journal order.  Each stage's
        ``handle_upstream`` writes a ``ControlSignalRecord`` so a replay
        can see who saw the signal and when.

        Errors inside ``handle_upstream`` are isolated per-stage —
        signal propagation must not throw and break the legacy cancel
        path that the same caller relies on.
        """
        ordered = (
            self._transport_stage,
            self._tts_stage,
            self._agent_stage,
            self._turn_stage,
            self._stt_stage,
            self._vad_stage,
            self._audio_stage,
        )
        for stage in ordered:
            if stage is None:
                continue
            try:
                await stage.handle_upstream(signal, self._run_ctx)
            except Exception:  # noqa: BLE001 - never break cancel path
                logger.exception("Stage %s.handle_upstream failed", stage.name)
        # Telephony helpers journal the signal without a dedicated stage
        # wrapper: one bare aggregate control-signal record keeps the
        # observability identical to the old stage path.
        if self._telephony_helpers:
            _journal_control_signal(self._run_ctx, stage="telephony", signal=signal)
        # Annotate the trailing signal record with the originating cause
        # so the replay UI can display "interrupt — barge_in" instead of
        # bare signal IDs.
        if cause:
            self._journal_sink.append_record(
                kind=JournalRecordKind.CONTROL,
                name="control_signal_cause",
                turn_id=self._turn.id if self._turn else None,
                data={"signal_id": signal.signal_id, "cause": cause},
            )

    def _record_markdown_strip(
        self,
        *,
        phase: str,
        original_text: str,
        stripped_text: str,
        turn_id: str | None = None,
    ) -> None:
        """Append a journal record when final-response markdown stripping runs."""
        self._journal_sink.append_record(
            name="markdown_stripped",
            turn_id=turn_id,
            data={
                "phase": phase,
                "changed": original_text != stripped_text,
                "original_text": original_text,
                "stripped_text": stripped_text,
            },
        )

    def _record_tts_payload_prepared(
        self,
        *,
        original_text: str,
        original_format: str,
        prepared_payload: TTSInput,
        is_streaming: bool,
        is_final: bool,
        turn_id: str | None = None,
    ) -> None:
        self._journal_sink.append_record(
            name="tts_payload_prepared",
            turn_id=turn_id,
            data={
                "is_streaming": is_streaming,
                "is_final": is_final,
                "changed": (
                    original_text != prepared_payload.text
                    or original_format != prepared_payload.format
                ),
                "original_text": original_text,
                "original_format": original_format,
                "prepared_text": prepared_payload.text,
                "prepared_format": prepared_payload.format,
                "processors": [type(processor).__name__ for processor in self._output_processors],
                "ssml_downgraded": (
                    original_format == "ssml" and prepared_payload.format == "plain"
                ),
            },
        )

    def _record_interruption_notification(
        self,
        *,
        source: str,
        mode: str,
        text_spoken: str,
        notified: bool,
        turn_id: str | None = None,
    ) -> None:
        replacement_text = None
        self._journal_sink.append_record(
            name="assistant_interruption_notified",
            turn_id=turn_id,
            data={
                "source": source,
                "mode": mode,
                "text_spoken": text_spoken,
                "notified": notified,
                "replacement_text": replacement_text,
            },
        )

    def _reset_turn_state(self) -> None:
        """Clear turn correlation state and reset the turn manager."""
        self._cancel_scheduled_stt_segment_commit()
        self._cancel_inflight_stt_segment_commit()
        self._resolve_pending_stt_segment_futures("")
        if self._stt_final_future and not self._stt_final_future.done():
            self._stt_final_future.set_result("")
        self._stt_final_future = None
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
        await self.shutdown()

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
                self._tts_synth._outbound_queue = self._outbound_queue

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
                    )
                    checker.start()
                    self._health_checkers.append(checker)

            for helper in self._telephony_helpers:
                helper.start()

            self._is_running = True
            self._outbound_task = asyncio.create_task(self._drain_outbound_audio())
            self._pipeline_task = asyncio.create_task(self._run_pipeline())
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

            for task_name in ("_pipeline_task", "_outbound_task"):
                task = getattr(self, task_name)
                if task is not None and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                setattr(self, task_name, None)
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

    async def stop(self) -> None:
        """Gracefully stop the session and release live backend resources."""
        if self._closed or self._stopping:
            return
        self._stopping = True
        self._is_running = False
        current_task = asyncio.current_task()

        try:
            if self._turn:
                self._turn.cancel_token.cancel()

            # Cancel any in-flight text turn so it doesn't emit events
            # after the session is torn down.
            if self._text_turn_cancel_token:
                self._text_turn_cancel_token.cancel()
            text_task = self._active_text_turn
            if text_task is not None and not text_task.done():
                text_task.cancel()
                try:
                    await text_task
                except (asyncio.CancelledError, Exception):
                    pass

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

            await self._cancel_greeting_task()
            await self._cancel_stt()
            await self._cancel_tts()
            for checker in self._health_checkers:
                await checker.stop()
            self._health_checkers = []
            self._stop_helpers()
            if not self._outbound_queue_external:
                self._outbound_queue.close()
            # Cancel the outbound drain task BEFORE disconnecting the
            # transport — otherwise the task may hang on send_audio()
            # with a disconnected transport.
            if self._outbound_task and not self._outbound_task.done():
                self._outbound_task.cancel()
                try:
                    await self._outbound_task
                except (asyncio.CancelledError, Exception):
                    pass
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
            self.destroy()
            self._mark_closed()
        finally:
            self._stopping = False

    async def shutdown(self) -> None:
        """Force-cancel in-flight work, then release live backend resources."""
        if self._closed or self._stopping:
            return
        self._stopping = True
        self._is_running = False

        try:
            if self._turn:
                self._turn.cancel_token.cancel()

            # Cancel any in-flight text turn.
            if self._text_turn_cancel_token:
                self._text_turn_cancel_token.cancel()
            text_task = self._active_text_turn
            if text_task is not None and not text_task.done():
                text_task.cancel()
                try:
                    await text_task
                except (asyncio.CancelledError, Exception):
                    pass

            tasks: list[asyncio.Task[Any]] = []
            if self._pipeline_task and not self._pipeline_task.done():
                self._pipeline_task.cancel()
                tasks.append(self._pipeline_task)
            if self._current_tts_task and not self._current_tts_task.done():
                self._current_tts_task.cancel()
                tasks.append(self._current_tts_task)
            if self._outbound_task and not self._outbound_task.done():
                self._outbound_task.cancel()
                tasks.append(self._outbound_task)

            # Signal scoped work before awaiting other task handles so
            # migrated shutdown work preserves the previous force-cancel
            # ordering. Drain below after every task has observed cancellation.
            self._runtime_scope.cancel()
            for task in tasks:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._cancel_stt()
            # RuntimeScope-owned work currently covers heartbeat, greeting,
            # and STT segment commit/pause tasks. These can outlive the
            # pipeline/STT consumer handles above, so shutdown drains the
            # scope before provider teardown returns.
            await self._runtime_scope.cancel_and_drain()
            self._stt_pause_commit_task = None
            self._stt_segment_commit_task = None
            self._greeting_task = None
            self._heartbeat_task = None

            for checker in self._health_checkers:
                await checker.stop()
            self._health_checkers = []
            self._stop_helpers()
            if not self._outbound_queue_external:
                self._outbound_queue.close()
            await self.transport.disconnect()
            await self._turn_manager.shutdown()
            try:
                await aclose_if_supported(self.agent)
            except Exception:
                pass
            await self._close_audio_providers()
            self._turn = None
            self.destroy()
            self._mark_closed()
        finally:
            self._stopping = False

    def close(self) -> None:
        """Finalize the session journal without tearing down backends.

        Writes the clean-close marker so the journal is marked as
        properly shut down. This is the logical end-of-session marker,
        not the physical resource teardown step.

        Most callers should use :meth:`destroy` or the higher-level
        :meth:`stop` / :meth:`shutdown`, which release live backend
        resources while preserving a read-only post-stop debug surface.
        Safe to call multiple times.
        """
        if self._flushed:
            return
        self._flushed = True
        if self._journal:
            self._journal.finalize()

    def destroy(self) -> None:
        """Release live debug backends while keeping post-stop inspection working.

        This closes backend resources such as SQLite connections,
        Litestream sidecars, libSQL sync threads, and in-memory artifact
        stores. The session retains a read-only postmortem view, so
        ``session.journal.read()`` and ``export_debug_bundle()`` continue
        to work after :meth:`stop` / :meth:`shutdown`.

        Safe to call multiple times.
        """
        self.close()  # ensure the clean-close marker is written first

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

        If barge_in is True, emits an Interruption event and dispatches
        an upstream ``InterruptSignal`` through every stage so each one
        records its own ``ControlSignalRecord`` (WS3 T3.8 dual-path
        coexistence: signal flow runs alongside the legacy cancel token).
        """
        if self._turn:
            self._turn.cancel_token.cancel()

        if barge_in:
            if self._turn:
                self._turn.record_barge_in()
            await self._emit(Interruption())
            await self._propagate_upstream_signal(
                _InterruptSignal(signal_id=f"barge-in-{uuid4().hex[:8]}"),
                cause="barge_in",
            )

        await self._cancel_stt()
        await self._cancel_tts()
        await clear_audio_if_supported(self.transport)
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
        await clear_audio_if_supported(self.transport)
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
        await clear_audio_if_supported(self.transport)
        self._outbound_queue.flush_for_new_turn()
        self._replay_chunks_pending = 0

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
        task = self._runtime_scope.add_task(
            "call_answered_greeting",
            self._journaled_task(
                self._deliver_call_answered_greeting(self._greeting),
                name="call_answered_greeting",
            ),
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
            self._journaled_task(self.stop(), name="opt_out_stop")

    async def _on_turn_started(self, event: TurnStarted) -> None:
        """Handle TurnStarted from TurnManager: start STT and prime pre-roll."""
        if not self._is_running:
            return

        self._cancel_scheduled_stt_segment_commit()
        self._cancel_inflight_stt_segment_commit()
        self._resolve_pending_stt_segment_futures("")
        self._stt_final_future = None

        # Cancel the previous turn's token so any in-flight agent/TTS work
        # notices the cancellation before we overwrite self._turn.
        prev = self._turn
        if prev and not prev.cancel_token.is_cancelled:
            prev.cancel_token.cancel()

        cancel_token = self._turn_manager.cancel_token or CancelToken()
        self._turn = TurnContext(turn_id=event.turn_id, cancel_token=cancel_token)
        self._turn_generation = self._turn.generation
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
            await self._stt_stage.execute(chunk, self._run_ctx, self._turn or self._no_turn)
            if self._turn is not None:
                self._turn.stt_has_uncommitted_audio = True

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

    def _schedule_turn_ended(self, event: TurnEnded) -> None:
        """Schedule end-of-turn processing without blocking other handlers.

        Cancels BOTH the scheduled pause-commit task and any in-flight
        segment-commit task before running ``_on_turn_ended``.  The
        in-flight cancel is the fix for the commit race that showed up
        as OpenAI Realtime "buffer too small" errors on plan-7: without
        it, ``_commit_stt_segment`` could race with ``_handle_end_of_speech``
        — the pause commit clears the STT server buffer, a few trailing
        audio frames sneak in after that, and ``end_stream``'s commit
        then fails with < 100ms of audio.
        """
        self._cancel_scheduled_stt_segment_commit()
        self._cancel_inflight_stt_segment_commit()
        if self._current_tts_task and not self._current_tts_task.done():
            self._current_tts_task.cancel()
        gen = self._turn_generation
        self._current_tts_task = self._journaled_task(
            self._on_turn_ended(event, gen),
            name="on_turn_ended",
            turn_id=event.turn_id,
        )
        self._current_tts_task.add_done_callback(self._log_task_exception)

    async def _on_turn_ended(self, event: TurnEnded, generation: int) -> None:
        """Handle TurnEnded from TurnManager: finalize STT and run agent/TTS."""
        if self._turn_generation != generation:
            return
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

    def _cancel_scheduled_stt_segment_commit(self, _event: VADStartSpeaking | None = None) -> None:
        task = self._stt_pause_commit_task
        if task is not None and not task.done():
            task.cancel()
        self._stt_pause_commit_task = None

    def _cancel_inflight_stt_segment_commit(self) -> None:
        task = self._stt_segment_commit_task
        if task is not None and not task.done():
            task.cancel()
        self._stt_segment_commit_task = None

    def _resolve_pending_stt_segment_futures(self, value: str) -> None:
        while self._stt_pending_segment_futures:
            future = self._stt_pending_segment_futures.pop(0)
            if not future.done():
                future.set_result(value)

    def _schedule_stt_segment_commit(self, _event: VADStopSpeaking) -> None:
        """Finalize the current STT segment on a shorter pause than turn end."""
        if not self._stt_active or self._turn is None or self._auto_turn_from_stt_final:
            return
        self._cancel_scheduled_stt_segment_commit()
        delay_s = self._stt_segment_silence_ms / 1000.0
        self._stt_pause_commit_task = self._runtime_scope.add_task(
            "stt_pause_commit",
            self._journaled_task(
                self._commit_stt_segment_after(delay_s),
                name="stt_pause_commit",
            ),
        )
        self._stt_pause_commit_task.add_done_callback(self._log_task_exception)

    async def _commit_stt_segment_after(self, delay_s: float) -> None:
        if delay_s > 0:
            await asyncio.sleep(delay_s)
        if self._turn_manager.state != TurnManagerState.USER_PAUSED:
            return
        await self._start_stt_segment_commit()

    async def _start_stt_segment_commit(self) -> None:
        turn = self._turn
        if (
            turn is None
            or turn.cancel_token.is_cancelled
            or not self._stt_active
            or not turn.stt_has_uncommitted_audio
        ):
            return
        if self._stt_segment_commit_task is not None and not self._stt_segment_commit_task.done():
            return
        self._stt_segment_commit_task = self._runtime_scope.add_task(
            "stt_segment_commit",
            self._journaled_task(
                self._commit_stt_segment(),
                name="stt_segment_commit",
                turn_id=turn.id if turn is not None else None,
            ),
        )
        self._stt_segment_commit_task.add_done_callback(self._log_task_exception)

    async def _commit_stt_segment(self) -> None:
        turn = self._turn
        commit_segment = getattr(self.stt, "commit_segment", None)
        if (
            turn is None
            or not callable(commit_segment)
            or turn.cancel_token.is_cancelled
            or not turn.stt_has_uncommitted_audio
        ):
            return

        next_segment_index = len(turn.stt_segments) + 1
        # Pull the provider's pending-commit byte count (if exposed)
        # into the journal so bundles show *why* a commit was skipped
        # or accepted.  ``OpenAIRealtimeSTT`` tracks this precisely;
        # providers without the attribute report None and the journal
        # reader treats it as unknown.
        pending_bytes = getattr(self.stt, "_bytes_since_last_commit", None)
        self._journal_sink.append_record(
            name="stt_segment_commit_requested",
            turn_id=turn.id,
            data={
                "segment_index": next_segment_index,
                "transcript_text": turn.transcript_text,
                "pending_commit_bytes": (
                    int(pending_bytes) if isinstance(pending_bytes, int) else None
                ),
            },
        )
        turn.stt_has_uncommitted_audio = False
        future = asyncio.get_running_loop().create_future()
        self._stt_pending_segment_futures.append(future)
        committed = False
        try:
            committed = await commit_segment()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("STT segment commit failed", exc_info=True)
        finally:
            self._journal_sink.append_record(
                name="stt_segment_commit_result",
                turn_id=turn.id,
                data={
                    "segment_index": next_segment_index,
                    "committed": committed,
                    "transcript_text": turn.transcript_text,
                },
            )
            if not committed:
                turn.stt_has_uncommitted_audio = True
                if future in self._stt_pending_segment_futures:
                    self._stt_pending_segment_futures.remove(future)
                if not future.done():
                    future.set_result("")
            self._stt_segment_commit_task = None

    async def _await_pending_stt_segments(self) -> bool:
        timeout = self._timeout_config.stt_timeout if self._timeout_config else None
        while self._stt_pending_segment_futures:
            future = self._stt_pending_segment_futures[0]
            try:
                if timeout:
                    await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
                else:
                    await future
            except TimeoutError:
                err = STTTimeoutError("stt", timeout)
                await self._emit(Error(exception=err, stage=ErrorStage.STT))
                return False
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Ignoring STT segment wait failure", exc_info=True)
            finally:
                if (
                    self._stt_pending_segment_futures
                    and self._stt_pending_segment_futures[0] is future
                ):
                    self._stt_pending_segment_futures.pop(0)
        return True

    def _start_stt_event_task(self) -> None:
        """Start background consumption of provider-scoped STT events."""
        if self._stt_task and not self._stt_task.done():
            self._stt_task.cancel()
        self._stt_final_future = None

        async def _consume() -> None:
            my_task = asyncio.current_task()
            turn = self._turn
            try:
                async for stt_event in self.stt.events():
                    if turn and turn.cancel_token.is_cancelled:
                        break
                    if stt_event.type == STTEventType.PARTIAL:
                        await self._emit(STTPartial(text=stt_event.text, track=stt_event.track))
                    elif stt_event.type == STTEventType.FINAL:
                        if turn:
                            if not self._stt_pending_segment_futures:
                                turn.stt_has_uncommitted_audio = False
                            turn.append_stt_segment(stt_event.text, track=stt_event.track)
                            self._journal_sink.append_record(
                                name="stt_segment_final",
                                turn_id=turn.id,
                                data={
                                    "segment_index": len(turn.stt_segments),
                                    "text": stt_event.text,
                                    "track": stt_event.track,
                                    "transcript_text": turn.transcript_text,
                                },
                            )
                        await self._emit(STTFinal(text=stt_event.text, track=stt_event.track))
                        if self._stt_pending_segment_futures:
                            future = self._stt_pending_segment_futures.pop(0)
                            if not future.done():
                                future.set_result(stt_event.text)
                        if self._auto_turn_from_stt_final:
                            await self._turn_manager.end_turn()
            except Exception as exc:
                logger.exception("STT event loop error")
                await self._emit(Error(exception=exc, stage=ErrorStage.STT))
            finally:
                # A predecessor consumer canceled by _start_stt_event_task()
                # must not clear futures that the successor has already
                # enqueued for the new turn.  Only the current owner of
                # self._stt_task is allowed to touch the shared list here.
                if self._stt_task is my_task:
                    self._resolve_pending_stt_segment_futures("")

        self._stt_task = asyncio.create_task(_consume())

    # ── Pipeline ───────────────────────────────────────────────

    async def _run_pipeline(self) -> None:
        """Main audio receive loop: Transport -> Noise Reduction -> AEC -> VAD -> STT."""
        try:
            async for chunk in self.transport.receive_audio():
                if not self._is_running:
                    break

                await self._emit(AudioIn(chunk=chunk))

                # Stages 1-2: Noise reduction + Echo cancellation via AudioStage.
                # AudioStage wraps both so a single journal record covers
                # the pair — matches WS3 T3.10's intent that Audio is
                # one stage for replay purposes.
                if self._enable_noise_reduction or self._enable_aec:
                    chunk = await self._audio_stage.execute(
                        chunk, self._run_ctx, self._turn or self._no_turn
                    )

                # Stage 3: VAD (optional) via VADStage.
                if self._enable_vad:
                    vad_events = await self._vad_stage.execute(
                        chunk, self._run_ctx, self._turn or self._no_turn
                    )
                    for vad_event in vad_events:
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

                if self._stt_active and not started_turn_from_chunk:
                    if self._turn is not None:
                        self._turn.stt_has_uncommitted_audio = True
                    await self._stt_stage.execute(
                        chunk, self._run_ctx, self._turn or self._no_turn
                    )

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
        turn = self._turn
        token = turn.cancel_token if turn else None
        self._cancel_scheduled_stt_segment_commit()

        # Stop forwarding audio to STT immediately so trailing frames
        # from continuous transports don't leak into the transcript.
        stt_needs_close = self._stt_active
        self._stt_active = False

        if self._stt_segment_commit_task and not self._stt_segment_commit_task.done():
            await self._stt_segment_commit_task

        if not await self._await_pending_stt_segments():
            if self._turn is turn:
                self._reset_turn_state()
            return

        if stt_needs_close:
            if turn is not None and turn.stt_has_uncommitted_audio:
                turn.stt_has_uncommitted_audio = False
                future = asyncio.get_running_loop().create_future()
                self._stt_pending_segment_futures.append(future)
            await self.stt.end_stream()

        if not await self._await_pending_stt_segments():
            if self._turn is turn:
                self._reset_turn_state()
            return

        transcript = ""
        if turn is not None:
            transcript = turn.transcript_text
        if not transcript and self._stt_final_future is not None:
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

        if transcript:
            if self._stt_final_future is not None and not self._stt_final_future.done():
                self._stt_final_future.set_result(transcript)
            if turn:
                turn.stt_final_time = time.monotonic()

        if not transcript or (token and token.is_cancelled):
            if self._turn is turn:
                self._reset_turn_state()
            return

        await self._emit(AgentRequestStarted())
        await self._run_streaming_agent(transcript, token)

    # ── Streaming agent path ───────────────────────────────────

    async def _run_streaming_agent(self, transcript: str, token: CancelToken | None) -> None:
        """Streaming agent path with incremental TTS on sentence boundaries.

        Uses :func:`consume_agent_stream` to translate agent events into
        TTS payloads, and runs TTS synthesis concurrently.
        """
        turn = self._turn
        assert turn is not None
        turn_gen = self._turn_generation
        tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()
        tts_playback_started = False
        tts_chunks: list[tuple[str, int, bool]] = []
        tts_should_stop = False

        # ── TTS consumer task ──

        async def _process_tts() -> None:
            nonlocal tts_should_stop
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
                        is_active=(
                            None
                            if self._is_gated
                            else lambda: self._turn_manager.state == TurnManagerState.BOT_SPEAKING
                        ),
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
                tts_should_stop = await self._drain_session_actions()
                if tts_should_stop:
                    await self._wait_outbound_drain()
                    await self._turn_manager.bot_stopped_speaking()
                else:
                    await self._turn_manager.bot_stopped_speaking()
                    # Wait for queued audio to drain so _drain_outbound_audio
                    # can still call turn.record_audio_sent() and emit playback
                    # marks for the tail of this turn's audio.
                    await self._wait_outbound_drain()
                # Only clear if a new turn hasn't started during the drain.
                if self._turn is turn and self._turn_generation == turn_gen:
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
        system_prefix = self._caller_id_system_message()

        async def _run_agent_consumer() -> None:
            nonlocal agent_result
            agent_result = await consume_agent_stream(
                stream_factory=lambda: self._agent_stage.execute_streaming(
                    transcript,
                    self._run_ctx,
                    turn,
                    cancel_token=token,
                    system_prefix=system_prefix,
                ),
                cancel_token=token,
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
            # AgentTimeoutError is already logged and emitted by with_agent_timeout.
            if not isinstance(exc, AgentTimeoutError):
                logger.exception("Streaming agent error")
                await self._emit(Error(exception=exc, stage=ErrorStage.AGENT))
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
            original_text = accumulated_text
            stripped = strip_markdown(accumulated_text, normalize_code_spans=True)
            self._record_markdown_strip(
                phase="streaming_final",
                original_text=original_text,
                stripped_text=stripped,
                turn_id=turn.id,
            )
            if stripped != original_text:
                accumulated_text = stripped
                self.agent.replace_last_assistant_text(stripped)

        if (accumulated_text or structured_output is not None) and stream_succeeded:
            await self._emit(
                AgentFinal(text=accumulated_text, structured_output=structured_output)
            )

        try:
            await tts_task
        except asyncio.CancelledError:
            pass

        interruption_notification = estimate_and_notify_interruption(
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
        if interruption_notification is not None:
            self._record_interruption_notification(
                source="streaming_turn",
                mode=interruption_notification.mode,
                text_spoken=interruption_notification.text_spoken,
                notified=interruption_notification.notified,
                turn_id=turn.id,
            )

        if tts_should_stop:
            await self.stop()
            return

        # If a newer turn started (e.g. barge-in), avoid clobbering its state.
        if self._turn is turn and self._turn_generation == turn_gen:
            if self._turn_manager.state != TurnManagerState.IDLE:
                self._reset_turn_state()

    def _prepare_tts_payload(self, text: str, *, is_streaming: bool, is_final: bool) -> TTSInput:
        original_payload = TTSInput(text=text, format="plain")
        payload = original_payload
        payload = apply_output_processors(
            payload,
            self._output_processors,
            is_final=is_final,
            is_streaming=is_streaming,
        )
        if payload.format == "ssml" and not getattr(self.tts, "supports_ssml", False):
            payload = TTSInput(text=strip_ssml_tags(payload.text), format="plain")
        self._record_tts_payload_prepared(
            original_text=original_payload.text,
            original_format=original_payload.format,
            prepared_payload=payload,
            is_streaming=is_streaming,
            is_final=is_final,
        )
        return payload

    # ── TTS synthesis helper ───────────────────────────────────

    async def _synthesize_tts(self, payload: TTSInput | str, token: CancelToken | None) -> bool:
        """Synthesize TTS for a complete payload and emit audio events.

        Returns ``True`` if a drained session action signalled that the
        session should stop.
        """
        should_stop = False
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
                should_stop = await self._drain_session_actions()
                if should_stop:
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
        return should_stop

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
                self._stamp_outbound_chunk(chunk, turn)
                delivered = await self._transport_stage.execute(
                    chunk, self._run_ctx, turn or self._no_turn
                )
                if delivered and not self._transport_reports_audio_delivery:
                    # Stamp turn_id from self._turn at dequeue time (captured
                    # before send_audio awaits) so a slow send under
                    # backpressure doesn't inherit a newer turn's id.
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
        turn = self._turn
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
        if self._enable_aec:
            self.echo_canceller.feed_reference(chunk)

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

    async def _on_transport_audio_delivered(self, event: TransportAudioDelivered) -> None:
        """Finalize accounting for buffered transports at their no-clear point."""
        turn = event.turn_ref if isinstance(event.turn_ref, TurnContext) else None
        if turn is None and self._turn is not None:
            if event.turn_id is None or self._turn.id == event.turn_id:
                turn = self._turn

        turn_id = event.turn_id or (turn.id if turn is not None else None)
        await self._handle_audio_delivery(event.chunk, turn)
        await self._emit(AudioOut(chunk=event.chunk, turn_id=turn_id))

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

        # Serialize cancel-and-launch so concurrent send_text() calls
        # cannot both observe the same prev task and launch parallel turns.
        async with self._text_turn_lock:
            prev = self._active_text_turn
            if prev is not None and not prev.done():
                delivered = self._text_turn_accumulated
                if self._text_turn_cancel_token:
                    self._text_turn_cancel_token.cancel()
                prev.cancel()
                try:
                    await prev
                except (asyncio.CancelledError, Exception):
                    pass
                notified = _notify_bridge_interruption(
                    self.agent, delivered, self._interruption_mode
                )
                self._record_interruption_notification(
                    source="text_session",
                    mode=self._interruption_mode,
                    text_spoken=delivered,
                    notified=notified,
                )

            token = CancelToken()
            self._text_turn_cancel_token = token
            task = asyncio.ensure_future(self._execute_text_turn(text, token))
            self._active_text_turn = task
        return await task

    async def _execute_text_turn(self, text: str, cancel_token: CancelToken | None = None) -> str:
        turn_id = f"turn-{uuid4().hex[:12]}"
        await self._emit(TurnStarted(session_id=self.session_id, turn_id=turn_id))
        try:
            t0 = time.monotonic()
            await self._emit(AgentRequestStarted(session_id=self.session_id, turn_id=turn_id))
            structured_output = None
            self._text_turn_accumulated = ""
            # Build a turn context for this text turn so AgentStage can
            # stamp records with the right turn_id.
            text_turn = TurnContext(turn_id=turn_id, cancel_token=cancel_token or CancelToken())
            accumulated = ""
            system_prefix = self._caller_id_system_message()
            async for event in self._agent_stage.execute_streaming(
                text,
                self._run_ctx,
                text_turn,
                cancel_token=cancel_token,
                system_prefix=system_prefix,
            ):
                kind = getattr(event, "kind", None)
                if kind is None:
                    continue
                if kind == "done":
                    if event.text:
                        accumulated = event.text
                    if getattr(event, "structured_output", None) is not None:
                        structured_output = event.structured_output
                    break
                if kind == "text_delta" and event.text:
                    accumulated += event.text
                    self._text_turn_accumulated = accumulated
                    await self._emit(
                        AgentDelta(
                            text=event.text,
                            session_id=self.session_id,
                            turn_id=turn_id,
                        )
                    )
                elif kind == "tool_started":
                    await self._emit(
                        ToolCallStarted(
                            tool_name=event.tool_name,
                            call_id=event.call_id,
                            session_id=self.session_id,
                            turn_id=turn_id,
                        )
                    )
                elif kind == "tool_delta":
                    await self._emit(
                        ToolCallDelta(
                            call_id=event.call_id,
                            delta=event.text,
                            session_id=self.session_id,
                            turn_id=turn_id,
                        )
                    )
                elif kind == "tool_result":
                    await self._emit(
                        ToolCallResult(
                            call_id=event.call_id,
                            result=event.result,
                            session_id=self.session_id,
                            turn_id=turn_id,
                        )
                    )
            response = accumulated
            elapsed_ms = (time.monotonic() - t0) * 1000
            await self._emit(
                AgentFinal(
                    text=response,
                    structured_output=structured_output,
                    session_id=self.session_id,
                    turn_id=turn_id,
                )
            )
            if self._journal:
                self._journal_sink.append_record(
                    kind=JournalRecordKind.METRIC,
                    name="text_turn_latency_ms",
                    turn_id=turn_id,
                    data={"value": elapsed_ms},
                )
        except Exception as exc:
            logger.exception("Agent error in text_session send_text")
            await self._emit(
                Error(
                    exception=exc,
                    stage=ErrorStage.AGENT,
                    session_id=self.session_id,
                    turn_id=turn_id,
                )
            )
            raise
        finally:
            await self._emit(TurnEnded(session_id=self.session_id, turn_id=turn_id))
        return response

    async def _cancel_stt(self) -> None:
        await self._runtime_scope.cancel_and_drain("stt_pause_commit")
        await self._runtime_scope.cancel_and_drain("stt_segment_commit")
        self._stt_pause_commit_task = None
        self._stt_segment_commit_task = None
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
        self._resolve_pending_stt_segment_futures("")
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
