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
from easycat._bounded_queue import BoundedAudioQueue
from easycat._health_check import PeriodicHealthChecker
from easycat._log_context import bind_session, bind_turn, reset_session
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
    SessionActionCompleted,
    SessionActionFailed,
    SessionActionRequested,
    SessionActionStarted,
    STTFinal,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
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
from easycat.runtime.journal import (
    ExecutionJournal,
    InMemoryRingBuffer,
    JournalView,
    ReadonlySqliteJournal,
)
from easycat.runtime.scope import RuntimeScope
from easycat.session._builder import (
    _OUTBOUND_QUEUE_MAX_SIZE,
    _OUTBOUND_QUEUE_NAME,
    _OUTBOUND_QUEUE_POLICY,
    SessionComponents,
    build_session,
)
from easycat.session._caller_id import CallerIdState
from easycat.session._telephony_facade import TelephonyFacade
from easycat.session._types import (
    _TM_TO_TURN_STATE,
    Agent,
    CallerIdExposure,
    CallIdentity,
    SessionConfig,
    TurnState,
)
from easycat.session.actions import (
    CoreSessionActionExecutor,
    SessionAction,
    SessionActionExecutor,
)
from easycat.stages.base import (
    InterruptSignal as _InterruptSignal,
)
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

        # ── Providers (fall back to no-op stubs) ─────────────────
        self.stt = cfg.stt or NoopSTT()
        self.tts = cfg.tts or NoopTTS()
        self.vad = cfg.vad or NoopVAD()
        self.noise_reducer = cfg.noise_reducer or PassthroughNoiseReducer()
        self.echo_canceller = cfg.echo_canceller or PassthroughAEC()
        self.transport = cfg.transport or NoopTransport()

        # ── Agent ────────────────────────────────────────────────
        # Back-store for the ``agent`` property so late assignments
        # (``session.agent = X``) keep the AgentStage wrapper in sync.
        # ``auto_adapt_agent`` returns plain ``async run(text)`` agents
        # unchanged; we wrap here as a safety net so the bridge interface
        # Session relies on (``reset``, ``replace_last_assistant_text``) is
        # always present.
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
        self._inject_agent_runtime_config(self._agent)

        # ── Event bus + provider event-bus attach ────────────────
        self.event_bus = cfg.event_bus or EventBus()
        self._maybe_attach_event_bus(self.stt)
        self._maybe_attach_event_bus(self.tts)
        self._maybe_attach_event_bus(self.transport)

        # ── Noop validation (audio sessions must have real providers) ─
        self._validate_providers(cfg)

        # ── Pipeline flags ───────────────────────────────────────
        # Auto-enable when a real provider is supplied so that direct
        # SessionConfig users don't silently lose processing.
        self._enable_noise_reduction = cfg.enable_noise_reduction or is_active_provider(
            self.noise_reducer
        )
        self._enable_aec = (
            cfg.enable_echo_cancellation or is_active_provider(self.echo_canceller)
        ) and is_active_provider(self.echo_canceller)
        self._enable_vad = cfg.enable_vad
        self._auto_turn_from_stt_final = cfg.auto_turn_from_stt_final
        self._audio_gate = cfg.audio_gate

        # ── Turn manager (single source of truth for turn state) ──
        self._turn_manager = cfg.turn_manager or TurnManager(
            self.event_bus,
            config=cfg.turn_manager_config,
        )
        self._turn_manager.bind_journal_hook(self._on_turn_state_changed)

        # ── Reliability / observability config ───────────────────
        self._timeout_config = cfg.timeout_config or self._default_timeout_config()
        self._journal = cfg.journal
        self._journal_view: JournalView | None = (
            JournalView(self._journal) if self._journal is not None else None
        )
        self._artifact_store = cfg.artifact_store

        # ── Outbound audio queue config (queue built by the builder) ─
        self._outbound_queue_external = cfg.outbound_queue is not None
        self._outbound_queue_max_size = _OUTBOUND_QUEUE_MAX_SIZE
        self._outbound_queue_policy = _OUTBOUND_QUEUE_POLICY
        self._outbound_queue_name = _OUTBOUND_QUEUE_NAME

        # ── Session-owned services ───────────────────────────────
        self._health_checkers: list[PeriodicHealthChecker] = []
        self._runtime_scope = RuntimeScope()
        self._session_actions = cfg.session_actions
        self._action_executors: list[SessionActionExecutor] = [
            *cfg.action_executors,
            CoreSessionActionExecutor(),
        ]
        # Caller / callee identity + exposure policy.  Owned by a small
        # collaborator so Session just delegates its call_identity /
        # caller_id_exposure properties.
        self._caller_id = CallerIdState(
            identity=cfg.call_identity,
            exposure=cfg.caller_id_exposure,
        )
        # Telephony helpers behind a single ``session.telephony`` facade.
        self.telephony = TelephonyFacade(list(cfg.telephony_helpers))

        # ── Lifecycle / turn-pointer state ───────────────────────
        self._is_running = False
        self._closed = False
        self._stopping = False
        self._flushed = False
        self._observability_active = False
        self._closed_event: asyncio.Event | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._session_log_token = None
        # Per-turn state — created fresh at each turn start.
        # _turn_generation is a monotonic counter incremented at turn start
        # so stale callbacks from previous turns are detectable.
        self._turn: TurnContext | None = None
        self._turn_generation: int = 0

        self.session_id = cfg.session_id or f"session-{uuid4().hex[:12]}"
        self._runtime_mode = cfg.runtime_mode
        self._turn_manager.bind_session(self.session_id)

        # ── Assemble collaborators ───────────────────────────────
        # The builder constructs the 7 stages, the shared RunContext, the
        # journal sink, the outbound queue, and every collaborator
        # (AudioRouter, STTCommitter, TTSScheduler, CancelOrchestrator,
        # TurnRunner, GreetingController, OptOutPolicy), wires their
        # event-bus subscriptions and TurnManager bindings, and returns the
        # assembled bundle for us to unpack onto private fields.
        self._unpack(build_session(self, cfg))

    def _unpack(self, components: SessionComponents) -> None:
        """Assign the assembled collaborator bundle onto private fields.

        Field names are preserved (``_audio_router``, ``_stt_committer``,
        …) so the orchestration in this class — and the tests that poke
        these internals — keep working.
        """
        self._run_ctx = components.run_ctx
        self._no_turn = components.no_turn
        self._journal_sink = components.journal_sink
        self._outbound_queue = components.outbound_queue
        self._stt_stage = components.stt_stage
        self._tts_stage = components.tts_stage
        self._vad_stage = components.vad_stage
        self._audio_stage = components.audio_stage
        self._transport_stage = components.transport_stage
        self._agent_stage = components.agent_stage
        self._turn_stage = components.turn_stage
        self._audio_router = components.audio_router
        self._stt_committer = components.stt_committer
        self._tts_scheduler = components.tts_scheduler
        self._cancel = components.cancel_orchestrator
        self._turn_runner = components.turn_runner
        self._greeting = components.greeting
        self._opt_out = components.opt_out

    def _validate_providers(self, cfg: SessionConfig) -> None:
        """Reject noop providers for audio sessions; warn on missing NR backend.

        Text sessions intentionally use noop audio providers, so the check
        is skipped there.
        """
        if cfg.runtime_mode == "text_session":
            return
        noops = []
        if is_passthrough_provider(self.stt):
            noops.append("stt")
        if is_passthrough_provider(self.tts):
            noops.append("tts")
        if cfg.enable_vad and is_passthrough_provider(self.vad):
            noops.append("vad")
        # A passthrough noise reducer is a legitimate graceful-degradation
        # outcome (no optional backend installed), mirroring PassthroughAEC.
        # ``create_noise_reducer`` already logs an actionable warning — and
        # ``NoiseReducerConfig(fallback_policy="error")`` is the opt-in for
        # fail-loud — so enabling noise reduction without a backend must
        # warn-and-continue rather than crash at Session construction.
        if cfg.enable_noise_reduction and is_passthrough_provider(self.noise_reducer):
            logger.warning(
                "Noise reduction is enabled but the configured noise_reducer is a "
                "passthrough (no real backend); audio will pass through unchanged. "
                "Install easycat[rnnoise] or configure Krisp, or set "
                "NoiseReducerConfig(fallback_policy='error') to fail loudly instead."
            )
        if is_passthrough_provider(self.transport):
            noops.append("transport")
        if cfg.agent is None and is_passthrough_provider(self.agent):
            noops.append("agent")
        if noops:
            raise ValueError(
                "SessionConfig must provide non-noop implementations for: " + ", ".join(noops)
            )

    @staticmethod
    def _default_timeout_config():
        from easycat.timeouts import TimeoutConfig

        return TimeoutConfig()

    def _active_turn(self) -> TurnContext | None:
        """Return the turn that is currently *active* for correlation purposes.

        This is deliberately stricter than the live ``self._turn`` pointer.  In
        the gated-TTS path ``self._turn`` is kept alive after the turn manager
        resets to IDLE for playback-mark bookkeeping, but events emitted (and
        TTS scheduled) during that window must not carry the old turn's ID.
        Treat the turn as active only while the turn manager has not returned
        to IDLE.
        """
        if self._turn and self._turn_manager.state != TurnManagerState.IDLE:
            return self._turn
        return None

    def _with_correlation(self, event: Any) -> Any:
        """Attach session/turn identifiers to events when supported."""
        if not hasattr(event, "session_id") and not hasattr(event, "turn_id"):
            return event
        kwargs: dict[str, Any] = {}
        if hasattr(event, "session_id") and getattr(event, "session_id", None) is None:
            kwargs["session_id"] = self.session_id
        if hasattr(event, "turn_id") and getattr(event, "turn_id", None) is None:
            active_turn = self._active_turn()
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
        bind_turn(None)
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

    # ── Properties ─────────────────────────────────────────────

    def subscribe_event(self, event_type: type, handler: EventHandler) -> None:
        """Subscribe to a session event via the underlying EventBus."""
        self.event_bus.subscribe(event_type, handler)

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
        """Return the first attached telephony helper matching *helper_type*.

        Thin delegate to :attr:`telephony`; equivalent to
        ``session.telephony.get(helper_type)``.  The named accessors
        (``session.telephony.outbound_call_manager`` etc.) are usually
        more convenient.
        """
        return self.telephony.get(helper_type)

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
    def dnc_list(self) -> Any | None:
        """Do-Not-Call list consulted by opt-out auto-detection.

        Apps that want opt-out flows to persist across sessions
        assign the same ``DNCList`` instance to every session
        (or wire a shared store behind a DNC-list-compatible object).
        Delegates to the :class:`OptOutPolicy` collaborator that owns it.
        """
        return self._opt_out.dnc_list

    @dnc_list.setter
    def dnc_list(self, value: Any | None) -> None:
        self._opt_out.dnc_list = value

    @property
    def call_identity(self) -> CallIdentity | None:
        """Caller / callee identity for this session.

        Populated by telephony transports on connect (Twilio reads
        ``<Stream>`` customParameters) or by
        :meth:`OutboundCallManager.place_call` for outbound calls.
        Tool code (including agent function tools) reads this directly
        unless :attr:`caller_id_exposure` is ``"off"``.  Internal
        telephony policy hooks retain the private value so opt-out
        detection can still update DNC state.  Delegates to the
        :class:`CallerIdState` collaborator.
        """
        return self._caller_id.identity

    @call_identity.setter
    def call_identity(self, value: CallIdentity | None) -> None:
        self._caller_id.identity = value

    @property
    def caller_id_exposure(self) -> CallerIdExposure:
        """Exposure policy for :attr:`call_identity`."""
        return self._caller_id.exposure

    @caller_id_exposure.setter
    def caller_id_exposure(self, value: CallerIdExposure) -> None:
        self._caller_id.exposure = value

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
        # Tag log records emitted in this context with the session id.  A
        # ContextVar default of None is fine; threading.Thread workers won't
        # inherit it, but EasyCat avoids that boundary.
        self._session_log_token = bind_session(self.session_id)
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

            for helper in self.telephony.helpers:
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
            self._reset_session_log_context()
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
                self._greeting.clear_task()
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

                await self._greeting.cancel()
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
                logger.debug("Error closing agent during stop", exc_info=True)
            await self._close_audio_providers()
            self._turn = None
            self._destroy()
            self._mark_closed()
        finally:
            self._mark_observability_inactive()
            self._reset_session_log_context()
            self._stopping = False

    def _reset_session_log_context(self) -> None:
        """Restore this task's pre-session logging correlation binding."""
        token = self._session_log_token
        if token is None:
            return
        self._session_log_token = None
        reset_session(token)

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
        task is the entire ``TurnRunner.on_turn_ended`` coroutine which
        includes the agent consumer.  Cancelling it would abort the
        agent stream.
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

    def _stop_helpers(self) -> None:
        """Stop attached helper components that own event subscriptions/state."""
        for helper in self.telephony.helpers:
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
                logger.warning(
                    "Failed to attach session EventBus to %r; provider-scoped events may be muted",
                    provider,
                    exc_info=True,
                )
        has_unset_bus = hasattr(provider, "_event_bus") and getattr(provider, "_event_bus") is None
        if not attached and has_unset_bus:
            try:
                setattr(provider, "_event_bus", self.event_bus)
            except Exception:
                logger.warning(
                    "Failed to attach session EventBus to %r; provider-scoped events may be muted",
                    provider,
                    exc_info=True,
                )

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
