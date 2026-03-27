"""Top-level configuration and session factory for EasyCat."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from easycat.agent_runner import AgentRunner, AgentRunnerConfig
from easycat.agents.factory import auto_adapt_agent
from easycat.echo_cancellation import EchoCancellationConfig, create_echo_canceller
from easycat.event_logging import EventLoggingConfig, EventTraceLogger
from easycat.events import EventBus
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
from easycat.telephony.ivr import IVRNavigator
from easycat.telephony.screening import CallScreeningDetector
from easycat.telephony.voicemail import (
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
    callee_language: str = "en"
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""


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
    wrap_agent: bool = True
    strip_markdown: bool = False
    output_processors: Sequence[LLMOutputProcessor] = ()

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
        self._validate()

    def _default_echo_cancellation_for_transport(self) -> EchoCancellationConfig:
        enable_aec = isinstance(
            self.transport,
            (LocalTransportConfig, WebSocketTransportConfig, WebSocketConnectionTransport),
        )
        return EchoCancellationConfig(enabled=enable_aec)

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
        if config.wrap_agent:
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
            return _outbound_sm.gate.is_closed

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

    # Wire gate flush callback to re-enqueue buffered audio on release.
    if _outbound_sm is not None:
        queue = session._outbound_queue

        async def _flush_gated_audio(events: list) -> None:
            for ev in events:
                await queue.put(ev.chunk)

        _outbound_sm._on_gate_flush = _flush_gated_audio

    return session


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
        oc = config.outbound
        # State machine.
        sm = OutboundCallStateMachine(
            event_bus,
            classification_timeout_s=oc.classification_gate_timeout_s,
            max_call_duration_s=oc.max_call_duration_s,
            classification_gate=oc.classification_gate,
            classification_gate_timeout_s=oc.classification_gate_timeout_s,
            classification_gate_hold_audio=oc.classification_gate_hold_audio,
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
            )
            helpers.append(screening)

        # IVR navigator — activated/deactivated by state machine transitions.
        ivr = IVRNavigator(event_bus)
        helpers.append(ivr)

        def _on_state_changed_for_ivr(event: CallStateChanged) -> None:
            if event.new == OutboundCallState.IVR:
                ivr.activate()
            elif event.new in {OutboundCallState.HUMAN, OutboundCallState.ENDED}:
                ivr.deactivate()

        event_bus.subscribe(CallStateChanged, _on_state_changed_for_ivr)

        # Voicemail policy handler.
        helpers.append(VoicemailPolicyHandler(event_bus))

    return helpers


def _create_metrics(config: MetricsConfig | None) -> MetricsCollector | None:
    if not config or not config.enabled:
        return None
    return config.collector or InMemoryMetrics()


def _create_tracer(config: TracingConfig | None) -> Tracer | None:
    if not config or not config.enabled:
        return None
    return Tracer(exporter=config.exporter)
