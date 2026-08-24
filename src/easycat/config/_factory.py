"""Session-building factories: :func:`create_session` / :func:`create_text_session`.

This module owns everything that needs the :class:`Session` class — the two
public factories plus the Session-coupled helpers (transport building and
journal provider-version emission). The provider-factory names that tests
monkeypatch (``create_vad``, ``create_noise_reducer``, …) are bound here so a
``monkeypatch.setattr("easycat.config._factory.create_vad", ...)`` lands in the
same globals :func:`create_session` resolves them from.

Telephony runtime wiring is imported LAZILY (inside :func:`create_session`)
from :mod:`easycat.config._telephony_wiring`, so a non-telephony session never
loads the outbound stack. The PEP 562 ``__getattr__`` below exposes
:class:`OutboundCallManager` as a lazily-resolved module attribute (kept off
the eager import set) that telephony wiring resolves — and tests patch —
through ``easycat.config._factory.OutboundCallManager``.
"""

from __future__ import annotations

import copy
import inspect
import logging
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from easycat._provider_catalog import inject_event_bus
from easycat.echo_cancellation import EchoCancellationConfig, create_echo_canceller
from easycat.events import EventBus
from easycat.integrations.agents import ExternalAgentBridge
from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.noise_reduction import NoiseReducerConfig, create_noise_reducer
from easycat.providers import TransportLike
from easycat.runtime.artifacts import FilesystemArtifactStore, InMemoryArtifactStore
from easycat.runtime.capabilities import bind_identity_sink_if_supported
from easycat.runtime.journal_factory import create_journal
from easycat.session._session import Session
from easycat.session._types import Agent as _AgentProto
from easycat.session._types import SessionConfig, SessionHelper
from easycat.session.actions import SessionActionExecutor
from easycat.smart_turn import SmartTurnConfig, create_smart_turn
from easycat.stt.factory import (
    STTProviderConfig,
    create_stt_provider,
    create_stt_provider_from_config,
)
from easycat.stubs import NoopAgent
from easycat.transports._webrtc_config import WebRTCTransportConfig
from easycat.transports.local import LocalTransportConfig
from easycat.transports.telnyx_media import TelnyxTransportConfig
from easycat.transports.twilio_media import TwilioTransportConfig
from easycat.transports.websocket import WebSocketTransportConfig
from easycat.transports.webtransport import WebTransportTransportConfig
from easycat.tts.factory import (
    TTSProviderConfig,
    create_tts_provider,
    create_tts_provider_from_config,
)
from easycat.turn_manager import TurnManagerConfig, TurnMode
from easycat.vad import create_vad

from .easy import (
    EasyConfig,
    EasyConfigError,
    OutboundCallConfig,
    TelephonyConfig,
    TextSessionConfig,
    TransportConfig,
    _AgentSessionConfig,
    _inject_agent_runtime,
)

if TYPE_CHECKING:
    from easycat.runtime.journal import ExecutionJournal
    from easycat.telephony.call_state import OutboundCallStateMachine

    from ._telephony_wiring import TelephonyHelpers

logger = logging.getLogger("easycat.config")

# Re-export the provider factories so they keep their historical
# ``easycat.config`` binding semantics — see the module docstring on
# monkeypatching. ``create_stt_provider_from_config`` /
# ``create_tts_provider_from_config`` / ``create_vad`` /
# ``create_noise_reducer`` / ``create_echo_canceller`` are referenced
# unqualified below so a patch on this module's namespace takes effect.
__all__ = [
    "create_session",
    "create_text_session",
]


# Lazily-resolved telephony runtime class. Kept out of the module-level import
# set so a non-telephony session never loads the outbound stack. Exposed as a
# module attribute via PEP 562 ``__getattr__`` so ``_telephony_wiring`` can
# reference it through the module namespace and tests can ``monkeypatch`` it.
_LAZY_RUNTIME_IMPORTS = {
    "OutboundCallManager": "easycat.telephony.outbound",
}


def __getattr__(name: str) -> Any:
    module_path = _LAZY_RUNTIME_IMPORTS.get(name)
    if module_path is not None:
        import importlib

        value = getattr(importlib.import_module(module_path), name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _transport_factories() -> dict[type[TransportConfig], Any]:
    """Build the ``{config type -> factory}`` map, importing transport
    implementation classes lazily so they never load at config import time.

    Keyed by config type and rebuilt on each ``_create_transport`` call;
    Python's import cache makes the second build essentially free, and
    sessions are not built in hot loops, so the cost is irrelevant next
    to keeping ``EasyConfig`` cold starts free of every transport SDK.
    """
    from easycat.transports.local import LocalTransport
    from easycat.transports.telnyx_media import TelnyxTransport
    from easycat.transports.twilio_media import TwilioTransport
    from easycat.transports.webrtc import WebRTCTransport
    from easycat.transports.websocket import WebSocketTransport
    from easycat.transports.webtransport import WebTransportTransport

    return {
        LocalTransportConfig: lambda config, event_bus: LocalTransport(config),
        WebSocketTransportConfig: lambda config, event_bus: WebSocketTransport(config),
        TwilioTransportConfig: lambda config, event_bus: TwilioTransport(
            config=config, event_bus=event_bus
        ),
        TelnyxTransportConfig: lambda config, event_bus: TelnyxTransport(
            config=config, event_bus=event_bus
        ),
        WebRTCTransportConfig: lambda config, event_bus: WebRTCTransport(config),
        WebTransportTransportConfig: lambda config, event_bus: WebTransportTransport(config),
    }


def _create_transport(config: TransportConfig, event_bus: EventBus) -> Any:
    # Discriminate a pre-built transport *instance* from a transport *config*
    # using the narrow audio contract (TransportLike) rather than the full
    # Transport protocol. The full protocol also requires version_info(), so
    # checking it here would silently reject custom transports that satisfy the
    # audio contract but do not implement version_info(), routing them to the
    # config-factory path and raising a misleading "Unsupported ..." error.
    if isinstance(config, TransportLike):
        if hasattr(config, "_event_bus") and config._event_bus is None:
            config._event_bus = event_bus
        return config
    factory = _transport_factories().get(type(config))
    if factory is None:
        raise ValueError(
            f"Unsupported transport configuration type: {type(config).__name__!r}. "
            "Pass a known transport config or a transport instance implementing "
            "the connect/disconnect/receive_audio/send_audio contract."
        )
    return factory(config, event_bus)


def _is_stt_provider_instance(value: Any) -> bool:
    return not isinstance(value, type) and all(
        callable(getattr(value, name, None))
        for name in ("start_stream", "send_audio", "commit_segment", "end_stream", "events")
    )


def _create_stt(config: Any, event_bus: EventBus) -> Any:
    if _is_stt_provider_instance(config):
        return config
    if isinstance(config, STTProviderConfig):
        return create_stt_provider(config, event_bus)
    return create_stt_provider_from_config(config, event_bus)


def _is_tts_provider_instance(value: Any) -> bool:
    return not isinstance(value, type) and all(
        callable(getattr(value, name, None)) for name in ("synthesize", "stop", "cancel")
    )


def _create_tts(config: Any, event_bus: EventBus) -> Any:
    if _is_tts_provider_instance(config):
        return config
    if isinstance(config, TTSProviderConfig):
        return create_tts_provider(config, event_bus)
    return create_tts_provider_from_config(config, event_bus)


def _is_vad_provider_instance(value: Any) -> bool:
    return not isinstance(value, type) and all(
        callable(getattr(value, name, None)) for name in ("process", "configure")
    )


def _create_vad(config: Any) -> Any:
    if _is_vad_provider_instance(config):
        return config
    return create_vad(config)


def _is_noise_reducer_instance(value: Any) -> bool:
    return not isinstance(value, type) and callable(getattr(value, "process", None))


def _resolve_noise_reducer(config: Any) -> Any:
    if _is_noise_reducer_instance(config):
        return config
    return create_noise_reducer(config)


def _is_echo_canceller_instance(value: Any) -> bool:
    return not isinstance(value, type) and all(
        callable(getattr(value, name, None)) for name in ("process", "feed_reference")
    )


def _resolve_echo_canceller(config: Any) -> Any:
    if _is_echo_canceller_instance(config):
        return config
    return create_echo_canceller(config)


def _create_artifact_store(
    session_id: str, debug: str, *, data_dir: str | Path | None = None
) -> InMemoryArtifactStore | FilesystemArtifactStore | None:
    if debug == "off":
        return None
    if debug == "full":
        return FilesystemArtifactStore(session_id, data_dir=data_dir)
    return InMemoryArtifactStore()


def _normalized_smart_turn(config: EasyConfig) -> SmartTurnConfig:
    """Return the smart-turn config normalized by ``EasyConfig.__post_init__``."""
    smart_turn = config.smart_turn
    assert isinstance(smart_turn, SmartTurnConfig)
    return smart_turn


def _should_auto_turn_from_stt_final(config: EasyConfig) -> bool:
    """Whether this session should derive turn boundaries from STT finals.

    True for STT providers that do their own endpointing (Deepgram Flux,
    Cartesia ink-2, ElevenLabs realtime VAD) — unless an explicit endpointing
    choice overrides it: push-to-talk, smart-turn, or a telephony voicemail
    detector all keep EasyCat's own VAD + commit path. When True, the caller
    disables the Silero VAD stage (``enable_vad = not auto_turn``) and the STT
    committer stops driving manual commits, so the provider's native VAD is the
    single source of turn boundaries (no double endpointing / duplicate FINALs).
    """
    from .easy import _stt_uses_native_endpointing

    if config.turn_taking.mode == TurnMode.PUSH_TO_TALK:
        return False
    if _normalized_smart_turn(config).enabled:
        return False
    if config.telephony and config.telephony.enable_voicemail_detector:
        return False
    return _stt_uses_native_endpointing(config.stt)


def _validate_agent_shape(adapted: Any, *, wrap_agent: bool) -> None:
    """Fail fast when ``agent=`` won't survive the first turn.

    Called on the ``auto_adapt_agent`` output *before* the
    :class:`AgentRunner` wrap — ``AgentRunner`` satisfies both contracts,
    so a post-wrap check would be a no-op.  A fully-built
    :class:`ExternalAgentBridge` is accepted as-is.  Otherwise the object
    must satisfy the :class:`Agent` protocol *and* expose an
    ``async run`` method: ``@runtime_checkable`` only checks method-name
    presence, so the :func:`inspect.iscoroutinefunction` tightening is
    what actually catches a sync / non-callable ``run``.  Skipped when
    ``wrap_agent`` is False so deliberate custom-bridge flows pass.
    """
    if not wrap_agent or isinstance(adapted, ExternalAgentBridge):
        return
    run_attr = getattr(adapted, "run", None)
    if not (isinstance(adapted, _AgentProto) and inspect.iscoroutinefunction(run_attr)):
        raise EasyConfigError(
            "agent must expose `async run(text) -> str` or be a recognized "
            "framework agent (see auto_adapt_agent's supported list)."
        )


def _safe_config_ns(config: EasyConfig) -> object:
    """Build a lightweight namespace snapshot of the safe config fields.

    Only copies the fields that ``safe_config_snapshot`` reads so we
    never attempt to deep-copy live client objects, agents, or other
    non-picklable instances on the config.
    """
    from types import SimpleNamespace

    from easycat.runtime.safe_defaults import SAFE_CONFIG_FIELDS

    attrs: dict[str, Any] = {}
    for name in SAFE_CONFIG_FIELDS:
        val = getattr(config, name, None)
        if val is None:
            continue
        # Shallow-copy dataclass values so later mutation of the original
        # config (e.g. turn_taking.end_of_turn_silence_ms = 500) doesn't
        # retroactively change the snapshot.
        attrs[name] = copy.copy(val) if hasattr(val, "__dataclass_fields__") else val
    return SimpleNamespace(**attrs)


def _audio_runtime_config(config: EasyConfig) -> EasyConfig:
    """Copy the mutable user specification before build-time normalization.

    ``EasyConfig`` remains mutable for backward compatibility, but session
    construction treats the supplied object as a specification. The runtime
    copy owns fields that validation may normalize and mutable collections that
    a downstream collaborator could otherwise retain. Already-built provider,
    transport, and agent instances remain shared intentionally: callers who
    inject live objects own their lifecycle and concurrency contract.
    """
    runtime = copy.copy(config)
    runtime.turn_taking = copy.copy(config.turn_taking)
    runtime.timeouts = copy.copy(config.timeouts)
    runtime.mcp_servers = list(config.mcp_servers) if config.mcp_servers is not None else None
    runtime.output_processors = tuple(config.output_processors)
    runtime.action_executors = tuple(config.action_executors)
    telephony = config.telephony
    if telephony is not None:
        if not isinstance(telephony, TelephonyConfig):
            raise EasyConfigError("telephony must be a TelephonyConfig instance or None.")
        outbound = telephony.outbound
        if outbound is not None and not isinstance(outbound, OutboundCallConfig):
            raise EasyConfigError("telephony.outbound must be an OutboundCallConfig instance.")
        runtime.telephony = copy.copy(telephony)
        runtime.telephony.dtmf_aggregator = copy.copy(telephony.dtmf_aggregator)
        runtime.telephony.voicemail_detector = copy.copy(telephony.voicemail_detector)
        if outbound is not None:
            runtime.telephony.outbound = copy.copy(outbound)
            runtime.telephony.outbound.voicemail_detection = copy.copy(
                outbound.voicemail_detection
            )
            if outbound.retry_strategy is not None:
                runtime.telephony.outbound.retry_strategy = copy.copy(outbound.retry_strategy)
    return runtime


def _text_runtime_config(config: TextSessionConfig) -> TextSessionConfig:
    """Copy mutable text-session specification fields for one runtime."""
    runtime = copy.copy(config)
    runtime.mcp_servers = list(config.mcp_servers) if config.mcp_servers is not None else None
    return runtime


def _merge_twilio_identity(existing: Any, incoming: Any) -> Any:
    """Preserve an existing call identity while adding Twilio metadata."""
    if incoming is None:
        return existing
    if existing is None:
        return incoming

    updates: dict[str, Any] = {}
    incoming_call_sid = getattr(incoming, "call_sid", None)
    if getattr(existing, "call_sid", None) is None and incoming_call_sid:
        updates["call_sid"] = incoming_call_sid

    existing_fields = getattr(existing, "custom_fields", None)
    incoming_fields = getattr(incoming, "custom_fields", None)
    if isinstance(existing_fields, dict) and isinstance(incoming_fields, dict):
        merged_fields = dict(incoming_fields)
        merged_fields.update(existing_fields)
        if merged_fields != existing_fields:
            updates["custom_fields"] = merged_fields

    if not updates:
        return existing
    if hasattr(existing, "__dataclass_fields__"):
        from dataclasses import replace

        return replace(existing, **updates)

    merged = copy.copy(existing)
    for key, value in updates.items():
        setattr(merged, key, value)
    return merged


def _emit_provider_versions(
    journal: Any,
    session_id: str,
    *,
    stt: Any = None,
    tts: Any = None,
    transport: Any = None,
    vad: Any = None,
    noise_reducer: Any = None,
    echo_canceller: Any = None,
    agent: Any = None,
) -> None:
    """Write a single journal record with version info from all providers."""
    from easycat.runtime.record_contracts import validate_builtin_record
    from easycat.runtime.records import JournalRecordKind

    versions: dict[str, dict[str, str]] = {}
    for role, provider in [
        ("stt", stt),
        ("tts", tts),
        ("transport", transport),
        ("vad", vad),
        ("noise_reducer", noise_reducer),
        ("echo_canceller", echo_canceller),
        ("agent", agent),
    ]:
        if provider is not None and hasattr(provider, "version_info"):
            versions[role] = provider.version_info()
    kind = JournalRecordKind.EVENT
    validate_builtin_record(name="provider_versions", kind=kind, data=versions)
    journal.append(
        kind=kind,
        name="provider_versions",
        session_id=session_id,
        data=versions,
    )


@dataclass(frozen=True, slots=True)
class _DebugResources:
    """Journal resources acquired before session assembly."""

    artifact_store: InMemoryArtifactStore | FilesystemArtifactStore | None
    journal: ExecutionJournal | None


@dataclass(frozen=True, slots=True)
class _AudioPipeline:
    """Resolved provider instances and their derived runtime flags."""

    # These remain gradual because the public factory deliberately accepts
    # pre-version_info provider shapes in addition to the full protocols.
    stt: Any
    tts: Any
    vad: Any
    noise_reducer: Any
    echo_canceller: Any
    transport: Any
    auto_turn_from_stt_final: bool
    enable_vad: bool
    enable_echo_cancellation: bool


@dataclass(frozen=True, slots=True)
class _TelephonyPipeline:
    """Lazy telephony result plus the sequence fields consumed by Session."""

    bundle: TelephonyHelpers | None
    helpers: tuple[SessionHelper, ...]
    outbound_state_machine: OutboundCallStateMachine | None
    action_executors: tuple[SessionActionExecutor, ...]


@dataclass(frozen=True, slots=True)
class _BuiltAudioSession:
    """Session plus collaborators needed by post-construction wiring."""

    session: Session
    event_bus: EventBus
    telephony: _TelephonyPipeline


def _create_debug_resources(
    config: _AgentSessionConfig,
    session_id: str,
) -> _DebugResources:
    artifact_store = _create_artifact_store(
        session_id,
        config.debug,
        data_dir=config.data_dir,
    )
    if config.debug == "off":
        return _DebugResources(artifact_store=artifact_store, journal=None)
    try:
        journal = create_journal(
            session_id,
            debug=config.debug,
            backend=config.journal_backend,
            capacity=config.journal_capacity,
            redaction=config.journal_redaction,
            artifact_store=(
                artifact_store if isinstance(artifact_store, InMemoryArtifactStore) else None
            ),
            retention_mode=config.journal_retention,
            data_dir=config.data_dir,
        )
    except BaseException:
        _close_factory_resource(artifact_store)
        raise
    return _DebugResources(artifact_store=artifact_store, journal=journal)


def _close_factory_resource(resource: Any) -> None:
    """Close a partially built resource without masking the factory failure."""
    try:
        close = getattr(resource, "close", None)
        if callable(close) and not inspect.iscoroutinefunction(close):
            close()
    except BaseException:
        logger.warning(
            "Factory rollback cleanup failed for %s",
            type(resource).__name__,
            exc_info=True,
        )


def _register_close(rollback: ExitStack, resource: Any) -> Any:
    """Register a synchronous close hook for factory rollback.

    Session construction is synchronous, so rollback can only run immediate
    closers. Async provider shutdown remains owned by ``Session.stop()`` once
    a session has been assembled.
    """
    close = getattr(resource, "close", None)
    if callable(close) and not inspect.iscoroutinefunction(close):
        rollback.callback(_close_factory_resource, resource)
    return resource


def _resolve_audio_pipeline(
    config: EasyConfig,
    event_bus: EventBus,
) -> _AudioPipeline:
    with ExitStack() as rollback:
        stt = _create_stt(config.stt, event_bus)
        tts = _create_tts(config.tts, event_bus)
        auto_turn_from_stt_final = _should_auto_turn_from_stt_final(config)
        enable_vad = not auto_turn_from_stt_final
        vad_config_or_provider = (
            config.vad
            if _is_vad_provider_instance(config.vad)
            else inject_event_bus(config.vad, event_bus)
        )
        vad = (
            _register_close(rollback, _create_vad(vad_config_or_provider)) if enable_vad else None
        )
        noise_config_or_provider = (
            config.noise_reduction
            if _is_noise_reducer_instance(config.noise_reduction)
            else inject_event_bus(config.noise_reduction, event_bus)
        )
        noise_reducer = (
            _resolve_noise_reducer(noise_config_or_provider or NoiseReducerConfig())
            if config.enable_noise_reduction or config.noise_reduction is not None
            else None
        )
        if noise_reducer is not None:
            _register_close(rollback, noise_reducer)
        # EasyConfig fills this default while preserving pre-built providers.
        echo_config_or_provider = (
            config.echo_cancellation
            if _is_echo_canceller_instance(config.echo_cancellation)
            else inject_event_bus(config.echo_cancellation, event_bus)
        )
        assert echo_config_or_provider is not None
        echo_canceller = _register_close(
            rollback,
            _resolve_echo_canceller(echo_config_or_provider),
        )
        enable_echo_cancellation = (
            echo_config_or_provider.enabled
            if isinstance(echo_config_or_provider, EchoCancellationConfig)
            else False
        )
        pipeline = _AudioPipeline(
            stt=stt,
            tts=tts,
            vad=vad,
            noise_reducer=noise_reducer,
            echo_canceller=echo_canceller,
            transport=_create_transport(config.transport, event_bus),
            auto_turn_from_stt_final=auto_turn_from_stt_final,
            enable_vad=enable_vad,
            enable_echo_cancellation=enable_echo_cancellation,
        )
        rollback.pop_all()
        return pipeline


def _resolve_agent(
    config: _AgentSessionConfig,
    mcp_servers: tuple[str, ...],
    *,
    default_agent: Any | None = None,
) -> Any | None:
    """Resolve shared agent settings while preserving caller-specific absence.

    Audio construction leaves ``default_agent`` as ``None``. Text construction
    supplies ``NoopAgent()`` so its historical echo fallback is still wrapped
    according to ``wrap_agent``. Runtime settings and shape validation apply
    only to an explicitly configured agent, matching both factories' previous
    behavior.
    """
    configured_agent = config.agent
    if configured_agent is None:
        agent = default_agent
    else:
        agent = auto_adapt_agent(configured_agent, model=config.agent_model)
        _inject_agent_runtime(
            agent,
            mcp_servers=mcp_servers,
            agent_model=config.agent_model,
            remote_agent_api_key=config.remote_agent_api_key,
        )
        _validate_agent_shape(agent, wrap_agent=config.wrap_agent)
    if agent is not None and config.wrap_agent and not isinstance(agent, AgentRunner):
        agent = AgentRunner(agent, config.agent_runner or AgentRunnerConfig())
    return agent


def _resolve_turn_config(config: EasyConfig) -> TurnManagerConfig:
    turn_config = config.turn_taking
    smart_turn_config = _normalized_smart_turn(config)
    smart_turn = create_smart_turn(smart_turn_config)
    if smart_turn is None:
        return turn_config
    turn_config = replace(turn_config, endpoint_detector=smart_turn)
    if turn_config.endpoint_threshold is None:
        return replace(turn_config, endpoint_threshold=smart_turn_config.threshold)
    if turn_config.endpoint_threshold != smart_turn_config.threshold:
        logger.warning(
            "Both turn_taking.endpoint_threshold (%.3f) and "
            "smart_turn.threshold (%.3f) are set to different values; "
            "the manager-level endpoint_threshold wins and the provider "
            "threshold is ignored. Set only one to avoid confusion.",
            turn_config.endpoint_threshold,
            smart_turn_config.threshold,
        )
    return turn_config


def _resolve_telephony(config: EasyConfig, event_bus: EventBus) -> _TelephonyPipeline:
    if config.telephony is None:
        return _TelephonyPipeline(
            bundle=None,
            helpers=(),
            outbound_state_machine=None,
            action_executors=tuple(config.action_executors),
        )
    from easycat.config import _telephony_wiring

    bundle = _telephony_wiring.create_telephony_helpers(
        event_bus,
        config.telephony,
        dnc_list=config.dnc_list,
    )
    return _TelephonyPipeline(
        bundle=bundle,
        helpers=tuple(bundle.helpers),
        outbound_state_machine=bundle.state_machine,
        action_executors=(
            *config.action_executors,
            *_telephony_wiring.create_action_executors(config.telephony),
        ),
    )


def _audio_gate_for(
    state_machine: OutboundCallStateMachine | None,
) -> Callable[[], bool] | None:
    if state_machine is None:
        return None

    def audio_gate() -> bool:
        return bool(state_machine.gate.is_buffering)

    return audio_gate


def _make_session_config(
    config: EasyConfig,
    session_id: str,
    debug: _DebugResources,
    audio: _AudioPipeline,
    telephony: _TelephonyPipeline,
    event_bus: EventBus,
    agent: Any | None,
    mcp_servers: tuple[str, ...],
) -> SessionConfig:
    return SessionConfig(
        stt=audio.stt,
        tts=audio.tts,
        vad=audio.vad,
        noise_reducer=audio.noise_reducer,
        echo_canceller=audio.echo_canceller,
        transport=audio.transport,
        agent=agent,
        event_bus=event_bus,
        turn_manager_config=_resolve_turn_config(config),
        timeout_config=config.timeouts,
        journal=debug.journal,
        artifact_store=debug.artifact_store,
        journal_detail=config.debug,
        journal_redaction=config.journal_redaction,
        warmup=config.warmup,
        record_to=config.record_to,
        session_id=session_id,
        telephony_helpers=telephony.helpers,
        enable_vad=audio.enable_vad,
        enable_noise_reduction=config.enable_noise_reduction,
        enable_echo_cancellation=audio.enable_echo_cancellation,
        capture_aec_reference=config.capture_aec_reference,
        capture_audio=config.capture_audio,
        auto_turn_from_stt_final=audio.auto_turn_from_stt_final,
        strip_markdown=config.strip_markdown,
        output_processors=config.output_processors,
        on_agent_failure=config.on_agent_failure,
        session_actions=config.session_actions,
        action_executors=telephony.action_executors,
        audio_gate=_audio_gate_for(telephony.outbound_state_machine),
        mcp_servers=mcp_servers,
        caller_id_exposure=config.caller_id_exposure,
        greeting=config.greeting,
        dnc_list=config.dnc_list,
    )


def _bind_transport_identity(session: Session, transport: Any) -> None:
    def on_identity(identity: Any) -> None:
        session.call_identity = _merge_twilio_identity(
            session._caller_id.private_identity, identity
        )

    bind_identity_sink_if_supported(transport, on_identity)


def _build_audio_session(
    config: EasyConfig,
    session_id: str,
    debug: _DebugResources,
    rollback: ExitStack,
) -> _BuiltAudioSession:
    event_bus = EventBus(
        slow_handler_threshold_s=config.slow_handler_threshold_s,
        handler_error_policy=config.handler_error_policy,
    )
    audio = _resolve_audio_pipeline(config, event_bus)
    for resource in (audio.vad, audio.noise_reducer, audio.echo_canceller):
        if resource is not None:
            _register_close(rollback, resource)
    mcp_servers = tuple(config.mcp_servers) if config.mcp_servers else ()
    agent = _resolve_agent(config, mcp_servers)
    if debug.journal is not None:
        _emit_provider_versions(
            debug.journal,
            session_id,
            stt=audio.stt,
            tts=audio.tts,
            transport=audio.transport,
            vad=audio.vad,
            noise_reducer=audio.noise_reducer,
            echo_canceller=audio.echo_canceller,
            agent=agent,
        )
    telephony = _resolve_telephony(config, event_bus)
    session_config = _make_session_config(
        config,
        session_id,
        debug,
        audio,
        telephony,
        event_bus,
        agent,
        mcp_servers,
    )
    session = Session(session_config)
    _bind_transport_identity(session, audio.transport)
    return _BuiltAudioSession(
        session=session,
        event_bus=event_bus,
        telephony=telephony,
    )


def _subscribe_outbound_identity(session: Session) -> None:
    from easycat.events import CallInitiated
    from easycat.session._types import CallIdentity

    def on_call_initiated(event: CallInitiated) -> None:
        identity = session._caller_id.private_identity
        if identity is not None and identity.direction == "inbound":
            return
        session.call_identity = CallIdentity(
            caller_number=event.to,
            called_number=event.from_,
            direction="outbound",
            call_sid=event.call_sid,
        )

    session._subscribe_owned(CallInitiated, on_call_initiated)


def _wire_outbound_pipeline(built: _BuiltAudioSession) -> None:
    telephony = built.telephony
    if telephony.outbound_state_machine is None or telephony.bundle is None:
        return
    from easycat.config import _telephony_wiring

    _telephony_wiring.wire_outbound_pipeline(
        built.session,
        telephony.bundle,
        built.event_bus,
    )


def _maybe_launch_debugger(config: EasyConfig, session: Session) -> None:
    if config.debug != "full":
        return
    from easycat.debugger._autolaunch import maybe_launch_debugger_ui

    maybe_launch_debugger_ui(
        session,
        config_opt_in=config.debugger_autolaunch,
    )


def _maybe_arm_dev_session(session: Session) -> None:
    import os
    import sys

    from easycat._env import is_truthy

    if not (is_truthy(os.getenv("EASYCAT_DEV")) or "easycat.debugger.dev" in sys.modules):
        return
    from easycat.debugger.dev import arm_dev_session

    arm_dev_session(session)


def _finalize_audio_session(config: EasyConfig, built: _BuiltAudioSession) -> None:
    session = built.session
    session._easycat_config = _safe_config_ns(config)
    session._data_dir = config.data_dir
    session._agent_model = config.agent_model
    session._remote_agent_api_key = config.remote_agent_api_key
    _wire_outbound_pipeline(built)
    _subscribe_outbound_identity(session)
    _maybe_launch_debugger(config, session)
    _maybe_arm_dev_session(session)
    if config.debug != "off" and _emergency_export_enabled(config):
        install_emergency_export(session)


def create_session(config: EasyConfig) -> Session:
    """Create a fully wired :class:`Session` from an :class:`EasyConfig`.

    Resolves every pipeline piece declared on ``config`` — STT/TTS (shortcut
    strings, provider config dataclasses, or live provider instances), VAD,
    noise reduction, echo cancellation, transport, turn-taking, the agent
    bridge (plain ``async run(text) -> str`` agents are wrapped in
    ``AgentRunner`` unless ``wrap_agent=False``), telephony helpers, and the
    execution journal (created when ``debug != "off"``) — then builds the
    Session with every collaborator subscribed to a shared ``EventBus``.

    The returned session is **not started**: subscribe events, attach a
    debugger, or register helpers first, then ``await session.start()`` or
    hand it to ``easycat.helpers.run_session``. See
    ``docs/reference/easyconfig.md`` for the field reference and
    ``docs/reference/session-lifecycle.md`` for start/stop semantics.

    Raises ``EasyConfigError`` for invalid app configuration and an
    :class:`easycat.errors.EasyCatError` when a selected
    provider's credentials or optional extra are missing.
    """
    # Config dataclasses are mutable for backward compatibility. Revalidate at
    # the build boundary before journals, provider clients, or transports are
    # allocated, and surface a high-level missing-agent error here rather than
    # a low-level SessionConfig noop-provider failure after partial wiring.
    runtime_config = _audio_runtime_config(config)
    runtime_config._validate_for_session()
    session_id = runtime_config.session_id or f"session-{uuid4().hex[:12]}"
    debug = _create_debug_resources(runtime_config, session_id)
    with ExitStack() as rollback:
        _register_close(rollback, debug.artifact_store)
        _register_close(rollback, debug.journal)
        built = _build_audio_session(runtime_config, session_id, debug, rollback)
        _finalize_audio_session(runtime_config, built)
        rollback.pop_all()
        return built.session


def _emergency_export_enabled(config: Any) -> bool:
    """Whether opt-in emergency export should be armed for *config*.

    Opt-in only: armed when ``EASYCAT_EMERGENCY_EXPORT`` is truthy in the
    environment, or the ``emergency_export`` config knob is truthy.
    Defaults off so a normal process never installs ``atexit`` / excepthook
    hooks.
    """
    import os

    from easycat._env import is_truthy

    if is_truthy(os.environ.get("EASYCAT_EMERGENCY_EXPORT")):
        return True
    return bool(getattr(config, "emergency_export", False))


# Module-level registry shared by every armed session. Chaining many
# sessions through per-session ``sys.excepthook`` closures was un-restorable:
# the second armed session orphaned the first's hook forever. Instead we keep
# ONE installed excepthook + atexit hook for the whole process and fan out to
# the registered exporters; arming adds an exporter and disarming removes just
# that one, restoring the original ``sys.excepthook``/atexit state only when
# the registry drains. ``_EXPORT_REGISTRY`` preserves insertion order so
# exporters fire oldest-first on a crash.
_EXPORT_REGISTRY: dict[int, Callable[[], None]] = {}
_EXPORT_INSTALLED = False
_EXPORT_PREVIOUS_EXCEPTHOOK: Callable[..., None] | None = None
_EXPORT_EXCEPTHOOK: Callable[..., None] | None = None


def _run_all_exporters() -> None:
    """Fire every registered exporter best-effort (one bad one can't block the rest)."""
    for export in list(_EXPORT_REGISTRY.values()):
        try:
            export()
        except Exception:
            logger.warning("Emergency debug-bundle export failed", exc_info=True)


def _make_excepthook(previous: Callable[..., None] | None) -> Callable[..., None]:
    """Create one generation of the emergency-export excepthook.

    A fresh function per install keeps third-party hooks that captured an older
    EasyCat hook from re-entering the current hook chain after a later reinstall.
    """

    def _easycat_excepthook(exc_type: Any, exc_value: Any, exc_tb: Any) -> None:
        _run_all_exporters()
        if previous is not None:
            previous(exc_type, exc_value, exc_tb)

    return _easycat_excepthook


def _install_shared_hooks() -> None:
    """Install the single process-wide excepthook + atexit hook (idempotent)."""
    global _EXPORT_EXCEPTHOOK, _EXPORT_INSTALLED, _EXPORT_PREVIOUS_EXCEPTHOOK
    import atexit
    import sys

    if _EXPORT_INSTALLED:
        return
    _EXPORT_PREVIOUS_EXCEPTHOOK = sys.excepthook
    _EXPORT_EXCEPTHOOK = _make_excepthook(_EXPORT_PREVIOUS_EXCEPTHOOK)
    sys.excepthook = _EXPORT_EXCEPTHOOK
    atexit.register(_run_all_exporters)
    _EXPORT_INSTALLED = True


def _uninstall_shared_hooks() -> None:
    """Remove the shared hooks and restore the original excepthook (idempotent).

    Only restores ``sys.excepthook`` when ours is still the top hook; if a
    later caller chained on top we leave their hook intact rather than dropping
    it. ``atexit.unregister`` is always safe to call.
    """
    global _EXPORT_EXCEPTHOOK, _EXPORT_INSTALLED, _EXPORT_PREVIOUS_EXCEPTHOOK
    import atexit
    import sys

    if not _EXPORT_INSTALLED:
        return
    try:
        atexit.unregister(_run_all_exporters)
    except Exception:  # noqa: BLE001, S110 intentional boundary or best-effort cleanup
        pass
    if sys.excepthook is _EXPORT_EXCEPTHOOK:
        sys.excepthook = _EXPORT_PREVIOUS_EXCEPTHOOK or sys.__excepthook__
    _EXPORT_PREVIOUS_EXCEPTHOOK = None
    _EXPORT_EXCEPTHOOK = None
    _EXPORT_INSTALLED = False


def install_emergency_export(session: Session) -> Callable[[], None]:
    """Arm a best-effort debug-bundle export for an abnormal process exit.

    Adds this session's exporter to a process-wide registry behind a SINGLE
    installed ``sys.excepthook`` + ``atexit`` hook shared across every armed
    session, so a crash (unhandled exception) or an unexpected interpreter
    shutdown flushes a redacted debug bundle next to the session's data dir
    before the process dies.

    Returns an idempotent *unregister* callable that removes only this
    session's exporter; the shared hook is uninstalled and the original
    ``sys.excepthook``/atexit state restored only once the registry drains.
    The unregister is also stored on ``session._emergency_export_unregister``
    and invoked directly by :meth:`Session.stop` after clean teardown; the
    export body retains a stopped-session fallback for defensive cleanup.

    Strictly opt-in (see :func:`_emergency_export_enabled`): callers must
    explicitly arm it. Never raises; the export is wrapped best-effort.
    """
    import os
    from datetime import UTC, datetime

    key = id(session)
    state = {"done": False, "unregistered": False}

    def _export() -> None:
        if state["done"]:
            return
        # If the session already stopped cleanly, the normal teardown path
        # (record_to / explicit export) owns the bundle — stay out of it and
        # drop this exporter so the registry can eventually drain.
        if getattr(session, "_closed", False):
            _unregister()
            return
        state["done"] = True
        try:
            data_dir = getattr(session, "_data_dir", None) or os.environ.get(
                "EASYCAT_DATA_DIR", ".easycat"
            )
            crash_dir = Path(data_dir) / "crash-dumps"
            crash_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = crash_dir / f"{session.session_id}-emergency-{stamp}.zip"
            session.export_debug_bundle(str(path))
            logger.info("Emergency debug bundle exported to %s", path)
        except Exception:
            logger.warning("Emergency debug-bundle export failed", exc_info=True)

    def _unregister() -> None:
        if state["unregistered"]:
            return
        state["unregistered"] = True
        _EXPORT_REGISTRY.pop(key, None)
        if not _EXPORT_REGISTRY:
            _uninstall_shared_hooks()

    _install_shared_hooks()
    _EXPORT_REGISTRY[key] = _export
    session._emergency_export_unregister = _unregister
    return _unregister


def create_text_session(
    config: TextSessionConfig | None = None,
    *,
    agent: Any = None,
    session_id: str | None = None,
    debug: Literal["off", "light", "full"] = "light",
    slow_handler_threshold_s: float | None = 0.005,
    handler_error_policy: Literal["continue", "raise"] = "continue",
    journal_backend: Literal["sqlite", "sqlite+litestream", "libsql"] = "sqlite",
    journal_capacity: int = 10_000,
    journal_redaction: Literal["secrets", "pii"] = "secrets",
    journal_retention: Literal["archive", "delete"] = "archive",
    warmup: bool | None = None,
    wrap_agent: bool = True,
    agent_runner: AgentRunnerConfig | None = None,
    agent_model: str | None = None,
    remote_agent_api_key: str | None = None,
    mcp_servers: list[str] | None = None,
    record_to: str | Path | None = None,
    capture_audio: bool | Callable[[], bool] = True,
    data_dir: str | Path | None = None,
    emergency_export: bool = False,
) -> Session:
    """Create a text-only Session (no audio pipeline).

    Accepts a :class:`TextSessionConfig` (the ``create_*(config)`` shape
    shared with :func:`create_session`) or, for back-compat, the legacy
    loose keyword arguments. The two forms are mutually exclusive: passing
    a ``config`` together with any non-default loose keyword raises
    :class:`EasyConfigError`.

    The returned session supports :meth:`Session.send_text` for
    request/response agent interaction without STT, TTS, VAD, or
    transport.  Useful for testing agent logic and building text-based
    UIs on the same agent adapter stack.

    ``record_to=`` mirrors :class:`EasyConfig`: with ``debug="light"`` or
    ``debug="full"``, teardown auto-exports a timestamped debug bundle into
    that directory.

    Raises :class:`RuntimeError` if the caller attempts to call
    :meth:`Session.start` on a text session.
    """
    config = TextSessionConfig.from_kwargs(
        config,
        agent=agent,
        session_id=session_id,
        debug=debug,
        slow_handler_threshold_s=slow_handler_threshold_s,
        handler_error_policy=handler_error_policy,
        journal_backend=journal_backend,
        journal_capacity=journal_capacity,
        journal_redaction=journal_redaction,
        journal_retention=journal_retention,
        warmup=warmup,
        wrap_agent=wrap_agent,
        agent_runner=agent_runner,
        agent_model=agent_model,
        remote_agent_api_key=remote_agent_api_key,
        mcp_servers=mcp_servers,
        record_to=record_to,
        capture_audio=capture_audio,
        data_dir=data_dir,
        emergency_export=emergency_export,
    )
    config = _text_runtime_config(config)
    config._validate_for_session()

    sid = config.session_id or f"session-{uuid4().hex[:12]}"
    debug_resources = _create_debug_resources(config, sid)
    with ExitStack() as rollback:
        _register_close(rollback, debug_resources.artifact_store)
        _register_close(rollback, debug_resources.journal)
        event_bus = EventBus(
            slow_handler_threshold_s=config.slow_handler_threshold_s,
            handler_error_policy=config.handler_error_policy,
        )
        resolved_mcp_servers = tuple(config.mcp_servers) if config.mcp_servers else ()
        adapted = _resolve_agent(
            config,
            resolved_mcp_servers,
            default_agent=NoopAgent(),
        )
        if debug_resources.journal is not None:
            _emit_provider_versions(debug_resources.journal, sid, agent=adapted)

        # Text sessions use noop providers — validation is skipped because
        # runtime_mode="text_session" never enters the audio pipeline.
        from easycat.stubs import NoopSTT, NoopTransport, NoopTTS, NoopVAD

        session = Session(
            SessionConfig(
                stt=NoopSTT(),
                tts=NoopTTS(),
                vad=NoopVAD(),
                transport=NoopTransport(),
                agent=adapted,
                event_bus=event_bus,
                journal=debug_resources.journal,
                artifact_store=debug_resources.artifact_store,
                journal_detail=config.debug,
                journal_redaction=config.journal_redaction,
                capture_audio=config.capture_audio,
                warmup=config.warmup,
                record_to=config.record_to,
                session_id=sid,
                runtime_mode="text_session",
                mcp_servers=resolved_mcp_servers,
            )
        )
        rollback.pop_all()
    # Stash user-facing settings so debug bundle export can snapshot them
    # instead of serializing live provider instances from SessionConfig.
    from types import SimpleNamespace

    session._easycat_config = SimpleNamespace(
        debug=config.debug,
        journal_backend=config.journal_backend,
        journal_capacity=config.journal_capacity,
        journal_redaction=config.journal_redaction,
        journal_retention=config.journal_retention,
        capture_audio=config.capture_audio,
        warmup=config.warmup,
        record_to=config.record_to,
    )
    session._data_dir = config.data_dir
    session._agent_model = config.agent_model
    session._remote_agent_api_key = config.remote_agent_api_key
    if config.debug != "off" and _emergency_export_enabled(config):
        install_emergency_export(session)
    return session
