"""Assembles a Session's collaborators out of its config + providers.

``Session.__init__`` is a short field-assignment shell: it stores the
config, resolves provider fallbacks, sets up the agent, runs noop
validation, creates the event bus / turn manager / caller-id state, then
calls :func:`build_session` to construct everything else.

This module owns ALL collaborator construction that used to be inlined in
the constructor — the seven pipeline stages, the shared ``RunContext``,
the ``no-turn`` ``TurnContext``, the journal sink, the outbound audio
queue, the AudioRouter / STTCommitter / TTSScheduler / CancelOrchestrator
/ TurnRunner, and the greeting + opt-out collaborators — plus the
deferred event-bus subscriptions and TurnManager bindings.  Pulling this
out of ``__init__`` keeps construction and subscription from interleaving
in the constructor body, and concentrates the dependency order here where
the typed :class:`SessionWiringContext` makes it safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from easycat._bounded_queue import BoundedAudioQueue, DropPolicy
from easycat._turn_context import TurnContext
from easycat.cancel import CancelToken
from easycat.events import (
    PlaybackMarkAck,
    TransportAudioDelivered,
    TurnEnded,
    TurnStarted,
    VADStartSpeaking,
    VADStopSpeaking,
)
from easycat.runtime.context import RunContext
from easycat.session._audio_router import AudioRouter
from easycat.session._cancel_orchestrator import CancelOrchestrator
from easycat.session._greeting import GreetingController
from easycat.session._journal_sink import SessionJournalSink
from easycat.session._opt_out import OptOutPolicy
from easycat.session._stt_committer import STTCommitter
from easycat.session._tts_scheduler import TTSScheduler
from easycat.session._turn_runner import TurnRunner
from easycat.session._wiring import _SessionTurnHandle, build_wiring
from easycat.stages.agent import AgentStage
from easycat.stages.audio import AudioStage
from easycat.stages.stt import STTStage
from easycat.stages.transport import TransportStage
from easycat.stages.tts import TTSStage
from easycat.stages.turn import TurnStage
from easycat.stages.vad import VADStage

if TYPE_CHECKING:
    from easycat.session._session import Session
    from easycat.session._types import SessionConfig

_OUTBOUND_QUEUE_MAX_SIZE = 200
_OUTBOUND_QUEUE_POLICY = DropPolicy.DROP_NEWEST
_OUTBOUND_QUEUE_NAME = "outbound_audio"


@dataclass(frozen=True)
class SessionComponents:
    """The assembled collaborator bundle handed back to ``Session.__init__``.

    Session unpacks these onto its private fields (keeping the historical
    names ``_audio_router``, ``_stt_committer``, … so the test fauna that
    pokes those attributes keeps working).
    """

    run_ctx: RunContext
    no_turn: TurnContext
    journal_sink: SessionJournalSink
    outbound_queue: BoundedAudioQueue

    stt_stage: STTStage
    tts_stage: TTSStage
    vad_stage: VADStage
    audio_stage: AudioStage
    transport_stage: TransportStage
    agent_stage: AgentStage
    turn_stage: TurnStage

    audio_router: AudioRouter
    stt_committer: STTCommitter
    tts_scheduler: TTSScheduler
    cancel_orchestrator: CancelOrchestrator
    turn_runner: TurnRunner

    greeting: GreetingController
    opt_out: OptOutPolicy


def build_session(session: Session, cfg: SessionConfig) -> SessionComponents:
    """Construct every Session collaborator and wire its subscriptions.

    Expects ``session`` to already have its primitive fields assigned
    (providers, flags, event bus, turn manager, caller-id state, journal
    handles, runtime scope, outbound-queue config, session actions, turn
    pointers).  Returns the assembled bundle; the caller unpacks it.
    """
    journal = session._journal
    event_bus = session.event_bus

    # ── Shared context + leaf objects ────────────────────────────
    run_ctx = RunContext(
        run_id=session.session_id,
        session_id=session.session_id,
        runtime_mode=cfg.runtime_mode,
        journal=journal,
        artifact_store=session._artifact_store,
    )
    no_turn = TurnContext(turn_id="no-turn", cancel_token=CancelToken())

    journal_sink = SessionJournalSink(
        event_bus=event_bus,
        journal=journal,
        artifact_store=session._artifact_store,
        session_id=session.session_id,
        current_turn_id=session._journal_turn_id,
    )
    journal_sink.subscribe()

    # Outbound (played-back) audio queue.  Shared between the TTS
    # synthesizer (producer) and the AudioRouter (drain consumer); built
    # once here and handed to both.  Outbound speech must NOT use
    # DROP_OLDEST — dropping the earliest unsent bot audio makes the
    # listener hear the utterance jump forward; DROP_NEWEST trims only the
    # tail when the transport falls behind.  Callers wanting real
    # backpressure inject a BLOCK-policy queue via
    # ``SessionConfig.outbound_queue``.
    outbound_queue = cfg.outbound_queue or BoundedAudioQueue(
        max_size=_OUTBOUND_QUEUE_MAX_SIZE,
        policy=_OUTBOUND_QUEUE_POLICY,
        name=_OUTBOUND_QUEUE_NAME,
        on_drop=session._on_queue_drop,
    )

    # ── Stages (the debug / replay surface) ──────────────────────
    stt_stage = STTStage(session.stt, journal=journal)
    tts_stage = TTSStage(session.tts, journal=journal)
    vad_stage = VADStage(session.vad, journal=journal)
    audio_stage = AudioStage(
        session.noise_reducer,
        echo_canceller=session.echo_canceller if session._enable_aec else None,
        journal=journal,
    )
    transport_stage = TransportStage(session.transport, journal=journal)
    agent_stage = AgentStage(
        session.agent,
        journal=journal,
        artifact_store=session._artifact_store,
        session_id=session.session_id,
        mcp_servers=tuple(cfg.mcp_servers),
    )
    turn_stage = TurnStage(
        session._turn_manager.endpoint_detector,
        journal=journal,
    )

    # ── Typed late-binding wiring (replaces ~40 inline lambdas) ──
    # Built once from the live Session; every collaborator reads its
    # Session-derived getters off this single object.  The getters
    # resolve live state when called, so construction order below is not
    # a footgun (e.g. AudioRouter can reference is_stt_active() before
    # the STTCommitter that backs it exists).
    wiring = build_wiring(session)

    tm_config = getattr(session._turn_manager, "_config", None)
    stt_segment_silence_ms = max(0, getattr(tm_config, "stt_segment_silence_ms", 0))

    # ── AudioRouter ──────────────────────────────────────────────
    audio_router = AudioRouter(
        wiring=wiring,
        transport=session.transport,
        audio_stage=audio_stage,
        vad_stage=vad_stage,
        stt_stage=stt_stage,
        transport_stage=transport_stage,
        turn_manager=session._turn_manager,
        event_bus=event_bus,
        journal_sink=journal_sink,
        run_ctx=run_ctx,
        no_turn=no_turn,
        echo_canceller=session.echo_canceller,
        outbound_queue=outbound_queue,
    )
    event_bus.subscribe(PlaybackMarkAck, audio_router.on_playback_ack)
    event_bus.subscribe(TransportAudioDelivered, audio_router.on_audio_delivered)

    # ── STTCommitter ─────────────────────────────────────────────
    stt_committer = STTCommitter(
        wiring=wiring,
        event_bus=event_bus,
        journal_sink=journal_sink,
        runtime_scope=session._runtime_scope,
        timeout_config=session._timeout_config,
        segment_silence_ms=stt_segment_silence_ms,
        no_turn=no_turn,
        turn_manager=session._turn_manager,
        on_speech_detection_reset=audio_router.reset_speech_detection,
    )
    event_bus.subscribe(VADStopSpeaking, stt_committer.schedule)
    event_bus.subscribe(VADStartSpeaking, stt_committer.cancel_scheduled)

    # ── TTSScheduler ─────────────────────────────────────────────
    tts_scheduler = TTSScheduler(
        wiring=wiring,
        tts_stage=tts_stage,
        turn_manager=session._turn_manager,
        event_bus=event_bus,
        journal_sink=journal_sink,
        run_ctx=run_ctx,
        no_turn=no_turn,
        audio_router=audio_router,
        outbound_queue=outbound_queue,
        timeout_config=session._timeout_config,
        audio_gate=cfg.audio_gate,
        output_processors=list(cfg.output_processors),
        strip_markdown_enabled=cfg.strip_markdown,
    )

    # ── CancelOrchestrator ───────────────────────────────────────
    # Owns control-signal propagation and barge-in policy; needs all 7
    # stages above.  Wired into the TurnManager as the barge-in callback.
    cancel_orchestrator = CancelOrchestrator(
        wiring=wiring,
        transport_stage=transport_stage,
        tts_stage=tts_stage,
        agent_stage=agent_stage,
        turn_stage=turn_stage,
        stt_stage=stt_stage,
        vad_stage=vad_stage,
        audio_stage=audio_stage,
        run_ctx=run_ctx,
        journal_sink=journal_sink,
        interruption_mode=cfg.interruption_mode,
        interruption_latency_compensation_ms=cfg.interruption_latency_compensation_ms,
        interruption_ack_stale_ms=cfg.interruption_ack_stale_ms,
        interruption_ack_tail_cap_ms=cfg.interruption_ack_tail_cap_ms,
    )
    session._turn_manager.set_cancel_callback(cancel_orchestrator.for_barge_in)

    # ── TurnRunner ───────────────────────────────────────────────
    # The hub: depends on every collaborator above.  Its TurnStarted /
    # TurnEnded subscriptions are wired after construction.
    turn_runner = TurnRunner(
        wiring=wiring,
        stt_committer=stt_committer,
        tts_scheduler=tts_scheduler,
        audio_router=audio_router,
        cancel_orchestrator=cancel_orchestrator,
        turn_manager=session._turn_manager,
        agent_stage=agent_stage,
        run_ctx=run_ctx,
        event_bus=event_bus,
        journal_sink=journal_sink,
        runtime_scope=session._runtime_scope,
        timeout_config=session._timeout_config,
        turn_handle=_SessionTurnHandle(session),
        stt_stage=stt_stage,
        session_id=session.session_id,
        journal_enabled=journal is not None,
    )
    event_bus.subscribe(TurnStarted, turn_runner.on_turn_started)
    event_bus.subscribe(TurnEnded, turn_runner.schedule_turn_ended)

    # Plug the TurnStage into the TurnManager's endpoint-detector call so
    # smart-turn decisions go through stage.execute() and produce journal
    # records.
    if session._turn_manager.endpoint_detector is not None:
        session._turn_manager.bind_endpoint_stage(
            turn_stage,
            run_ctx_getter=lambda: run_ctx,
            turn_getter=lambda: session._turn or no_turn,
        )

    # ── Greeting + opt-out collaborators ─────────────────────────
    # Built last: they only reference Session via callables (synthesize /
    # caller-id / actions / stop), so they self-subscribe to the bus
    # without needing anything constructed after them.  ``synthesize``
    # re-reads ``session.synthesize_bypass`` per call so test patches
    # (``session.synthesize_bypass = AsyncMock()``) stay observable.
    greeting = GreetingController(
        greeting=cfg.greeting,
        synthesize=lambda text: session.synthesize_bypass(text),
        event_bus=event_bus,
        runtime_scope=session._runtime_scope,
        journal_sink=journal_sink,
    )
    opt_out = OptOutPolicy(
        enabled=cfg.opt_out_detection,
        phrases=(list(cfg.opt_out_phrases) if cfg.opt_out_phrases is not None else None),
        dnc_list=cfg.dnc_list,
        caller_id=session._caller_id,
        session_actions=wiring.session_actions,
        emit=session._emit,
        stop=lambda: session.stop(),
        event_bus=event_bus,
        runtime_scope=session._runtime_scope,
        journal_sink=journal_sink,
    )

    return SessionComponents(
        run_ctx=run_ctx,
        no_turn=no_turn,
        journal_sink=journal_sink,
        outbound_queue=outbound_queue,
        stt_stage=stt_stage,
        tts_stage=tts_stage,
        vad_stage=vad_stage,
        audio_stage=audio_stage,
        transport_stage=transport_stage,
        agent_stage=agent_stage,
        turn_stage=turn_stage,
        audio_router=audio_router,
        stt_committer=stt_committer,
        tts_scheduler=tts_scheduler,
        cancel_orchestrator=cancel_orchestrator,
        turn_runner=turn_runner,
        greeting=greeting,
        opt_out=opt_out,
    )


__all__ = ["SessionComponents", "build_session"]
