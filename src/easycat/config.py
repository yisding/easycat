"""Top-level configuration and session factory for EasyCat."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from easycat.agent_runner import AgentRunner, AgentRunnerConfig
from easycat.agents.factory import auto_adapt_agent
from easycat.echo_cancellation import EchoCancellationConfig, create_echo_canceller
from easycat.event_logging import EventLoggingConfig, EventTraceLogger
from easycat.events import CallInitiated, CallScreening, EventBus, TTSAudio
from easycat.llm_output_processing import LLMOutputProcessor
from easycat.metrics import InMemoryMetrics, MetricsCollector
from easycat.noise_reduction import NoiseReducerConfig, create_noise_reducer
from easycat.providers import Transport
from easycat.session import Session, SessionConfig
from easycat.smart_turn import SmartTurnConfig, create_smart_turn
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.stt.factory import STTConfig, create_stt_provider_from_config
from easycat.stt.openai_provider import OpenAISTTConfig
from easycat.stubs import NoopAgent
from easycat.telephony.call_state import (
    CallStateChanged,
    OutboundCallState,
    OutboundCallStateMachine,
)
from easycat.telephony.dtmf import DTMFAggregator, DTMFAggregatorConfig
from easycat.telephony.ivr import (
    AgentCallback,
    DTMFDelivery,
    IVRAction,
    IVRActionType,
    IVRNavigator,
)
from easycat.telephony.outbound import OutboundCallManager
from easycat.telephony.screening import (
    CallScreeningDetector,
    ScreeningResponse,
    screening_patterns_for_languages,
)
from easycat.telephony.voicemail import (
    PostScreeningVoicemailDetector,
    STTAMDFusionClassifier,
    VoicemailDetector,
    VoicemailDetectorConfig,
    VoicemailPolicyHandler,
)
from easycat.timeouts import TimeoutConfig
from easycat.tracing import TraceExporter, Tracer
from easycat.transports.local import LocalTransport, LocalTransportConfig
from easycat.transports.twilio_media import TwilioTransport, TwilioTransportConfig
from easycat.transports.webrtc import WebRTCTransport, WebRTCTransportConfig
from easycat.transports.websocket import (
    WebSocketConnectionTransport,
    WebSocketTransport,
    WebSocketTransportConfig,
)
from easycat.tts.factory import TTSConfig, create_tts_provider_from_config
from easycat.tts.openai_tts import OpenAITTSConfig
from easycat.turn_manager import TurnManagerConfig, TurnMode
from easycat.vad import VADConfig, create_vad

logger = logging.getLogger(__name__)


@dataclass
class MetricsConfig:
    """Configuration for metrics collection."""

    enabled: bool = False
    collector: MetricsCollector | None = None


@dataclass
class TracingConfig:
    """Configuration for tracing."""

    enabled: bool = False
    exporter: TraceExporter | None = None


@dataclass
class OutboundCallConfig:
    """Configuration for outbound call manager."""

    from_number: str = ""
    amd_mode: str = "DetectMessageEnd"
    async_amd: bool = True
    amd_timeout: int = 30
    speech_threshold: int = 2400
    speech_end_threshold: int = 1200
    silence_timeout: int = 5000
    enable_screening_detection: bool = True
    screening_response: str = ""
    screening_use_agent: bool = False
    max_screening_turns: int = 3
    enable_realtime_transcription: bool = True
    classification_gate: bool = True
    classification_gate_timeout_s: float = 5.0
    classification_gate_hold_audio: str = ""
    max_call_duration_s: int = 300
    late_voicemail_window_s: float = 30.0
    voicemail_pickup_window_s: float = 60.0
    callee_language: str = "en"
    twilio_account_sid: str = field(default="", repr=False)
    twilio_auth_token: str = field(default="", repr=False)
    twiml_url: str = ""
    status_callback_url: str = ""
    ivr_agent_callback: AgentCallback | None = None
    ivr_dtmf_delivery: DTMFDelivery | None = None

    def __post_init__(self) -> None:
        if self.classification_gate_timeout_s <= 0:
            raise ValueError("classification_gate_timeout_s must be positive")
        if self.max_call_duration_s <= 0:
            raise ValueError("max_call_duration_s must be positive")


@dataclass
class TelephonyConfig:
    """Configuration for telephony helpers."""

    enable_dtmf_aggregator: bool = False
    enable_voicemail_detector: bool = False
    enable_outbound_call_manager: bool = False
    dtmf_aggregator: DTMFAggregatorConfig = field(default_factory=DTMFAggregatorConfig)
    voicemail_detector: VoicemailDetectorConfig = field(default_factory=VoicemailDetectorConfig)
    outbound: OutboundCallConfig | None = None


TransportConfig = (
    LocalTransportConfig
    | WebSocketTransportConfig
    | TwilioTransportConfig
    | WebRTCTransportConfig
    | Transport
)
_TRANSPORT_FACTORIES: dict[type[TransportConfig], Any] = {
    LocalTransportConfig: lambda config, event_bus: LocalTransport(config),
    WebSocketTransportConfig: lambda config, event_bus: WebSocketTransport(config),
    TwilioTransportConfig: lambda config, event_bus: TwilioTransport(
        config=config, event_bus=event_bus
    ),
    WebRTCTransportConfig: lambda config, event_bus: WebRTCTransport(config),
}


@dataclass
class EasyCatConfig:
    """Top-level configuration for EasyCat sessions."""

    openai_api_key: str | None = None
    stt: STTConfig | None = None
    tts: TTSConfig | None = None
    vad: VADConfig = field(default_factory=VADConfig)
    noise_reduction: NoiseReducerConfig = field(default_factory=NoiseReducerConfig)
    echo_cancellation: EchoCancellationConfig | None = None
    transport: TransportConfig = field(default_factory=LocalTransportConfig)
    turn_taking: TurnManagerConfig = field(default_factory=TurnManagerConfig)
    smart_turn: SmartTurnConfig = field(default_factory=SmartTurnConfig)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    telephony: TelephonyConfig | None = None
    event_logging: EventLoggingConfig = field(default_factory=EventLoggingConfig)
    metrics: MetricsConfig | None = None
    tracing: TracingConfig | None = None
    agent: Any = None
    agent_runner: AgentRunnerConfig | None = None
    strip_markdown: bool = False
    output_processors: Sequence[LLMOutputProcessor] = ()
    debug: bool = False

    def __post_init__(self) -> None:
        if self.openai_api_key:
            if self.stt is None:
                self.stt = OpenAISTTConfig(api_key=self.openai_api_key)
            if self.tts is None:
                # Match the TTS output format to the transport's audio format
                # so that TTSBase._normalize_audio resamples correctly
                # (e.g. OpenAI produces 24kHz but LocalTransport plays 16kHz).
                transport_fmt = getattr(self.transport, "audio_format", None)
                tts_kwargs: dict[str, Any] = {"api_key": self.openai_api_key}
                if transport_fmt is not None:
                    tts_kwargs["output_format"] = transport_fmt
                self.tts = OpenAITTSConfig(**tts_kwargs)
        if self.echo_cancellation is None:
            self.echo_cancellation = self._default_echo_cancellation_for_transport()
        if self.debug:
            self._apply_debug_defaults()
        self._validate()

    def _default_echo_cancellation_for_transport(self) -> EchoCancellationConfig:
        enable_aec = isinstance(
            self.transport,
            (LocalTransportConfig, WebSocketTransportConfig, WebSocketConnectionTransport),
        )
        return EchoCancellationConfig(enabled=enable_aec)

    def _apply_debug_defaults(self) -> None:
        """Enable verbose logging, event tracing with partials, and metrics."""
        # Ensure log output is visible — add a root handler if none exists.
        if not logging.root.handlers:
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s %(name)s %(levelname)s %(message)s",
            )
        # Set the easycat logger to DEBUG regardless of the root level.
        logging.getLogger("easycat").setLevel(logging.DEBUG)

        # Enable event trace logging with partials so STT progress is visible.
        self.event_logging = EventLoggingConfig(
            enabled=True,
            include_partials=True,
            level=logging.DEBUG,
        )

        # Enable in-memory metrics so latency data is always collected.
        if self.metrics is None:
            self.metrics = MetricsConfig(enabled=True)

        logger.debug("EasyCat debug mode enabled")

    def _validate(self) -> None:
        if self.stt is None:
            raise ValueError("STT configuration is required.")
        if self.tts is None:
            raise ValueError("TTS configuration is required.")
        for cfg in (self.stt, self.tts):
            if hasattr(cfg, "api_key") and not cfg.api_key:
                name = (
                    type(cfg)
                    .__name__.replace("Config", "")
                    .replace("STT", " STT")
                    .replace("TTS", " TTS")
                )
                raise ValueError(f"{name} requires an API key.")


def _should_auto_turn_from_stt_final(config: EasyCatConfig) -> bool:
    """Whether this session should derive turn boundaries from STT finals."""
    if not isinstance(config.stt, DeepgramSTTConfig):
        return False
    if config.turn_taking.mode == TurnMode.PUSH_TO_TALK:
        return False
    if config.smart_turn.enabled:
        return False
    if config.telephony and config.telephony.enable_voicemail_detector:
        return False
    return config.stt.is_flux


def create_session(config: EasyCatConfig) -> Session:
    """Create a fully wired Session from EasyCatConfig."""
    event_bus = EventBus()
    stt = create_stt_provider_from_config(config.stt, event_bus)
    tts = create_tts_provider_from_config(config.tts, event_bus)
    auto_turn_from_stt_final = _should_auto_turn_from_stt_final(config)
    enable_vad = not auto_turn_from_stt_final
    vad = create_vad(config.vad) if enable_vad else None
    noise_reducer = create_noise_reducer(config.noise_reduction)
    echo_canceller = create_echo_canceller(config.echo_cancellation or EchoCancellationConfig())
    transport = _create_transport(config.transport, event_bus)

    if config.agent is not None:
        agent = auto_adapt_agent(config.agent)
        runner_cfg = config.agent_runner or AgentRunnerConfig()
        agent = AgentRunner(agent, runner_cfg)
    else:
        agent = NoopAgent()

    metrics = _create_metrics(config.metrics)
    tracer = _create_tracer(config.tracing)

    turn_config = config.turn_taking
    smart_turn = create_smart_turn(config.smart_turn)
    if smart_turn is not None:
        turn_config = replace(turn_config, endpoint_detector=smart_turn)

    telephony_helpers = _create_telephony_helpers(event_bus, config.telephony)
    if config.event_logging.enabled:
        telephony_helpers.append(EventTraceLogger(event_bus, config.event_logging))

    # Extract audio gate from the outbound call state machine, if present.
    audio_gate = None
    _outbound_sm = None
    for h in telephony_helpers:
        if isinstance(h, OutboundCallStateMachine):
            _outbound_sm = h
            break

    if _outbound_sm is not None:

        def audio_gate() -> bool:
            return _outbound_sm.gate.is_buffering

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
            metrics=metrics,
            tracer=tracer,
            telephony_helpers=telephony_helpers,
            enable_vad=enable_vad,
            auto_turn_from_stt_final=auto_turn_from_stt_final,
            strip_markdown=config.strip_markdown,
            output_processors=config.output_processors,
            audio_gate=audio_gate,
        )
    )

    if _outbound_sm is not None:
        _wire_outbound_pipeline(
            session,
            _outbound_sm,
            telephony_helpers,
            event_bus,
            agent,
        )

    return session


class _OutboundPipelineWiring:
    """Encapsulates mutable state for the outbound pipeline callbacks.

    Replaces bare closures with ``nonlocal`` to avoid unsynchronized
    access to ``_hold_audio_task`` from concurrent async callbacks.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._lock = asyncio.Lock()
        self._hold_audio_task: asyncio.Task[None] | None = None

    async def flush_gated_audio(self, events: list[TTSAudio]) -> None:
        async with self._lock:
            if self._hold_audio_task is not None and not self._hold_audio_task.done():
                self._hold_audio_task.cancel()
                self._hold_audio_task = None
        await self._session.replay_gated_audio(events)

    def play_hold_audio(self, text: str) -> None:
        async def _synthesize_hold() -> None:
            await self._session.synthesize_bypass(text)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running event loop — hold audio skipped")
            return

        # The lock is async-only; since this is a sync callback we just
        # do a best-effort swap — the flush side holds the lock and will
        # cancel whatever task reference it sees.
        self._hold_audio_task = loop.create_task(_synthesize_hold())


def _wire_outbound_pipeline(
    session: Session,
    sm: OutboundCallStateMachine,
    helpers: list[Any],
    event_bus: EventBus,
    agent: Any,
) -> None:
    """Connect the outbound call state machine to the session pipeline.

    Wires the classification gate flush/hold callbacks and the screening
    response handler so that TTS audio is buffered, replayed, and the bot
    responds to screening prompts.
    """
    wiring = _OutboundPipelineWiring(session)

    sm.set_gate_flush_callback(wiring.flush_gated_audio)
    sm.gate.set_hold_audio_callback(wiring.play_hold_audio)

    _screening_detector: CallScreeningDetector | None = None
    for _h in helpers:
        if isinstance(_h, CallScreeningDetector):
            _screening_detector = _h
            break

    async def _on_screening_response(event: ScreeningResponse) -> None:
        if event.mode == "agent" and _screening_detector is not None:
            try:
                prompt = _screening_detector.accumulated_text
                response_text = await agent.run(
                    f"The callee's phone is screening this call. "
                    f'Their screening prompt says: "{prompt}". '
                    f"Identify yourself briefly."
                )
                in_time = _screening_detector.notify_agent_responded()
                fallback_spoken = not in_time and _screening_detector.screening_response
                if response_text and not fallback_spoken:
                    await session.synthesize_bypass(response_text)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Agent-mode screening response failed, using static fallback")
                if _screening_detector.screening_response:
                    await session.synthesize_bypass(_screening_detector.screening_response)
        elif event.text:
            await session.synthesize_bypass(event.text)

    event_bus.subscribe(ScreeningResponse, _on_screening_response)


def _create_transport(config: TransportConfig, event_bus: EventBus) -> Any:
    if isinstance(config, Transport):
        if hasattr(config, "_event_bus") and getattr(config, "_event_bus") is None:
            config._event_bus = event_bus
        return config
    factory = _TRANSPORT_FACTORIES.get(type(config))
    if factory is None:
        raise ValueError("Unsupported transport configuration type.")
    return factory(config, event_bus)


def _create_telephony_helpers(event_bus: EventBus, config: TelephonyConfig | None) -> list[Any]:
    helpers: list[Any] = []
    if config is None:
        return helpers

    if config.enable_dtmf_aggregator:
        helpers.append(DTMFAggregator(event_bus, config.dtmf_aggregator))

    if config.enable_voicemail_detector:
        helpers.append(VoicemailDetector(event_bus, config.voicemail_detector))

    if config.enable_outbound_call_manager and config.outbound:
        _create_outbound_helpers(event_bus, config.outbound, helpers)

    return helpers


def _create_outbound_helpers(
    event_bus: EventBus, oc: OutboundCallConfig, helpers: list[Any]
) -> None:
    """Build and wire the outbound call pipeline helpers."""
    # STT+AMD fusion classifier — must be wired before the state machine
    # so that raw AMD events are intercepted and re-emitted with source="fusion".
    fusion = STTAMDFusionClassifier(event_bus)
    helpers.append(fusion)

    # Post-screening voicemail detector — re-classifies after screening.
    post_screening_vm = PostScreeningVoicemailDetector(event_bus)
    helpers.append(post_screening_vm)

    def _on_screening_for_post_vm(event: CallScreening) -> None:
        post_screening_vm.activate()

    event_bus.subscribe(CallScreening, _on_screening_for_post_vm)

    # Build language-aware screening patterns once so both the state
    # machine and the screening detector share the same set.
    screening_langs = ["en"]
    if oc.callee_language and oc.callee_language != "en":
        screening_langs.append(oc.callee_language)
    _screening_patterns = screening_patterns_for_languages(screening_langs)

    # State machine — expect fused voicemail events (ignore raw AMD).
    sm = OutboundCallStateMachine(
        event_bus,
        classification_timeout_s=float(oc.amd_timeout),
        max_call_duration_s=oc.max_call_duration_s,
        classification_gate=oc.classification_gate,
        classification_gate_timeout_s=oc.classification_gate_timeout_s,
        classification_gate_hold_audio=oc.classification_gate_hold_audio,
        expect_fused_voicemail=True,
        late_voicemail_window_s=oc.late_voicemail_window_s,
        voicemail_pickup_window_s=oc.voicemail_pickup_window_s,
        screening_patterns=_screening_patterns,
    )
    helpers.append(sm)

    # Screening detector.
    if oc.enable_screening_detection:
        screening = CallScreeningDetector(
            event_bus,
            enabled=True,
            screening_response=oc.screening_response,
            screening_use_agent=oc.screening_use_agent,
            max_screening_turns=oc.max_screening_turns,
            patterns=_screening_patterns,
            track_filter=None,
        )
        helpers.append(screening)

    # IVR navigator — only created when an agent callback is configured.
    if oc.ivr_agent_callback is not None:
        ivr_delivery = oc.ivr_dtmf_delivery
        ivr = IVRNavigator(
            event_bus,
            agent_callback=oc.ivr_agent_callback,
            dtmf_delivery=ivr_delivery,
        )
        helpers.append(ivr)

        # Propagate the live call SID so DTMFDelivery can send digits/speech.
        if ivr_delivery is not None:

            async def _on_call_initiated_for_ivr(event: CallInitiated) -> None:
                ivr_delivery.call_sid = event.call_sid

            event_bus.subscribe(CallInitiated, _on_call_initiated_for_ivr)

        def _on_state_changed_for_ivr(event: CallStateChanged) -> None:
            if event.new == OutboundCallState.IVR:
                ivr.activate()
            elif event.new in {OutboundCallState.HUMAN, OutboundCallState.ENDED}:
                ivr.deactivate()

        event_bus.subscribe(CallStateChanged, _on_state_changed_for_ivr)

        # React to IVR navigator actions: human pickup, speech, and hangup.
        async def _on_ivr_action(event: IVRAction) -> None:
            if event.type == IVRActionType.HUMAN_DETECTED:
                if sm.state == OutboundCallState.IVR:
                    await sm.transition(OutboundCallState.HUMAN)
            elif event.type == IVRActionType.HANGUP:
                if sm.state == OutboundCallState.IVR:
                    await sm.transition(OutboundCallState.ENDED)
            elif event.type == IVRActionType.SPEAK:
                if ivr_delivery is not None:
                    await ivr_delivery.send_speech(event.text)

        event_bus.subscribe(IVRAction, _on_ivr_action)

    # Voicemail policy handler.
    helpers.append(VoicemailPolicyHandler(event_bus, expect_fused=True))

    # Outbound call manager (requires Twilio credentials).
    if oc.twilio_account_sid and oc.twilio_auth_token:
        try:
            manager = OutboundCallManager(
                event_bus,
                from_number=oc.from_number,
                amd_mode=oc.amd_mode,
                async_amd=oc.async_amd,
                amd_timeout=oc.amd_timeout,
                speech_threshold=oc.speech_threshold,
                speech_end_threshold=oc.speech_end_threshold,
                silence_timeout=oc.silence_timeout,
                enable_realtime_transcription=oc.enable_realtime_transcription,
                twilio_account_sid=oc.twilio_account_sid,
                twilio_auth_token=oc.twilio_auth_token,
                twiml_url=oc.twiml_url,
                status_callback_url=oc.status_callback_url,
            )
            helpers.append(manager)
        except ImportError:
            logger.warning("twilio package not installed — OutboundCallManager disabled")


def _create_metrics(config: MetricsConfig | None) -> MetricsCollector | None:
    if not config or not config.enabled:
        return None
    return config.collector or InMemoryMetrics()


def _create_tracer(config: TracingConfig | None) -> Tracer | None:
    if not config or not config.enabled:
        return None
    return Tracer(exporter=config.exporter)
