"""Session: the core runtime for a single voice conversation.

Manages the voice pipeline lifecycle, wires provider stages together,
and handles turn state and cancellation.  Drives the agent bridge
through a single streaming path and feeds incremental TTS synthesis on
sentence boundaries for low-latency playback.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import math
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from re import sub
from typing import Any, Literal, Self, TypeVar, cast
from uuid import uuid4

from easycat import _observability as observability
from easycat._bounded_queue import BoundedAudioQueue
from easycat._concurrency import RuntimeSupervisor
from easycat._health_check import PeriodicHealthChecker
from easycat._log_context import bind_session, bind_turn, reset_session
from easycat._provider_helpers import ProviderErrorEmitter
from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.echo_cancellation import PassthroughAEC
from easycat.events import (
    AgentDelta,
    AgentFinal,
    BotStartedSpeaking,
    BotStoppedSpeaking,
    Error,
    Event,
    EventBus,
    EventHandler,
    EventSubscription,
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
from easycat.providers import (
    EchoCanceller,
    NoiseReducer,
    STTProvider,
    Transport,
    TTSProvider,
    VADProvider,
)
from easycat.runtime.capabilities import (
    RuntimeScopeBindable,
    aclose_if_supported,
    clear_audio_if_supported,
    close_if_supported,
    health_checkable,
    is_active_provider,
    is_passthrough_provider,
    rollback_warmup_if_supported,
)
from easycat.runtime.journal import JournalView
from easycat.runtime.record_contracts import BUILTIN_JOURNAL_RECORD_CONTRACTS
from easycat.runtime.records import JournalRecordKind
from easycat.runtime.scope import RuntimeCohortSignal, RuntimeScope, RuntimeTaskAction
from easycat.session._builder import (
    _OUTBOUND_QUEUE_MAX_SIZE,
    _OUTBOUND_QUEUE_NAME,
    _OUTBOUND_QUEUE_POLICY,
    SessionComponents,
    build_session,
)
from easycat.session._caller_id import CallerIdState
from easycat.session._debug_backends import SessionDebugBackends
from easycat.session._telephony_facade import TelephonyFacade
from easycat.session._turn_lifecycle import TurnLifecycle
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
from easycat.teardown_budgets import (
    SESSION_BARGE_IN_CUTOFF_TIMEOUT_S as _BARGE_IN_CUTOFF_TIMEOUT_S,
)
from easycat.teardown_budgets import (
    SESSION_FORCE_START_LOCK_TIMEOUT_S,
    SESSION_SUPERSEDED_STOP_TIMEOUT_S,
)
from easycat.turn_manager import TurnManager, TurnManagerState

logger = logging.getLogger(__name__)
_BARGE_IN_CLEANUP_TASK = "barge_in_cleanup"
_SUPERVISOR_STREAM_COHORT = "supervisor-streams"
_EventT = TypeVar("_EventT", bound=Event)


def _recording_filename_session_id(session_id: str) -> str:
    """Return a filesystem-local session id component for record_to exports."""
    safe = sub(r"[:\\/]+", "-", session_id)
    safe = safe.replace("..", "__").strip(". ")
    return safe or "session"


def _validate_application_record_name(name: str) -> None:
    if not isinstance(name, str):
        raise ValueError("Application journal record name must be a string")  # noqa: TRY004 domain-specific validation error
    if name in BUILTIN_JOURNAL_RECORD_CONTRACTS:
        raise ValueError(f"Journal record name {name!r} is reserved by EasyCat")
    if not name.startswith("app.") or not name.removeprefix("app.").strip():
        raise ValueError("Application journal record names must use the 'app.<name>' namespace")


def _application_record_tags(tags: object) -> frozenset[str]:
    if isinstance(tags, (str, bytes)) or tags is None:
        raise ValueError("Application journal record tags must be an iterable of strings")
    try:
        frozen = frozenset(cast(Iterable[object], tags))
    except TypeError as exc:
        raise ValueError("Application journal record tags must be an iterable of strings") from exc
    for tag in frozen:
        if not isinstance(tag, str) or not tag:
            raise ValueError("Application journal record tags must be non-empty strings")
        if "," in tag:
            raise ValueError("Application journal record tags must not contain commas")
    return cast(frozenset[str], frozen)


def _application_record_data(data: object) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Application journal record data must be a dictionary")  # noqa: TRY004 domain-specific validation error
    active: set[int] = set()

    def _snapshot(value: Any, path: str) -> Any:
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"Application journal record {path} must be finite")
            return value
        if isinstance(value, (dict, list)):
            identity = id(value)
            if identity in active:
                raise ValueError(f"Application journal record {path} contains a cycle")
            active.add(identity)
            try:
                if isinstance(value, dict):
                    snapshot: dict[str, Any] = {}
                    for key, item in value.items():
                        if not isinstance(key, str):
                            raise ValueError(  # noqa: TRY004 domain-specific validation error
                                f"Application journal record {path} keys must be strings"
                            )
                        snapshot[key] = _snapshot(item, f"{path}.{key}")
                    return snapshot
                return [_snapshot(item, f"{path}[{index}]") for index, item in enumerate(value)]
            finally:
                active.remove(identity)
        raise ValueError(f"Application journal record {path} must contain only JSON-native values")

    return _snapshot(data, "data")


_APPLICATION_TURN_ID_OMITTED = object()


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

    # Set dynamically by ``easycat.config._factory``: a lightweight config
    # snapshot for debug-bundle export, and the emergency-export unregister
    # hook. Declared (not assigned) so ``getattr(..., default)`` probes keep
    # their runtime behavior.
    _easycat_config: Any
    _data_dir: str | Path | None
    _emergency_export_unregister: Callable[[], None]

    def __init__(self, config: SessionConfig) -> None:
        cfg = config
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
        # always present — including for the default NoopAgent.
        self._agent: ExternalAgentBridge = _ensure_bridge(
            auto_adapt_agent(cfg.agent) if cfg.agent else NoopAgent()
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
        for provider in (
            self.stt,
            self.tts,
            self.vad,
            self.noise_reducer,
            self.echo_canceller,
            self.transport,
        ):
            self._maybe_attach_event_bus(provider)

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
        self._audio_capture_policy = cfg.capture_audio
        self._audio_capture_override: bool | None = None
        self._audio_capture_policy_failed = False
        self._last_audio_capture_enabled: bool | None = None
        self._audio_capture_epoch = 0

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
        self._record_to: Path | None = None
        self._record_to_exported = False
        if cfg.record_to is not None:
            if self._journal is None:
                logger.warning(
                    "record_to=%r requested but debug journaling is disabled; "
                    "set debug='light' or 'full' to enable recording.",
                    str(cfg.record_to),
                )
            else:
                self._record_to = Path(cfg.record_to)

        # ── Outbound audio queue config (queue built by the builder) ─
        self._outbound_queue_external = cfg.outbound_queue is not None
        self._outbound_queue_max_size = _OUTBOUND_QUEUE_MAX_SIZE
        self._outbound_queue_policy = _OUTBOUND_QUEUE_POLICY
        self._outbound_queue_name = _OUTBOUND_QUEUE_NAME

        # ── Session-owned services ───────────────────────────────
        self._health_checkers: list[PeriodicHealthChecker] = []
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
        # Do-Not-Call list consulted by outbound telephony pre-dial checks.
        # Stored here so the public ``Session.dnc_list`` property is a plain
        # session-owned attribute.
        self._dnc_list = cfg.dnc_list
        # Telephony helpers behind a single ``session.telephony`` facade.
        self.telephony = TelephonyFacade(list(cfg.telephony_helpers))

        # ── Lifecycle / turn-pointer state ───────────────────────
        self._is_running = False
        self._start_lock = asyncio.Lock()
        self._start_task: asyncio.Task[Any] | None = None
        self._closed = False
        self._stopping = False
        self._stop_task: asyncio.Task[Any] | None = None
        self._stop_force = False
        self._lifecycle_cleanup_error: Exception | None = None
        self._observability_active = False
        self._closed_event: asyncio.Event | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._session_log_token = None
        self._event_subscription_owner = object()
        self._event_subscriptions: list[EventSubscription] = []
        # Canonical turn-identity owner. Private compatibility properties below
        # keep existing focused harnesses on the same publication seam.
        self._turn_lifecycle = TurnLifecycle()

        self.session_id = cfg.session_id or f"session-{uuid4().hex[:12]}"
        self._runtime_mode = cfg.runtime_mode
        self._runtime_supervisor = RuntimeSupervisor(capacity=1)
        self._runtime_scope = RuntimeScope.create_root(
            name="session",
            root_id=f"session:{self.session_id}",
            supervisor=self._runtime_supervisor,
            survivor_capacity=1,
        )
        for role, provider in (("stt", self.stt), ("tts", self.tts)):
            if isinstance(provider, ProviderErrorEmitter):
                provider._attach_provider_event_scope(
                    self._runtime_scope,
                    name=f"{role}-provider-events",
                )
        for scope_name, provider in (
            ("transport-runtime", self.transport),
            ("stt-provider-runtime", self.stt),
            ("tts-provider-runtime", self.tts),
        ):
            if isinstance(provider, RuntimeScopeBindable):
                provider.set_runtime_scope(
                    self._runtime_scope,
                    name=scope_name,
                )
        self._turn_manager.bind_session(self.session_id)
        for event_producer in (self.transport, *cfg.telephony_helpers):
            self._maybe_bind_session_id(event_producer)

        # ── Assemble collaborators ───────────────────────────────
        # The builder constructs the 7 stages, the shared RunContext, the
        # journal sink, the outbound queue, and every collaborator
        # (AudioRouter, STTCommitter, TTSScheduler, CancelOrchestrator,
        # TurnRunner, GreetingController), wires their event-bus
        # subscriptions and TurnManager bindings, and returns the
        # assembled bundle for us to unpack onto private fields.
        try:
            self._unpack(build_session(self, cfg))
        except BaseException:
            # The caller-supplied EventBus can outlive a failed Session
            # constructor. Do not let partially assembled collaborators remain
            # pinned to that bus when later construction raises.
            self._unsubscribe_session_event_handlers()
            raise
        self._debug_backends = SessionDebugBackends(
            journal=self._journal,
            journal_view=self._journal_view,
            artifact_store=self._artifact_store,
            journal_sink=self._journal_sink,
        )
        self._commit_event_bus_ownership()

    @classmethod
    def from_providers(
        cls,
        *,
        stt: STTProvider,
        tts: TTSProvider,
        vad: VADProvider,
        transport: Transport,
        agent: Agent,
        noise_reducer: NoiseReducer | None = None,
        echo_canceller: EchoCanceller | None = None,
        **session_options: Any,
    ) -> Session:
        """Build a :class:`Session` from already-constructed providers.

        This is the advanced raw-provider entry point. New applications
        should usually start with :class:`~easycat.EasyConfig` plus
        :func:`~easycat.create_session`, but callers that truly own their
        provider instances can use this instead of spelling out
        ``Session(SessionConfig(...))``. Extra keyword arguments are
        forwarded to :class:`SessionConfig` for lifecycle and policy knobs.
        """
        return cls(
            SessionConfig(
                stt=stt,
                tts=tts,
                vad=vad,
                transport=transport,
                agent=agent,
                noise_reducer=noise_reducer,
                echo_canceller=echo_canceller,
                **session_options,
            )
        )

    def _unpack(self, components: SessionComponents) -> None:
        """Assign the assembled collaborator bundle onto private fields.

        Field names are preserved (``_audio_router``, ``_stt_committer``,
        …) so the orchestration in this class — and the tests that poke
        these internals — keep working.
        """
        self._run_ctx = components.run_ctx
        self._no_turn = components.no_turn
        self._journal_sink = components.journal_sink
        self._warmup = components.warmup
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
                "Install RNNoise with: uv add 'easycat[rnnoise]'. From the "
                "EasyCat repo, use: uv sync --extra rnnoise --group dev. Or configure Krisp. Set "
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

    @property
    def _turn(self) -> TurnContext | None:
        """Compatibility view over the canonical Session identity owner."""
        return self._turn_lifecycle.current

    @_turn.setter
    def _turn(self, turn: TurnContext | None) -> None:
        if turn is None:
            self._turn_lifecycle.clear_identity()
        else:
            self._turn_lifecycle.publish_identity(turn)

    @property
    def _turn_generation(self) -> int:
        """Legacy generation view, dual-written from the identity epoch."""
        return self._turn_lifecycle.generation

    @_turn_generation.setter
    def _turn_generation(self, generation: int) -> None:
        self._turn_lifecycle.assert_legacy_generation(generation)

    @property
    def current_turn(self) -> TurnContext | None:
        """Return the live turn context, including post-playback bookkeeping.

        Most applications should use :attr:`turn_state` for lifecycle decisions.
        This lower-level accessor exists for diagnostics, replay, and focused
        test harnesses that need to inspect per-turn accounting without reaching
        into Session's collaborator graph.
        """
        return self._turn

    def begin_turn(
        self,
        turn_id: str,
        cancel_token: CancelToken | None = None,
    ) -> TurnContext:
        """Create and install a turn context without advancing the turn manager.

        This is a low-level seam for diagnostics, replay, and focused test
        harnesses. Applications that initiate user speech should call
        :meth:`start_turn`, which performs the full lifecycle transition.
        """
        if not turn_id.strip():
            raise ValueError("turn_id must be a non-empty string")
        turn = TurnContext(turn_id=turn_id, cancel_token=cancel_token or CancelToken())
        self._turn_lifecycle.publish_identity(turn)
        return turn

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
        self._turn_lifecycle.clear_identity()
        self._audio_router.reset_speech_detection()
        self._audio_router.reset_replay_chunks()
        self._turn_manager.reset()

    @property
    def _is_gated(self) -> bool:
        """Whether the classification gate is currently buffering TTS audio."""
        return self._audio_gate is not None and self._audio_gate()

    def _is_audio_capture_enabled(self) -> bool:
        policy = self._audio_capture_policy
        if isinstance(policy, bool):
            decision = self._audio_capture_override
            if decision is None:
                decision = policy
            return self._observe_audio_capture_decision(decision)
        decision = self._evaluate_audio_capture_predicate(policy)
        # A callable is the consent ceiling: a runtime pause can disable
        # capture, but resuming cannot override a predicate that was revoked.
        if self._audio_capture_override is False:
            decision = False
        return self._observe_audio_capture_decision(decision)

    def _evaluate_audio_capture_predicate(self, policy: Callable[[], bool]) -> bool:
        try:
            decision = policy()
        except Exception:
            if not self._audio_capture_policy_failed:
                self._audio_capture_policy_failed = True
                logger.warning(
                    "capture_audio predicate failed; audio artifact capture is disabled",
                    exc_info=True,
                )
            return False
        if isinstance(decision, bool):
            return decision
        if inspect.isawaitable(decision):
            close = getattr(decision, "close", None)
            if callable(close):
                close()
        if not self._audio_capture_policy_failed:
            self._audio_capture_policy_failed = True
            logger.warning(
                "capture_audio predicate returned %s instead of bool; "
                "audio artifact capture is disabled",
                type(decision).__name__,
            )
        return False

    def _observe_audio_capture_decision(self, enabled: bool) -> bool:
        if not enabled and self._last_audio_capture_enabled is True:
            self._audio_capture_epoch += 1
        if enabled and self._last_audio_capture_enabled is False:
            self._turn_manager.discard_buffered_audio()
            audio_router = getattr(self, "_audio_router", None)
            if audio_router is not None:
                audio_router.discard_pending_capture_audio()
        self._last_audio_capture_enabled = enabled
        return enabled

    def _audio_capture_epoch_value(self) -> int:
        return self._audio_capture_epoch

    def set_audio_capture_enabled(self, enabled: bool | None) -> None:
        """Pause/resume audio capture, or clear the runtime override with ``None``."""
        if enabled is not None and not isinstance(enabled, bool):
            raise TypeError("enabled must be a bool or None")
        self._audio_capture_override = enabled
        self._is_audio_capture_enabled()

    # ── Properties ─────────────────────────────────────────────

    def subscribe_event(
        self,
        event_type: type[_EventT],
        handler: Callable[[_EventT], None | Awaitable[None]],
    ) -> EventSubscription:
        """Subscribe a typed handler and return its idempotent unsubscribe token."""
        return self.event_bus.subscribe(event_type, cast(EventHandler, handler))

    def _subscribe_owned(
        self,
        event_type: type,
        handler: EventHandler,
    ) -> EventSubscription:
        """Subscribe one Session-owned handler with correlation isolation."""

        scoped_handler = self._scope_event_handler(handler)
        subscription = self.event_bus.subscribe(event_type, scoped_handler)
        self._event_subscriptions.append(subscription)
        return subscription

    def _subscribe_owned_reserved(
        self,
        event_type: type,
        handler: EventHandler,
    ) -> EventSubscription:
        """Reserve a Session-owned lifecycle handler ahead of public observers."""

        scoped_handler = self._scope_event_handler(handler)
        subscription = self.event_bus._subscribe_reserved(event_type, scoped_handler)
        self._event_subscriptions.append(subscription)
        return subscription

    def _scope_event_handler(self, handler: EventHandler) -> EventHandler:
        """Wrap a session-level handler with this Session's correlation gate."""

        @wraps(handler)
        def _scoped_handler(event: Any) -> Any:
            if not self._accept_owned_event(event):
                return None
            return handler(event)

        # The marker lets sibling Session wrappers distinguish a genuinely
        # shared bus from app-level observers without retaining another strong
        # reference beyond the wrapper's existing closure over this Session.
        cast(Any, _scoped_handler)._easycat_event_owner = self._event_subscription_owner
        return cast(EventHandler, _scoped_handler)

    def _subscribe_scoped_handler(
        self,
        event_type: type,
        handler: EventHandler,
    ) -> EventHandler:
        """Subscribe a public convenience handler and return its bus wrapper."""
        return self._subscribe_owned(event_type, handler).handler

    def _commit_event_bus_ownership(self) -> None:
        """Mark a bus shared only after this Session constructed successfully."""
        if getattr(self.event_bus, "_easycat_was_shared_by_sessions", False):
            return
        for subscription in self._event_subscriptions:
            event_type = subscription.event_type
            if event_type is None:
                continue
            for candidate in self.event_bus.subscribers(event_type):
                owner = getattr(candidate, "_easycat_event_owner", None)
                if owner is not None and owner is not self._event_subscription_owner:
                    # Once two live Sessions have shared this bus, a later bare
                    # event is never safe to attribute by process of
                    # elimination. A stopped peer can still have a delayed
                    # transport/provider callback in flight, so this marker is
                    # deliberately monotonic.
                    cast(Any, self.event_bus)._easycat_was_shared_by_sessions = True
                    return

    def _accept_owned_event(self, event: Any) -> bool:
        """Return whether an event can safely drive this Session's internals."""
        event_session_id = getattr(event, "session_id", None)
        if event_session_id is not None:
            return event_session_id == self.session_id

        # Buffered delivery callbacks can carry the actual TurnContext even
        # when an older custom transport omits session correlation. Identity
        # with this Session's live turn is sufficient ownership proof.
        turn_ref = getattr(event, "turn_ref", None)
        if turn_ref is not None:
            return turn_ref is self._turn

        # Preserve the historical convenience of emitting bare events on a
        # bus that has only ever been owned by this Session. Once the bus has
        # been shared concurrently, explicit correlation remains mandatory
        # even after a peer unsubscribes because late callbacks can still be
        # in flight from that peer's transport or providers.
        return not getattr(self.event_bus, "_easycat_was_shared_by_sessions", False)

    def _unsubscribe_session_event_handlers(self) -> None:
        """Release every handler installed and owned by this Session."""
        subscriptions, self._event_subscriptions = self._event_subscriptions, []
        for subscription in subscriptions:
            subscription.unsubscribe()

    def unsubscribe_event(
        self,
        event_type: type[_EventT],
        handler: Callable[[_EventT], None | Awaitable[None]],
    ) -> None:
        """Unsubscribe a handler previously attached with ``subscribe_event``."""
        self.event_bus.unsubscribe(event_type, cast(EventHandler, handler))

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
            scoped_handler = self._subscribe_scoped_handler(event_type, handler)
            registrations.append((event_type, scoped_handler))

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
            (
                ToolCallStarted,
                tool_started,
                lambda cb: lambda e: cb(e.tool_name, e.call_id),
            ),
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
            scoped_handler = self._subscribe_scoped_handler(event_type, handler)
            registrations.append((event_type, scoped_handler))
        return registrations

    def unsubscribe_handlers(self, registrations: list[tuple[type, EventHandler]]) -> None:
        """Unsubscribe a batch of event handlers from prior registrations."""
        for event_type, handler in registrations:
            for index, subscription in enumerate(self._event_subscriptions):
                if subscription.event_type is event_type and subscription.handler is handler:
                    subscription.unsubscribe()
                    del self._event_subscriptions[index]
                    break
            else:
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

    def _record_debug_bundle(self) -> None:
        """Auto-export a timestamped bundle for ``record_to=`` sessions."""
        record_to = self._record_to
        if record_to is None or self._record_to_exported:
            return
        self._record_to_exported = True
        try:
            record_to.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
            safe_session_id = _recording_filename_session_id(self.session_id)
            session_digest = hashlib.blake2s(
                self.session_id.encode("utf-8", errors="surrogatepass"),
                digest_size=4,
            ).hexdigest()
            path = record_to / f"{safe_session_id}-{session_digest}-{stamp}.zip"
            self.export_debug_bundle(str(path))
            logger.info("Recorded debug bundle to %s", path)
        except Exception:
            logger.exception("Failed to record debug bundle to %s", record_to)

    @property
    def agent(self) -> ExternalAgentBridge:
        """Current agent provider, always wrapped as a bridge.

        Exposed as a property so callers that swap the agent mid-session
        (``session.agent = FailingAgent()``) automatically re-point the
        AgentStage wrapper at the new provider.  The setter accepts any
        supported agent shape (framework object, bare ``Agent``, bridge,
        or ``None``) and adapts it; the getter always returns the
        resulting :class:`ExternalAgentBridge`.
        """
        return self._agent

    @agent.setter
    def agent(self, value: Any) -> None:
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
            stage.set_provider(self._agent)  # keep the wrapper in sync and reset shadow history

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
        """Do-Not-Call list consulted by outbound telephony pre-dial checks.

        Apps that want DNC state to persist across sessions assign the
        same ``DNCList`` instance to every session (or wire a shared
        store behind a DNC-list-compatible object). Agent tools can add or
        remove numbers at runtime via
        :meth:`~easycat.session.actions.SessionActions.add_to_dnc` /
        :meth:`~easycat.session.actions.SessionActions.remove_from_dnc`.
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
        telephony policy hooks retain the private value so DNC checks
        can still see the number.  Delegates to the
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
        """Read-only journal view, including after a clean stop.

        Returns a stable view — callers may cache the result and it will
        remain valid after :meth:`stop` replaces the underlying journal
        backend with a read-only snapshot.
        """
        return self._journal_view

    def record(
        self,
        name: str,
        *,
        data: dict[str, Any],
        turn_id: str | None = _APPLICATION_TURN_ID_OMITTED,  # type: ignore[assignment]
        tags: object = frozenset(),
    ) -> None:
        """Append an application event to the live session journal.

        Application names must use the ``app.`` namespace and cannot collide
        with EasyCat's built-in record vocabulary. Writes use the same
        redaction filter as runtime records. The read surface remains available
        separately through :attr:`journal`.
        """
        if self._closed or self._stopping:
            raise RuntimeError("Session is stopping or has been stopped")
        _validate_application_record_name(name)
        snapshot = _application_record_data(data)
        frozen_tags = _application_record_tags(tags)
        inherit_turn_id = turn_id is _APPLICATION_TURN_ID_OMITTED
        if not inherit_turn_id and turn_id is not None:  # noqa: SIM102 nested branches preserve decision context
            if not isinstance(turn_id, str) or not turn_id.strip():
                raise ValueError("Application journal record turn_id must be non-empty or None")
        sequence = self._journal_sink.append_record(
            name=name,
            kind=JournalRecordKind.EVENT,
            turn_id=None if inherit_turn_id else turn_id,
            data=snapshot,
            tags=frozen_tags,
            inherit_turn_id=inherit_turn_id,
        )
        if sequence is not None and sequence < 0:
            raise RuntimeError("Application journal record could not be written")

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

    async def __aenter__(self) -> Self:
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
        current_task = asyncio.current_task()
        async with self._start_lock:
            self._start_task = current_task
            try:
                if self._stopping:
                    error = RuntimeError(
                        "Session cleanup is incomplete; call stop() again before starting"
                    )
                    if self._lifecycle_cleanup_error is not None:
                        raise error from self._lifecycle_cleanup_error
                    raise error
                await self._start_locked()
            finally:
                if self._start_task is current_task:
                    self._start_task = None

    async def _start_locked(self) -> None:
        """Start the session while the startup lock is held."""
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
            # Prime providers/models BEFORE attaching the audio device/stream.
            # These warmup hooks load ONNX models (Silero, smart-turn) and run
            # network handshakes (TTS pool, realtime STT) and can take several
            # seconds.  ``transport.connect()`` opens the live mic / telephony
            # stream and starts capturing into the bounded inbound queue, but
            # nothing drains it until ``start_ingress()`` below — so running the
            # slow warmup between the two overflowed ``_in_queue`` (≈4 s at the
            # 200-frame default), flooding the journal with "inbound queue full
            # — dropping frame" and discarding the first seconds of capture.
            # None of these hooks need the transport connected.
            await self._warmup.run(select=lambda name: name != "transport")
            self._raise_if_start_interrupted()

            # Telephony helpers subscribe to lifecycle events emitted while a
            # preflighted transport applies its deferred start frame.
            for helper in self.telephony.helpers:
                helper.start()

            await self.transport.connect()
            transport_connected = True
            self._raise_if_start_interrupted()

            # The transport's own warmup runs AFTER connect (and before
            # ingress) so a transport ``warmup()`` may prime resources that
            # ``connect()`` initializes (socket/client handles, per-connection
            # queues).  No-op for transports without a warmup hook.
            await self._warmup.run(select=lambda name: name == "transport")
            self._raise_if_start_interrupted()

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
                    checker.set_runtime_scope(
                        self._runtime_scope,
                        name=f"{name}-health-check",
                    )
                    checker.start()
                    self._health_checkers.append(checker)

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
        except BaseException as startup_error:
            # Startup cancellation must roll back the same resources as an
            # ordinary failure. Callers such as SessionManager may release
            # their last registry reference as soon as start() raises.
            self._is_running = False
            self._mark_observability_inactive()
            try:
                cleanup_error = await self._finish_interrupted_start(
                    transport_connected=transport_connected
                )
            finally:
                # The binding token belongs to this task's Context, so it
                # cannot be reset from the protected cleanup task below.
                self._reset_session_log_context()
            if cleanup_error is not None:
                self._lifecycle_cleanup_error = cleanup_error
                self._stopping = True
                # Incomplete rollback can leave a provider retaining the
                # caller-owned bus after SessionManager drops this Session.
                # Release our handlers and require explicit correlation so a
                # replacement cannot claim the abandoned provider's late
                # bare callbacks. Successful rollback remains retryable and
                # keeps its subscriptions.
                cast(Any, self.event_bus)._easycat_was_shared_by_sessions = True
                self._unsubscribe_session_event_handlers()
                raise startup_error from cleanup_error
            raise

    def _raise_if_start_interrupted(self) -> None:
        """Abort startup if force teardown took ownership mid-await.

        A provider may suppress the cancellation delivered by
        ``stop(force=True)`` and return normally from a startup await. The
        stop path is then allowed to finish independently, so startup must
        turn that lost cancellation into its ordinary rollback path rather
        than resurrecting the closed session.
        """
        if self._closed or self._stopping:
            raise asyncio.CancelledError("Session startup was interrupted by stop()")

    async def _finish_interrupted_start(
        self,
        *,
        transport_connected: bool,
    ) -> Exception | None:
        """Complete partial-start cleanup despite repeated caller cancellation."""
        cleanup_task = asyncio.create_task(
            self._rollback_interrupted_start(transport_connected=transport_connected),
            name=f"easycat-start-rollback-{self.session_id}",
        )
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                # A second cancel request must not strand resources opened by
                # start(). Preserve it by re-raising the original cancellation
                # only after the independent cleanup task has completed.
                continue
            except Exception:  # noqa: BLE001 intentional boundary or best-effort cleanup
                # The owned task has settled with an error. Read it below so
                # the startup exception remains the primary caller outcome.
                break
        try:
            cleanup_task.result()
        except Exception as exc:  # noqa: BLE001 intentional boundary or best-effort cleanup
            return exc
        except BaseException:  # noqa: BLE001 intentional boundary or best-effort cleanup
            return RuntimeError("Session startup rollback was interrupted")
        return None

    async def _rollback_interrupted_start(self, *, transport_connected: bool) -> None:
        """Release resources opened by a failed or cancelled start attempt."""
        await self._audio_router.stop_ingress()
        await self._audio_router.stop_outbound()
        await self._runtime_scope.cancel_and_drain("pipeline_heartbeat")
        self._heartbeat_task = None

        for checker in self._health_checkers:
            await checker.stop()
        self._health_checkers = []

        self._stop_helpers()
        self._reset_turn_state()

        transport_error: Exception | None = None
        if transport_connected:
            try:
                await self.transport.disconnect()
            except Exception as exc:  # noqa: BLE001 aggregate independent rollback failures
                transport_error = exc

        try:
            await rollback_warmup_if_supported(self.agent)
        except Exception as agent_error:
            if transport_error is not None:
                raise ExceptionGroup(
                    "Session startup rollback failed",
                    [transport_error, agent_error],
                ) from None
            raise

        if transport_error is not None:
            raise transport_error

    async def stop(self, *, force: bool = False) -> None:
        """Stop the session and release live backend resources.

        The single public teardown verb.  ``force=False`` (the default)
        drains in-flight work gracefully; ``force=True`` aggressively
        cancels the pipeline / TTS / outbound tasks first, for when a
        graceful stop is hung on a misbehaving provider.

        Prefer the ``async with session:`` context manager, which calls
        this for you on exit.
        """
        current_task = asyncio.current_task()
        if current_task is None:  # pragma: no cover - asyncio always supplies one
            raise RuntimeError("Session.stop() requires a running asyncio task")
        if self._start_task is current_task:
            raise RuntimeError("Session.stop() cannot run reentrantly during start()")
        if current_task.cancelling():
            # Deliver a newly-pending cancellation before sampling baselines
            # used to distinguish caller cancellation from a joined stop.
            # A previously caught cancellation is not re-delivered here.
            await asyncio.sleep(0)

        # Serialize the startup transaction against teardown, but release the
        # lock before the potentially long stop body so a force caller can
        # still supersede a graceful owner through the existing stop protocol.
        if force:
            # Close admission before cancellation so a not-yet-started caller
            # that acquires the lock next fails its startup pre-check.
            self._stopping = True
            self._turn_manager.close_admission()
            start_task = self._start_task
            if start_task is not None and not start_task.done():
                start_task.cancel()
            try:
                await asyncio.wait_for(
                    self._start_lock.acquire(),
                    timeout=SESSION_FORCE_START_LOCK_TIMEOUT_S,
                )
            except TimeoutError:
                # Force teardown must not hang forever behind startup code that
                # ignores cancellation. Cleanup below proceeds with the
                # session marked stopping; the startup task retains its own
                # rollback obligation if it eventually unwinds.
                logger.warning("Session.start() ignored cancellation; continuing force teardown")
            else:
                self._start_lock.release()
        else:
            async with self._start_lock:
                self._stopping = True
                self._turn_manager.close_admission()

        # Idempotent callers join the active teardown. A force request may take
        # ownership from a graceful stop: cancel the old caller task, wait a
        # bounded interval for cooperative cleanup, then run the force path.
        # Ownership is transferred before cancellation so another concurrent
        # force caller joins this task rather than racing a second teardown.
        superseded_task: asyncio.Task[Any] | None = None
        while True:
            active_stop = self._stop_task
            if active_stop is None or active_stop.done():
                self._stop_task = current_task
                self._stop_force = force
                self._stopping = True
                break
            if active_stop is current_task:
                return
            if force and not self._stop_force:
                superseded_task = active_stop
                self._stop_task = current_task
                self._stop_force = True
                superseded_task.cancel()
                break
            cancellation_requests = current_task.cancelling()
            try:
                await asyncio.shield(active_stop)
            except asyncio.CancelledError:
                if current_task.cancelling() > cancellation_requests:
                    raise
                # The joined stop was superseded by another force caller.
                # Re-read ownership and join its replacement.
                continue
            return

        self._is_running = False

        stop_error: Exception | None = None
        try:
            if superseded_task is not None:
                done, _ = await asyncio.wait(
                    {superseded_task},
                    timeout=SESSION_SUPERSEDED_STOP_TIMEOUT_S,
                )
                if done:
                    try:
                        superseded_task.result()
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
                        pass
                else:
                    logger.warning(
                        "Graceful Session.stop() ignored cancellation; continuing force teardown"
                    )

            if self._closed:
                # A bounded libSQL close may have left a strongly-owned retry
                # pending after this Session first became logically closed.
                # Give later stop() calls a deterministic opportunity to
                # finish physical cleanup.
                await self._stt_committer.retry_transferred_provider_close()
                self._finalize_debug_backends()
                return

            prompt_task = self._turn_runner.active_application_prompt
            prompt_is_current = prompt_task is current_task
            if (
                not force
                and prompt_task is not None
                and not prompt_is_current
                and not prompt_task.done()
            ):
                # Application prompts are confirmed turn work. Graceful stop
                # lets them finish with a bound wait so cancellation-resistant
                # prompts do not hang teardown forever (gh 1025).
                try:
                    await asyncio.wait_for(asyncio.wait({prompt_task}), timeout=5.0)
                except TimeoutError:
                    pass

            turn = self._turn
            if turn and not prompt_is_current:
                turn.cancel_token.cancel()

            # Cancel any in-flight text turn so it doesn't emit events
            # after the session is torn down.
            text_token = self._turn_runner.text_turn_cancel_token
            if text_token:
                text_token.cancel()
            text_task = self._turn_runner.active_text_turn
            if text_task is not None and text_task is not current_task and not text_task.done():
                text_task.cancel()
                try:
                    await text_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
                    pass
            if force and not prompt_is_current:
                await self._turn_runner.cancel_application_prompt()

            # Speculative plain-agent work is not part of the confirmed turn
            # task yet. Drain it explicitly before either teardown path can
            # close the wrapped agent.
            await self._turn_runner.cancel_preemptive_generation()

            stt_provider_close_transferred = False
            if force:
                # Force path: aggressively cancel every pipeline task and
                # signal scoped work before awaiting any handle so the
                # force-cancel ordering is preserved.
                tasks: list[asyncio.Task[Any]] = []
                pipeline_task = self._audio_router.pipeline_task
                if (
                    pipeline_task
                    and pipeline_task is not current_task
                    and not pipeline_task.done()
                ):
                    pipeline_task.cancel()
                    tasks.append(pipeline_task)
                # STT teardown is delegated to STTCommitter.cancel() below
                # (it cancels the consumer task, ends the stream, and drains
                # scoped commit/pause tasks) — matching 92f8ebf's move away
                # from an ad-hoc stt_task cancel here.
                current_tts_task = self._tts_scheduler.active_turn_task
                if (
                    current_tts_task
                    and current_tts_task is not current_task
                    and not current_tts_task.done()
                ):
                    current_tts_task.cancel()
                    tasks.append(current_tts_task)
                outbound_task = self._audio_router.outbound_task
                if (
                    outbound_task
                    and outbound_task is not current_task
                    and not outbound_task.done()
                ):
                    outbound_task.cancel()
                    tasks.append(outbound_task)

                # Signal scoped work before awaiting other task handles so
                # migrated shutdown work preserves the previous force-cancel
                # ordering. Drain below after every task observed cancellation.
                runtime_signals = tuple(
                    self._runtime_scope.signal_cohort(
                        cohort,
                        force=True,
                        _exclude_tasks={current_task} if current_task is not None else None,
                    )
                    for cohort in self._runtime_scope.cohorts(force=True)
                )
                for task in tasks:
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
                        pass
                stt_cleanup_complete = await self._stt_committer.cancel(turn)
                # RuntimeScope-owned work currently covers heartbeat,
                # greeting, audio-router loops, and STT segment commit/pause
                # tasks. These can outlive the pipeline/STT consumer handles
                # above, so the force path drains the scope before provider
                # teardown.
                await self._drain_force_runtime_signals(runtime_signals, deferred=False)
                if stt_cleanup_complete is False:
                    # Establish survivor ownership before transferring close.
                    # The force cohort's hard-timeout escalation issues one
                    # final task.cancel() before it parks unfinished work; if
                    # close were transferred first, that cancellation could
                    # land inside provider.close() and drop the obligation.
                    # A task that settled during the drain needs no transfer:
                    # direct provider close below safely owns cleanup instead.
                    stt_provider_close_transferred = (
                        self._stt_committer.transfer_provider_close_to_owned_work()
                    )
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

                # Runtime barge-in returns after the audible cutoff and leaves
                # provider/event cleanup in this scoped task. Finish it while
                # the providers, transport, and journal backends are still
                # live; otherwise a graceful stop can close resources out
                # from underneath the detached cleanup.
                await self._runtime_scope.drain(_BARGE_IN_CLEANUP_TASK)
                await self._greeting.cancel()
                if (await self._stt_committer.cancel(turn)) is False:
                    raise RuntimeError(
                        "STT provider cleanup remains lifecycle-owned; "
                        "retry stop() after it settles or use stop(force=True)"
                    )
                await self._tts_scheduler.cancel()

            await self._audio_router.stop_ingress()
            for checker in self._health_checkers:
                await checker.stop()
            self._health_checkers = []
            if not force:
                await self._runtime_scope.drain_cohort(
                    _SUPERVISOR_STREAM_COHORT,
                    force=False,
                )
            self._stop_helpers()
            if not self._outbound_queue_external:
                self._outbound_queue.close()
            # Cancel the outbound drain task BEFORE disconnecting the
            # transport — otherwise the task may hang on send_audio()
            # with a disconnected transport.  (The force path already
            # cancelled it above; stop_outbound is idempotent.)
            await self._audio_router.stop_outbound(force=force)
            await self._runtime_scope.cancel_and_drain("pipeline_heartbeat")
            self._heartbeat_task = None
            await self.transport.disconnect()
            await self._turn_manager.shutdown()
            runtime_close_error: Exception | None = None
            try:
                await aclose_if_supported(self.agent)
            except Exception as exc:
                runtime_close_error = exc
                logger.warning("Error closing agent during stop", exc_info=True)
            try:
                await self._close_action_executors()
            except Exception as exc:
                if runtime_close_error is not None:
                    runtime_close_error = ExceptionGroup(
                        "Session runtime resource cleanup failed",
                        [runtime_close_error, exc],
                    )
                else:
                    runtime_close_error = exc
                logger.warning(
                    "Error closing action executors during stop",
                    exc_info=True,
                )
            try:
                await self._close_audio_providers(skip_stt=stt_provider_close_transferred)
            except Exception as provider_close_error:
                if runtime_close_error is not None:
                    raise runtime_close_error from provider_close_error
                raise
            if runtime_close_error is not None:
                raise runtime_close_error
            if force:
                await self._drain_force_runtime_signals(runtime_signals, deferred=True)
            self._turn_lifecycle.clear_identity()
            self._finalize_debug_backends()
            self._mark_closed()
            # Drop this session's armed emergency-export exporter from the
            # process-wide registry now that it has stopped cleanly. Otherwise
            # the exporter closure (which strongly references this Session)
            # lingers until the shared excepthook/atexit hook runs, pinning
            # every stopped session in memory for the process lifetime.
            unregister = getattr(self, "_emergency_export_unregister", None)
            if unregister is not None:
                unregister()
        except BaseException as exc:
            stop_error = (
                exc
                if isinstance(exc, Exception)
                else RuntimeError("Session stop was interrupted by cancellation")
            )
            raise
        finally:
            owns_stop = self._stop_task is current_task
            if owns_stop:
                if not self._closed:
                    # A failed/cancelled teardown can leave a custom provider
                    # retaining this caller-owned bus after we release our
                    # handlers. A replacement Session must not infer that a
                    # later bare callback belongs to it merely because the
                    # failed owner is no longer subscribed.
                    cast(Any, self.event_bus)._easycat_was_shared_by_sessions = True
                # Whether teardown completed or failed, this owner has made
                # the Session unavailable for new work. Release its EventBus
                # handlers so an abandoned failed stop cannot retain or keep
                # mutating the Session. A superseded graceful owner skips this
                # block; the force owner performs the eventual release.
                self._unsubscribe_session_event_handlers()
                self._stop_task = None
                self._stop_force = False
                if self._closed:
                    self._lifecycle_cleanup_error = None
                    self._stopping = False
                else:
                    self._lifecycle_cleanup_error = stop_error or RuntimeError(
                        "Session stop did not complete"
                    )
                    # Keep startup admission closed until a later stop() owner
                    # retries the partially completed teardown.
                    self._stopping = True
                self._mark_observability_inactive()
                self._reset_session_log_context()
                self._record_debug_bundle()

    def _reset_session_log_context(self) -> None:
        """Restore this task's pre-session logging correlation binding."""
        token = self._session_log_token
        if token is None:
            return
        self._session_log_token = None
        reset_session(token)

    def _finalize_debug_backends(self) -> None:
        """Finalize live debug backends and preserve post-stop inspection.

        This closes backend resources such as SQLite connections,
        Litestream sidecars, libSQL sync threads, and in-memory artifact
        stores. The session retains a read-only postmortem view, so
        ``session.journal.read()`` and ``export_debug_bundle()`` continue
        to work after :meth:`stop`.

        Invoked only by :meth:`stop` (and therefore the ``async with`` exit
        path). Safe to call multiple times.
        """
        state = self._debug_backends.destroy()
        self._journal = state.journal
        self._artifact_store = state.artifact_store

    # ── Cancellation ───────────────────────────────────────────

    async def _cut_off_turn_playback(
        self,
        turn: TurnContext | None,
        *,
        barge_in: bool,
    ) -> tuple[CancelToken | None, asyncio.Task[None] | None, asyncio.Task[bool]]:
        """Stop audible output before any provider or application teardown."""
        cutoff_started = time.monotonic() if barge_in else None
        manager_token = self._turn_manager.cancel_token
        if turn:
            turn.cancel_token.cancel()
        elif manager_token is not None:
            # TurnManager publishes its token before the asynchronous
            # TurnStarted subscriber installs Session's TurnContext. A direct
            # cancellation in that window must still stop the manager-owned
            # turn instead of leaving USER_SPEAKING live indefinitely.
            manager_token.cancel()
        prompt_token = self._turn_runner.application_prompt_cancel_token
        if prompt_token is not None:
            prompt_token.cancel()
        prompt_cleanup = asyncio.create_task(
            self._turn_runner.cancel_application_prompt(),
            name="application_prompt_cancel_cleanup",
        )
        # Let the cleanup task request cancellation and the prompt task observe
        # it before returning from the cutoff path, without waiting for
        # cancellation-resistant teardown. Both handoffs are event-loop turns;
        # the cleanup task itself remains owned by detached turn cleanup.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if barge_in and turn:
            turn.record_barge_in()
        self._tts_scheduler.set_playback_suppressed(True)
        tts_task = self._tts_scheduler.request_turn_cancel()
        self._outbound_queue.flush_for_new_turn()
        self._audio_router.reset_replay_chunks()
        if barge_in:
            try:
                async with asyncio.timeout(_BARGE_IN_CUTOFF_TIMEOUT_S):
                    await clear_audio_if_supported(self.transport)
            except TimeoutError:
                logger.warning(
                    "Transport playback clear exceeded %.0f ms during barge-in",
                    _BARGE_IN_CUTOFF_TIMEOUT_S * 1000,
                )
            except Exception:
                logger.exception("Transport playback clear failed during barge-in")
        else:
            try:
                async with asyncio.timeout(_BARGE_IN_CUTOFF_TIMEOUT_S):
                    await clear_audio_if_supported(self.transport)
            except TimeoutError:
                logger.warning(
                    "Transport playback clear exceeded %.0f ms",
                    _BARGE_IN_CUTOFF_TIMEOUT_S * 1000,
                )
            except Exception:
                logger.exception("Transport playback clear failed")

        if cutoff_started is not None:
            observability.record_histogram(
                "easycat.interruption.cutoff_latency",
                time.monotonic() - cutoff_started,
                attributes={"easycat.surface": "vad"},
            )
        return manager_token, tts_task, prompt_cleanup

    async def _notify_barge_in(self, turn: TurnContext | None) -> None:
        turn_id = turn.id if turn is not None else None
        await self._emit(Interruption(turn_id=turn_id))
        await self._cancel.propagate_signal(
            _InterruptSignal(signal_id=f"barge-in-{uuid4().hex[:8]}"),
            cause="barge_in",
            turn_id=turn_id,
        )

    def _cancel_cleanup_owns_turn(
        self,
        turn: TurnContext | None,
        manager_token: CancelToken | None,
    ) -> bool:
        """Whether a deferred cancel cleanup may still change turn-wide state.

        ``cancel_turn()`` captures a turn before awaiting the playback clear.
        A VAD/PTT barge-in can install its successor during that await, before
        the stale caller reaches its global STT/preemptive cleanup.  The
        manager publishes the successor's token before its ``TurnStarted``
        subscribers run, so checking both owners captured at cancellation
        start prevents an old cleanup from tearing down the successor while
        still recognizing a manager-only turn whose Session pointer has not
        been installed yet.
        """
        active_turn = self._turn
        manager_only_turn_was_installed = (
            turn is None
            and active_turn is not None
            and manager_token is not None
            and active_turn.cancel_token is manager_token
        )
        if active_turn is not turn and not manager_only_turn_was_installed:
            return False
        active_manager_token = self._turn_manager.cancel_token
        if active_manager_token is not None and active_manager_token is not manager_token:
            return False
        if turn is None:
            return True
        return manager_token is None or manager_token is turn.cancel_token

    async def _finish_turn_cancel(
        self,
        turn: TurnContext | None,
        manager_token: CancelToken | None,
        tts_task: asyncio.Task[None] | None,
        prompt_cleanup: asyncio.Task[bool],
        *,
        barge_in: bool,
        successor_expected: bool,
    ) -> None:
        """Drain captured work after the audible cutoff has completed."""
        tts_cleanup = asyncio.create_task(
            self._tts_scheduler.finish_turn_cancel(tts_task),
            name="tts_turn_cancel_cleanup",
        )
        stt_cleanup_complete = True
        try:
            if barge_in:
                await self._notify_barge_in(turn)
            if not successor_expected and self._cancel_cleanup_owns_turn(turn, manager_token):
                await self._turn_runner.cancel_preemptive_generation()
                if self._cancel_cleanup_owns_turn(turn, manager_token):
                    stt_cleanup_complete = await self._stt_committer.cancel(turn)
        finally:
            try:
                await tts_cleanup
            finally:
                await prompt_cleanup

        if stt_cleanup_complete is False:
            raise RuntimeError(
                "STT provider cleanup remains lifecycle-owned; retry cancellation after it settles"
            )
        if not barge_in and self._cancel_cleanup_owns_turn(turn, manager_token):
            self._reset_turn_state()

    async def _begin_barge_in(self) -> None:
        """Cut off playback inline and detach only old-turn cleanup work.

        This callback returns after the transport has been cleared so audio
        ingress can immediately resume and the turn manager can install the
        successor user turn. Provider cleanup, application interruption
        handlers, and signal journaling remain runtime-owned.
        """
        turn = self._turn
        manager_token, tts_task, prompt_cleanup = await self._cut_off_turn_playback(
            turn, barge_in=True
        )
        cleanup = self._runtime_scope.create_journaled_task(
            self._finish_turn_cancel(
                turn,
                manager_token,
                tts_task,
                prompt_cleanup,
                barge_in=True,
                successor_expected=True,
            ),
            name=_BARGE_IN_CLEANUP_TASK,
            journal_sink=self._journal_sink,
            turn_id=turn.id if turn is not None else None,
        )
        cleanup.add_done_callback(self._runtime_scope.log_task_exception)

    async def cancel_turn(self, *, barge_in: bool = False) -> None:
        """Cancel the active turn and await full provider cleanup.

        Runtime barge-in uses the internal fast callback above so the audio
        ingress loop waits only for the audible playback cutoff.
        """
        turn = self._turn
        manager_token, tts_task, prompt_cleanup = await self._cut_off_turn_playback(
            turn,
            barge_in=barge_in,
        )
        await self._finish_turn_cancel(
            turn,
            manager_token,
            tts_task,
            prompt_cleanup,
            barge_in=barge_in,
            successor_expected=False,
        )

    async def cancel_tts_playback(self) -> None:
        """Stop TTS provider and flush outbound audio.

        Unlike :meth:`cancel_turn`, this does NOT cancel the shared
        ``cancel_token`` so any in-flight agent stream can continue
        producing text (which will simply not be synthesized).

        Constraint: never cancel ``active_turn_task`` here — it is the whole
        ``on_turn_ended`` coroutine (agent consumer included), so cancelling it
        would abort the agent stream.
        """
        turn = self._turn
        self._tts_scheduler.set_playback_suppressed(True)
        self._outbound_queue.flush_for_new_turn()
        self._audio_router.reset_replay_chunks()
        # Start provider cancellation before the transport clear yields to a
        # successor admission. Calling the shared provider afterwards could
        # cancel synthesis that belongs to the newly installed turn.
        await self._tts_scheduler.synthesizer.cancel()
        await clear_audio_if_supported(self.transport)
        # A VAD barge-in can install a successor while a delayed transport
        # clear is in flight. Its shared TTS provider and BOT_SPEAKING state
        # belong to that successor, so this stale playback-only cancellation
        # must not touch either one.
        if self._turn is not turn:
            return
        if self._turn is turn and self._turn_manager.state == TurnManagerState.BOT_SPEAKING:
            self._reset_turn_state()

    async def _cut_off_tts_for_text_replacement(self) -> None:
        """Clear stale speech without cancelling the agent or ending its turn.

        An indexed bridge replacement can still repair buffered text. Once a
        TTS payload has been admitted, however, replaying the replacement
        would duplicate any prefix the caller already heard. The streaming
        consumer invokes this cutoff and suppresses further TTS while the
        agent continues to produce the corrected final transcript.
        """
        self._tts_scheduler.set_playback_suppressed(True)
        self._outbound_queue.flush_for_new_turn()
        self._audio_router.reset_replay_chunks()
        await self._tts_scheduler.synthesizer.cancel()
        await clear_audio_if_supported(self.transport)

    async def reset_state(self) -> None:
        """Cancel everything and return to idle/listening state.

        Also clears agent conversation history if the agent supports it.
        """
        turn = self._turn
        if turn:
            turn.cancel_token.cancel()
        await self._turn_runner.cancel_application_prompt()

        self._tts_scheduler.set_playback_suppressed(True)
        tts_task = self._tts_scheduler.request_turn_cancel()
        self._outbound_queue.flush_for_new_turn()
        self._audio_router.reset_replay_chunks()
        await clear_audio_if_supported(self.transport)
        await self._turn_runner.cancel_preemptive_generation()
        stt_cleanup_complete = await self._stt_committer.cancel(turn)
        await self._tts_scheduler.finish_turn_cancel(tts_task)
        if stt_cleanup_complete is False:
            raise RuntimeError(
                "STT provider cleanup remains lifecycle-owned; "
                "retry reset_state() after it settles"
            )

        self.agent.reset()
        self._agent_stage.reset_history()

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

        actions = self._session_actions.drain(preserve_no_interrupt=True)
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
        if self._closed or self._stopping:
            raise RuntimeError("Session is stopping or has been stopped")
        if not self._is_running:
            raise RuntimeError("Session is not running")
        await self._turn_manager.start_turn()

    async def end_turn(self) -> None:
        """Manually end the current user turn (push-to-talk mode)."""
        if self._closed or self._stopping:
            raise RuntimeError("Session is stopping or has been stopped")
        await self._turn_manager.end_turn()

    def _stop_helpers(self) -> None:
        """Stop attached helper components that own event subscriptions/state."""
        for helper in self.telephony.helpers:
            try:
                helper.stop()
            except Exception:
                logger.debug("Error stopping session helper", exc_info=True)

    async def _close_action_executors(self) -> None:
        """Release provider resources owned by optional action executors.

        Every executor is attempted even when one fails; the first error is
        raised after the loop so ``stop()`` can report it alongside the other
        runtime close errors.
        """
        first_error: Exception | None = None
        for executor in self._action_executors:
            try:
                await close_if_supported(executor)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                logger.warning(
                    "Error closing session action executor %r",
                    executor,
                    exc_info=True,
                )
        if first_error is not None:
            raise first_error

    async def _close_audio_providers(self, *, skip_stt: bool = False) -> None:
        """Release optional resources owned by audio providers."""
        providers = (
            ("stt", self.stt),
            ("tts", self.tts),
            ("vad", self.vad),
            ("noise_reducer", self.noise_reducer),
            ("echo_canceller", self.echo_canceller),
        )
        closed: set[int] = set()
        deferred = {id(self.stt)} if skip_stt else set()
        close_errors: list[Exception] = []
        for name, provider in providers:
            provider_id = id(provider)
            if provider_id in closed or provider_id in deferred:
                continue
            closed.add(provider_id)
            try:
                await close_if_supported(provider)
            except Exception as exc:
                close_errors.append(exc)
                logger.warning("Error closing %s provider", name, exc_info=True)
        if close_errors:
            primary = close_errors[0]
            if len(close_errors) > 1:
                raise primary from close_errors[1]
            raise primary

    async def _drain_force_runtime_signals(
        self,
        signals: tuple[RuntimeCohortSignal, ...],
        *,
        deferred: bool,
    ) -> None:
        """Drain force-signalled work in its owner-safe teardown phase."""
        current = asyncio.current_task()
        for signal in signals:
            requires_owner_close = signal.includes_action(RuntimeTaskAction.FINISH)
            if requires_owner_close is not deferred:
                continue
            try:
                await self._runtime_scope.drain_cohort(signal)
            except asyncio.CancelledError:
                if current is not None and current.cancelling():
                    raise
            except Exception:  # noqa: BLE001, S110 - preserve best-effort force drain
                # Preserve the legacy force drain's best-effort settlement.
                pass

    # ── Internal helpers ───────────────────────────────────────

    def _maybe_attach_event_bus(self, provider: Any) -> None:
        """Attach the session bus through the public hook or legacy attributes."""
        set_event_bus = getattr(provider, "set_event_bus", None)
        if callable(set_event_bus):
            try:
                result = set_event_bus(self.event_bus)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    raise TypeError("set_event_bus() must be synchronous")
                return
            except Exception:
                logger.warning(
                    "Provider %r rejected set_event_bus(); trying legacy EventBus attachment",
                    provider,
                    exc_info=True,
                )

        attached = False
        cfg = getattr(provider, "_config", None)
        if cfg is not None and hasattr(cfg, "event_bus") and cfg.event_bus is None:
            try:
                cfg.event_bus = self.event_bus
                attached = True
            except Exception:
                logger.warning(
                    "Failed to attach session EventBus to %r; provider-scoped events may be muted",
                    provider,
                    exc_info=True,
                )
        has_unset_bus = hasattr(provider, "_event_bus") and provider._event_bus is None
        if not attached and has_unset_bus:
            try:
                provider._event_bus = self.event_bus
            except Exception:
                logger.warning(
                    "Failed to attach session EventBus to %r; provider-scoped events may be muted",
                    provider,
                    exc_info=True,
                )

    def _maybe_bind_session_id(self, provider: Any) -> None:
        """Give an EasyCat-event producer this Session's correlation id."""
        set_session_id = getattr(provider, "set_session_id", None)
        if not callable(set_session_id):
            return
        try:
            result = set_session_id(self.session_id)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise TypeError("set_session_id() must be synchronous")
        except Exception:
            logger.warning(
                "Provider %r rejected set_session_id(); its events may lack correlation",
                provider,
                exc_info=True,
            )

    # ── Text mode ──────────────────────────────────────────────

    async def prompt_agent(
        self,
        text: str,
        *,
        role: Literal["system", "user"] = "system",
        speak: bool = True,
    ) -> str:
        """Run an application-initiated agent turn and optionally speak it."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        if role not in ("system", "user"):
            raise ValueError("role must be 'system' or 'user'")
        if not isinstance(speak, bool):
            raise TypeError("speak must be a bool")
        if self._closed:
            raise RuntimeError("Session has been stopped")
        if self._stopping:
            raise RuntimeError("Session is stopping")
        if speak and self._runtime_mode == "text_session":
            raise RuntimeError("speak=True is unavailable in text_session mode")
        if speak and not self._is_running:
            raise RuntimeError("Session must be started before speak=True")
        if self._turn is not None or self._turn_manager.state != TurnManagerState.IDLE:
            await self.cancel_turn()
        if self._stopping:
            raise RuntimeError("Session is stopping")
        was_active = self._observability_active
        self._mark_observability_active()
        try:
            with observability.span("easycat.session", {"easycat.surface": "agent_bridge"}):
                return await self._turn_runner.prompt_agent(
                    text.strip(),
                    role=role,
                    speak=speak,
                    admit=lambda: not self._closed and not self._stopping,
                )
        finally:
            if not was_active:
                self._mark_observability_inactive()

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
        if self._stopping:
            raise RuntimeError("Session is stopping")
        self._mark_observability_active()
        try:
            with observability.span("easycat.session", {"easycat.surface": "agent_bridge"}):
                return await self._turn_runner.send_text(
                    text,
                    admit=lambda: not self._closed and not self._stopping,
                )
        finally:
            self._mark_observability_inactive()
