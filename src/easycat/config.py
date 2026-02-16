"""Top-level configuration and session factory for EasyCat."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from easycat.agent_runner import AgentRunner, AgentRunnerConfig
from easycat.events import EventBus
from easycat.metrics import InMemoryMetrics, MetricsCollector
from easycat.noise_reduction import NoiseReducerConfig, create_noise_reducer
from easycat.session import Session, SessionConfig
from easycat.smart_turn import SmartTurnConfig, create_smart_turn
from easycat.stt.deepgram_provider import DeepgramSTT, DeepgramSTTConfig
from easycat.stt.elevenlabs_provider import ElevenLabsSTT, ElevenLabsSTTConfig
from easycat.stt.openai_provider import OpenAISTT, OpenAISTTConfig
from easycat.stubs import NoopAgent
from easycat.telephony.dtmf import DTMFAggregator, DTMFAggregatorConfig
from easycat.telephony.voicemail import VoicemailDetector, VoicemailDetectorConfig
from easycat.timeouts import TimeoutConfig
from easycat.tracing import TraceExporter, Tracer
from easycat.transports.local import LocalTransport, LocalTransportConfig
from easycat.transports.twilio_media import TwilioTransport, TwilioTransportConfig
from easycat.transports.websocket import WebSocketTransport, WebSocketTransportConfig
from easycat.tts.deepgram_tts import DeepgramTTS, DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTS, ElevenLabsTTSConfig
from easycat.tts.openai_tts import OpenAITTS, OpenAITTSConfig
from easycat.turn_manager import TurnManagerConfig
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
class TelephonyConfig:
    """Configuration for telephony helpers."""

    enable_dtmf_aggregator: bool = False
    enable_voicemail_detector: bool = False
    dtmf_aggregator: DTMFAggregatorConfig = field(default_factory=DTMFAggregatorConfig)
    voicemail_detector: VoicemailDetectorConfig = field(default_factory=VoicemailDetectorConfig)


TransportConfig = LocalTransportConfig | WebSocketTransportConfig | TwilioTransportConfig
STTConfig = OpenAISTTConfig | DeepgramSTTConfig | ElevenLabsSTTConfig
TTSConfig = OpenAITTSConfig | DeepgramTTSConfig | ElevenLabsTTSConfig

_STT_PROVIDERS: dict[type[STTConfig], Any] = {
    OpenAISTTConfig: OpenAISTT,
    DeepgramSTTConfig: DeepgramSTT,
    ElevenLabsSTTConfig: ElevenLabsSTT,
}

_TTS_PROVIDERS: dict[type[TTSConfig], Any] = {
    OpenAITTSConfig: OpenAITTS,
    DeepgramTTSConfig: DeepgramTTS,
    ElevenLabsTTSConfig: ElevenLabsTTS,
}

_TRANSPORT_FACTORIES: dict[type[TransportConfig], Any] = {
    LocalTransportConfig: lambda config, event_bus: LocalTransport(config),
    WebSocketTransportConfig: lambda config, event_bus: WebSocketTransport(config),
    TwilioTransportConfig: lambda config, event_bus: TwilioTransport(config=config, event_bus=event_bus),
}


@dataclass
class EasyCatConfig:
    """Top-level configuration for EasyCat sessions."""

    openai_api_key: str | None = None
    stt: STTConfig | None = None
    tts: TTSConfig | None = None
    vad: VADConfig = field(default_factory=VADConfig)
    noise_reduction: NoiseReducerConfig = field(default_factory=NoiseReducerConfig)
    transport: TransportConfig = field(default_factory=LocalTransportConfig)
    turn_taking: TurnManagerConfig = field(default_factory=TurnManagerConfig)
    smart_turn: SmartTurnConfig = field(default_factory=SmartTurnConfig)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    telephony: TelephonyConfig | None = None
    metrics: MetricsConfig | None = None
    tracing: TracingConfig | None = None
    agent: Any = None
    agent_runner: AgentRunnerConfig | None = None
    wrap_agent: bool = True

    def __post_init__(self) -> None:
        if self.openai_api_key:
            if self.stt is None:
                self.stt = OpenAISTTConfig(api_key=self.openai_api_key)
            if self.tts is None:
                self.tts = OpenAITTSConfig(api_key=self.openai_api_key)
        self._validate()

    def _validate(self) -> None:
        if self.stt is None:
            raise ValueError("STT configuration is required.")
        if self.tts is None:
            raise ValueError("TTS configuration is required.")
        if isinstance(self.stt, OpenAISTTConfig) and not self.stt.api_key:
            raise ValueError("OpenAI STT requires an API key.")
        if isinstance(self.stt, DeepgramSTTConfig) and not self.stt.api_key:
            raise ValueError("Deepgram STT requires an API key.")
        if isinstance(self.stt, ElevenLabsSTTConfig) and not self.stt.api_key:
            raise ValueError("ElevenLabs STT requires an API key.")
        if isinstance(self.tts, OpenAITTSConfig) and not self.tts.api_key:
            raise ValueError("OpenAI TTS requires an API key.")
        if isinstance(self.tts, DeepgramTTSConfig) and not self.tts.api_key:
            raise ValueError("Deepgram TTS requires an API key.")
        if isinstance(self.tts, ElevenLabsTTSConfig) and not self.tts.api_key:
            raise ValueError("ElevenLabs TTS requires an API key.")


def create_session(config: EasyCatConfig) -> Session:
    """Create a fully wired Session from EasyCatConfig."""
    event_bus = EventBus()
    stt = _create_stt_provider(config.stt, event_bus)
    tts = _create_tts_provider(config.tts, event_bus)
    vad = create_vad(config.vad)
    noise_reducer = create_noise_reducer(config.noise_reduction)
    transport = _create_transport(config.transport, event_bus)

    agent = config.agent or NoopAgent()
    if config.wrap_agent and config.agent is not None:
        runner_cfg = config.agent_runner or AgentRunnerConfig()
        agent = AgentRunner(agent, runner_cfg)

    metrics = _create_metrics(config.metrics)
    tracer = _create_tracer(config.tracing)

    turn_config = config.turn_taking
    smart_turn = create_smart_turn(config.smart_turn)
    if smart_turn is not None:
        turn_config = replace(turn_config, endpoint_detector=smart_turn)

    telephony_helpers = _create_telephony_helpers(event_bus, config.telephony)

    return Session(
        SessionConfig(
            stt=stt,
            tts=tts,
            vad=vad,
            noise_reducer=noise_reducer,
            transport=transport,
            agent=agent,
            event_bus=event_bus,
            turn_manager_config=turn_config,
            timeout_config=config.timeouts,
            metrics=metrics,
            tracer=tracer,
            telephony_helpers=telephony_helpers,
        )
    )


def _create_stt_provider(config: STTConfig, event_bus: EventBus) -> Any:
    provider_cls = _STT_PROVIDERS.get(type(config))
    if provider_cls is None:
        raise ValueError("Unsupported STT configuration type.")

    provider_config = config
    if isinstance(config, (DeepgramSTTConfig, ElevenLabsSTTConfig)) and config.event_bus is None:
        provider_config = replace(config, event_bus=event_bus)

    return provider_cls(provider_config)


def _create_tts_provider(config: TTSConfig, event_bus: EventBus) -> Any:
    provider_cls = _TTS_PROVIDERS.get(type(config))
    if provider_cls is None:
        raise ValueError("Unsupported TTS configuration type.")

    provider_config = config
    if isinstance(config, DeepgramTTSConfig) and config.event_bus is None:
        provider_config = replace(config, event_bus=event_bus)

    return provider_cls(provider_config)


def _create_transport(config: TransportConfig, event_bus: EventBus) -> Any:
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

    return helpers


def _create_metrics(config: MetricsConfig | None) -> MetricsCollector | None:
    if not config or not config.enabled:
        return None
    return config.collector or InMemoryMetrics()


def _create_tracer(config: TracingConfig | None) -> Tracer | None:
    if not config or not config.enabled:
        return None
    return Tracer(exporter=config.exporter)
