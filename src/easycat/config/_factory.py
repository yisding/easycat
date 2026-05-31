"""Session-building factories: :func:`create_session` / :func:`create_text_session`.

This module owns everything that needs the :class:`Session` class — the two
public factories plus the Session-coupled helpers (transport building, journal
provider-version emission, the ``record_to`` auto-export hook). The
provider-factory names that tests monkeypatch (``create_vad``,
``create_noise_reducer``, …) are bound here at module scope so a
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from easycat.echo_cancellation import create_echo_canceller
from easycat.events import EventBus
from easycat.integrations.agents import ExternalAgentBridge
from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.noise_reduction import NoiseReducerConfig, create_noise_reducer
from easycat.providers import TransportLike
from easycat.runtime.artifacts import FilesystemArtifactStore, InMemoryArtifactStore
from easycat.runtime.capabilities import bind_identity_sink_if_supported
from easycat.runtime.journal import create_journal
from easycat.session._session import Session
from easycat.session._types import Agent as _AgentProto
from easycat.session._types import SessionConfig
from easycat.smart_turn import create_smart_turn
from easycat.stt.factory import create_stt_provider_from_config
from easycat.stubs import NoopAgent
from easycat.transports.local import LocalTransportConfig
from easycat.transports.twilio_media import TwilioTransportConfig
from easycat.transports.webrtc import WebRTCTransportConfig
from easycat.transports.websocket import WebSocketTransportConfig
from easycat.transports.webtransport import WebTransportTransportConfig
from easycat.tts.factory import create_tts_provider_from_config
from easycat.turn_manager import TurnMode
from easycat.vad import create_vad

from .easy import (
    EasyConfig,
    EasyConfigError,
    TextSessionConfig,
    TransportConfig,
    _inject_agent_runtime,
)

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
        if hasattr(config, "_event_bus") and getattr(config, "_event_bus") is None:
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


def _create_artifact_store(
    session_id: str, debug: str
) -> InMemoryArtifactStore | FilesystemArtifactStore | None:
    if debug == "off":
        return None
    if debug == "full":
        return FilesystemArtifactStore(session_id)
    return InMemoryArtifactStore()


def _should_auto_turn_from_stt_final(config: EasyConfig) -> bool:
    """Whether this session should derive turn boundaries from STT finals."""
    from easycat.stt.deepgram_provider import DeepgramSTTConfig

    if not isinstance(config.stt, DeepgramSTTConfig):
        return False
    if config.turn_taking.mode == TurnMode.PUSH_TO_TALK:
        return False
    if config.smart_turn.enabled:
        return False
    if config.telephony and config.telephony.enable_voicemail_detector:
        return False
    return config.stt.is_flux


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
    stt: Any,
    tts: Any,
    transport: Any,
    vad: Any = None,
    noise_reducer: Any = None,
    echo_canceller: Any = None,
) -> None:
    """Write a single journal record with version info from all providers."""
    from easycat.runtime.records import JournalRecordKind

    versions: dict[str, dict[str, str]] = {}
    for role, provider in [
        ("stt", stt),
        ("tts", tts),
        ("transport", transport),
        ("vad", vad),
        ("noise_reducer", noise_reducer),
        ("echo_canceller", echo_canceller),
    ]:
        if provider is not None and hasattr(provider, "version_info"):
            versions[role] = provider.version_info()
    journal.append(
        kind=JournalRecordKind.EVENT,
        name="provider_versions",
        session_id=session_id,
        data=versions,
    )


def create_session(config: EasyConfig) -> Session:
    """Create a fully wired Session from EasyConfig."""
    from dataclasses import replace

    session_id = f"session-{uuid4().hex[:12]}"
    artifact_store = _create_artifact_store(session_id, config.debug)
    journal = (
        create_journal(
            session_id,
            debug=config.debug,
            backend=config.journal_backend,
            artifact_store=(
                artifact_store if isinstance(artifact_store, InMemoryArtifactStore) else None
            ),
            retention_mode=config.journal_retention,
        )
        if config.debug != "off"
        else None
    )

    try:
        event_bus = EventBus()
        stt = create_stt_provider_from_config(config.stt, event_bus)
        tts = create_tts_provider_from_config(config.tts, event_bus)
        auto_turn_from_stt_final = _should_auto_turn_from_stt_final(config)
        enable_vad = not auto_turn_from_stt_final
        vad = create_vad(config.vad) if enable_vad else None
        noise_reducer = (
            create_noise_reducer(config.noise_reduction or NoiseReducerConfig())
            if config.enable_noise_reduction or config.noise_reduction is not None
            else None
        )
        # ``EasyConfig.__post_init__`` always resolves ``echo_cancellation``
        # to a concrete config (honoring the tri-state ``enable_echo_cancellation``
        # via ``_default_echo_cancellation_for_transport``), so it is never None
        # here.
        echo_cfg = config.echo_cancellation
        assert echo_cfg is not None
        echo_canceller = create_echo_canceller(echo_cfg)
        transport = _create_transport(config.transport, event_bus)

        mcp_servers = tuple(config.mcp_servers) if config.mcp_servers else ()

        if config.agent is not None:
            agent = auto_adapt_agent(config.agent, model=config.agent_model)
            _inject_agent_runtime(
                agent,
                mcp_servers=mcp_servers,
                agent_model=config.agent_model,
                remote_agent_api_key=config.remote_agent_api_key,
            )
            _validate_agent_shape(agent, wrap_agent=config.wrap_agent)
            if config.wrap_agent and not isinstance(agent, AgentRunner):
                runner_cfg = config.agent_runner or AgentRunnerConfig()
                agent = AgentRunner(agent, runner_cfg)
        else:
            agent = NoopAgent()

        # Emit provider versions into the journal at session start.
        if journal is not None:
            _emit_provider_versions(
                journal,
                session_id,
                stt=stt,
                tts=tts,
                transport=transport,
                vad=vad,
                noise_reducer=noise_reducer,
                echo_canceller=echo_canceller,
            )

        turn_config = config.turn_taking
        smart_turn = create_smart_turn(config.smart_turn)
        if smart_turn is not None:
            turn_config = replace(turn_config, endpoint_detector=smart_turn)
            # There are two decision knobs for the same endpoint call:
            # ``SmartTurnConfig.threshold`` (used by the provider to compute
            # ``prediction``) and ``TurnManagerConfig.endpoint_threshold``
            # (re-decides on ``probability`` at the manager and wins when set).
            # To stop them diverging silently, when the user has not set an
            # explicit manager threshold (default ``None``), derive it from the
            # provider threshold so the single ``smart_turn.threshold`` knob is
            # authoritative. An explicit ``endpoint_threshold`` still wins, but
            # we warn when it disagrees with the provider threshold so the
            # precedence is never a hidden footgun.
            if turn_config.endpoint_threshold is None:
                turn_config = replace(turn_config, endpoint_threshold=config.smart_turn.threshold)
            elif turn_config.endpoint_threshold != config.smart_turn.threshold:
                logger.warning(
                    "Both turn_taking.endpoint_threshold (%.3f) and "
                    "smart_turn.threshold (%.3f) are set to different values; "
                    "the manager-level endpoint_threshold wins and the provider "
                    "threshold is ignored. Set only one to avoid confusion.",
                    turn_config.endpoint_threshold,
                    config.smart_turn.threshold,
                )

        # Telephony wiring is imported lazily so a non-telephony session never
        # loads the outbound stack (preserving the no-eager-telephony-import
        # property). ``create_telephony_helpers`` returns a typed bundle whose
        # ``state_machine`` / ``screening_detector`` we read by name.
        telephony = None
        if config.telephony is not None:
            from easycat.config import _telephony_wiring

            telephony = _telephony_wiring.create_telephony_helpers(
                event_bus,
                config.telephony,
                dnc_list=config.dnc_list,
            )
            action_executors = [
                *config.action_executors,
                *_telephony_wiring.create_action_executors(config.telephony),
            ]
        else:
            action_executors = [*config.action_executors]

        telephony_helpers = telephony.helpers if telephony is not None else []
        outbound_sm = telephony.state_machine if telephony is not None else None

        # Extract audio gate from the outbound call state machine, if present.
        audio_gate = None
        if outbound_sm is not None:

            def audio_gate() -> bool:
                return outbound_sm.gate.is_buffering

        session = Session(
            SessionConfig(
                stt=stt,
                tts=tts,
                vad=vad,
                noise_reducer=noise_reducer,
                echo_canceller=echo_canceller,
                transport=transport,
                agent=agent,
                event_bus=event_bus,
                turn_manager_config=turn_config,
                timeout_config=config.timeouts,
                journal=journal,
                artifact_store=artifact_store,
                session_id=session_id,
                telephony_helpers=telephony_helpers,
                enable_vad=enable_vad,
                enable_noise_reduction=config.enable_noise_reduction,
                enable_echo_cancellation=echo_cfg.enabled,
                auto_turn_from_stt_final=auto_turn_from_stt_final,
                strip_markdown=config.strip_markdown,
                output_processors=config.output_processors,
                session_actions=config.session_actions,
                action_executors=action_executors,
                audio_gate=audio_gate,
                mcp_servers=mcp_servers,
                caller_id_exposure=config.caller_id_exposure,
                greeting=config.greeting,
                dnc_list=config.dnc_list,
            )
        )

        # Bridge the Twilio start-event customParameters through to
        # ``session.call_identity`` so the agent (or its tools) sees
        # who's calling without every app reimplementing the plumbing.
        def _on_twilio_identity(identity: Any) -> None:
            session.call_identity = _merge_twilio_identity(
                session._caller_id.private_identity, identity
            )

        bind_identity_sink_if_supported(transport, _on_twilio_identity)
    except Exception:
        if journal is not None and hasattr(journal, "close"):
            journal.close()
        raise
    # Stash a lightweight snapshot of user-facing config fields so debug
    # bundle export can serialise settings (debug, journal_backend,
    # turn_taking, etc.) without touching live provider instances.
    # We intentionally avoid ``copy.deepcopy(config)`` because configs
    # may carry non-picklable objects (httpx clients, agent instances).
    session._easycat_config = _safe_config_ns(config)
    session._agent_model = config.agent_model
    session._remote_agent_api_key = config.remote_agent_api_key

    if outbound_sm is not None and telephony is not None:
        from easycat.config import _telephony_wiring

        _telephony_wiring.wire_outbound_pipeline(session, telephony, event_bus)

    # Outbound-call caller identity: when :class:`OutboundCallManager`
    # emits :class:`CallInitiated`, stamp the session with a
    # direction="outbound" identity so the agent/tools know who they're
    # calling without having to peek into the event bus themselves.
    from easycat.events import CallInitiated as _CallInitiatedEv
    from easycat.session._types import CallIdentity as _CallIdentity

    def _on_outbound_initiated(event: _CallInitiatedEv) -> None:
        # Don't clobber an existing inbound identity — a session that
        # places an outbound call while an inbound call is live is
        # unusual but shouldn't silently lose the inbound number.
        identity = session._caller_id.private_identity
        if identity is not None and identity.direction == "inbound":
            return
        session.call_identity = _CallIdentity(
            caller_number=event.to,
            called_number=event.from_,
            direction="outbound",
            call_sid=event.call_sid,
        )

    event_bus.subscribe(_CallInitiatedEv, _on_outbound_initiated)

    if config.record_to is not None:
        _install_record_to_hook(session, Path(config.record_to), debug_mode=config.debug)

    if config.debug == "full":
        from easycat.debugger._autolaunch import maybe_launch_debugger_ui

        maybe_launch_debugger_ui(session)

    return session


def _install_record_to_hook(
    session: Session,
    record_to: Path,
    *,
    debug_mode: Literal["off", "light", "full"],
) -> None:
    """Wire session stop/shutdown to auto-export a timestamped bundle.

    ``record_to`` is a no-op when ``debug="off"`` because the journal
    isn't created in that mode — we warn once and skip rather than
    silently export an empty bundle.  The export runs *after* the
    normal shutdown completes (so the journal already has the final
    records) and is wrapped in a broad try/except so a bundle-write
    failure never masks the real shutdown outcome.
    """
    if debug_mode == "off":
        logger.warning(
            "EasyConfig(record_to=%r) requested but debug='off' — no journal will "
            "be captured. Set debug='light' or 'full' to enable recording.",
            str(record_to),
        )
        return

    original_stop = session.stop

    async def _export_bundle() -> None:
        try:
            record_to.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = record_to / f"{session.session_id}-{stamp}.zip"
            session.export_debug_bundle(str(path))
            logger.info("Recorded debug bundle to %s", path)
        except Exception:
            logger.exception("Failed to record debug bundle to %s", record_to)

    async def _wrapped_stop(*, force: bool = False) -> None:
        try:
            await original_stop(force=force)
        finally:
            await _export_bundle()

    # ``shutdown()`` already delegates to ``stop(force=True)``, so wrapping
    # ``stop`` alone covers every teardown path (``stop``, ``shutdown``, and
    # the ``async with`` exit). The wrapper preserves the keyword-only
    # ``force`` parameter so the graceful-vs-force distinction survives.
    session.stop = _wrapped_stop  # type: ignore[method-assign]


def create_text_session(
    config: TextSessionConfig | None = None,
    *,
    agent: Any = None,
    session_id: str | None = None,
    debug: Literal["off", "light", "full"] = "off",
    journal_backend: Literal["sqlite", "sqlite+litestream", "libsql"] = "sqlite",
    journal_retention: Literal["archive", "delete"] = "archive",
    wrap_agent: bool = True,
    agent_runner: AgentRunnerConfig | None = None,
    agent_model: str | None = None,
    remote_agent_api_key: str | None = None,
    mcp_servers: list[str] | None = None,
) -> Session:
    """Create a text-only Session (no audio pipeline).

    Accepts a :class:`TextSessionConfig` (the ``create_*(config)`` shape
    shared with :func:`create_session`) or, for back-compat, the legacy
    loose keyword arguments. The two forms are mutually exclusive: passing
    a ``config`` together with any non-default loose keyword raises
    :class:`ValueError`.

    The returned session supports :meth:`Session.send_text` for
    request/response agent interaction without STT, TTS, VAD, or
    transport.  Useful for testing agent logic and building text-based
    UIs on the same agent adapter stack.

    Raises :class:`RuntimeError` if the caller attempts to call
    :meth:`Session.start` on a text session.
    """
    config = TextSessionConfig.from_kwargs(
        config,
        agent=agent,
        session_id=session_id,
        debug=debug,
        journal_backend=journal_backend,
        journal_retention=journal_retention,
        wrap_agent=wrap_agent,
        agent_runner=agent_runner,
        agent_model=agent_model,
        remote_agent_api_key=remote_agent_api_key,
        mcp_servers=mcp_servers,
    )

    agent = config.agent
    session_id = config.session_id
    debug = config.debug
    journal_backend = config.journal_backend
    journal_retention = config.journal_retention
    wrap_agent = config.wrap_agent
    agent_runner = config.agent_runner
    agent_model = config.agent_model
    remote_agent_api_key = config.remote_agent_api_key
    mcp_servers = config.mcp_servers

    sid = session_id or f"session-{uuid4().hex[:12]}"
    artifact_store = _create_artifact_store(sid, debug)
    journal = (
        create_journal(
            sid,
            debug=debug,
            backend=journal_backend,
            artifact_store=(
                artifact_store if isinstance(artifact_store, InMemoryArtifactStore) else None
            ),
            retention_mode=journal_retention,
        )
        if debug != "off"
        else None
    )
    try:
        event_bus = EventBus()

        adapted = auto_adapt_agent(agent, model=agent_model) if agent is not None else NoopAgent()
        _mcp = list(mcp_servers) if mcp_servers else []
        if agent is not None:
            _inject_agent_runtime(
                adapted,
                mcp_servers=_mcp,
                agent_model=agent_model,
                remote_agent_api_key=remote_agent_api_key,
            )
            _validate_agent_shape(adapted, wrap_agent=wrap_agent)
        if wrap_agent and not isinstance(adapted, AgentRunner):
            runner_cfg = agent_runner or AgentRunnerConfig()
            adapted = AgentRunner(adapted, runner_cfg)

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
                journal=journal,
                artifact_store=artifact_store,
                session_id=sid,
                runtime_mode="text_session",
                mcp_servers=tuple(_mcp),
            )
        )
    except Exception:
        if journal is not None and hasattr(journal, "close"):
            journal.close()
        raise
    # Stash user-facing settings so debug bundle export can snapshot them
    # instead of serializing live provider instances from SessionConfig.
    from types import SimpleNamespace

    session._easycat_config = SimpleNamespace(
        debug=debug,
        journal_backend=journal_backend,
        journal_retention=journal_retention,
    )
    session._agent_model = agent_model
    session._remote_agent_api_key = remote_agent_api_key
    return session
