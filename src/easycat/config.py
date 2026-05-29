"""Top-level configuration and session factory for EasyCat."""

from __future__ import annotations

import asyncio
import copy
import logging
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from easycat.audio_format import PCM16_MONO_24K, AudioFormat
from easycat.echo_cancellation import EchoCancellationConfig, create_echo_canceller
from easycat.events import CallInitiated, CallScreening, CallStateChanged, EventBus, TTSAudio
from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
from easycat.integrations.agents._factory import auto_adapt_agent
from easycat.integrations.agents.base import NULL_RECORDER, AgentTurnInput
from easycat.llm_output_processing import LLMOutputProcessor
from easycat.noise_reduction import NoiseReducerConfig, create_noise_reducer
from easycat.providers import Transport
from easycat.runtime.artifacts import FilesystemArtifactStore, InMemoryArtifactStore
from easycat.runtime.capabilities import (
    bind_identity_sink_if_supported,
    default_echo_cancellation_enabled,
)
from easycat.runtime.journal import create_journal
from easycat.session._session import Session
from easycat.session._types import SessionConfig
from easycat.session.actions import SessionActionExecutor, SessionActions
from easycat.smart_turn import SmartTurnConfig, create_smart_turn
from easycat.stt.deepgram_provider import DeepgramSTTConfig
from easycat.stt.factory import STTConfig, create_stt_provider_from_config, parse_stt_string
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig
from easycat.stubs import NoopAgent
from easycat.telephony.call_state import (
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
from easycat.telephony.number_health import (
    CallDispositionTracker,
    NumberHealthMonitor,
)
from easycat.telephony.outbound import OutboundCallManager
from easycat.telephony.retry import RetryStrategy, RetryStrategyConfig
from easycat.telephony.screening import (
    CallScreeningDetector,
    ScreeningResponse,
    screening_patterns_for_languages,
)
from easycat.telephony.session_actions import (
    TwilioSessionActionConfig,
    TwilioSessionActionExecutor,
)
from easycat.telephony.voicemail import (
    PostScreeningVoicemailDetector,
    STTAMDFusionClassifier,
    VoicemailDetector,
    VoicemailDetectorConfig,
    VoicemailPolicyHandler,
)
from easycat.timeouts import TimeoutConfig
from easycat.transports.local import LocalTransport, LocalTransportConfig
from easycat.transports.twilio_media import TwilioTransport, TwilioTransportConfig
from easycat.transports.webrtc import WebRTCTransport, WebRTCTransportConfig
from easycat.transports.websocket import (
    WebSocketTransport,
    WebSocketTransportConfig,
)
from easycat.transports.webtransport import (
    WebTransportTransport,
    WebTransportTransportConfig,
)
from easycat.tts.cartesia_tts import CartesiaTTSConfig
from easycat.tts.deepgram_tts import DeepgramTTSConfig
from easycat.tts.elevenlabs_tts import ElevenLabsTTSConfig
from easycat.tts.factory import TTSConfig, create_tts_provider_from_config, parse_tts_string
from easycat.tts.openai_tts import OpenAITTSConfig
from easycat.turn_manager import TurnManagerConfig, TurnMode
from easycat.vad import VADConfig, create_vad

logger = logging.getLogger(__name__)


@contextmanager
def _openai_env_override(api_key: str | None) -> Iterator[None]:
    """Project a programmatic OpenAI key into ``OPENAI_API_KEY``.

    Lets :func:`parse_stt_string` / :func:`parse_tts_string` stay
    provider-agnostic: they read ``OPENAI_API_KEY`` like any other
    provider's env var, while this helper owns the
    ``EasyConfig.openai_api_key`` → env-var policy. The override is
    unwound on exit.
    """
    if not api_key:
        yield
        return
    prev = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = api_key
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = prev


_EASYCAT_LOG_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def _resolve_easycat_log_level(*, default: int) -> int:
    """Read ``EASYCAT_LOG_LEVEL`` and map it to a logging level.

    Unknown values fall back to the caller-supplied default so a typo
    doesn't silence the logger entirely.  Exposed at module scope so
    both ``EasyConfig._apply_debug_defaults`` and
    ``easycat.run`` converge on the same policy.
    """
    raw = os.getenv("EASYCAT_LOG_LEVEL", "").strip().lower()
    if not raw:
        return default
    return _EASYCAT_LOG_LEVELS.get(raw, default)


_ELEVENLABS_PCM_OUTPUT_RATES = (16000, 22050, 24000, 44100)


def _transport_tts_output_format(transport: TransportConfig) -> AudioFormat | None:
    preferred = getattr(transport, "preferred_tts_output_format", None)
    if isinstance(preferred, AudioFormat):
        return preferred

    transport_fmt = getattr(transport, "audio_format", None)
    if isinstance(transport_fmt, AudioFormat):
        return transport_fmt
    return None


def _closest_elevenlabs_output_format(rate: int) -> str:
    closest = min(_ELEVENLABS_PCM_OUTPUT_RATES, key=lambda candidate: abs(candidate - rate))
    return f"pcm_{closest}"


def _align_tts_config_to_transport(
    tts_config: TTSConfig,
    transport: TransportConfig,
) -> TTSConfig:
    target_format = _transport_tts_output_format(transport)
    if target_format is None:
        return tts_config

    if isinstance(tts_config, OpenAITTSConfig):
        if tts_config.output_format != PCM16_MONO_24K:
            return tts_config
        return replace(tts_config, output_format=target_format)

    if isinstance(tts_config, DeepgramTTSConfig):
        if tts_config.sample_rate != 24000 or tts_config.output_format != PCM16_MONO_24K:
            return tts_config
        provider_rate = (
            target_format.sample_rate if target_format.sample_rate in (16000, 24000) else 24000
        )
        return replace(
            tts_config,
            sample_rate=provider_rate,
            output_format=target_format,
        )

    if isinstance(tts_config, CartesiaTTSConfig):
        if tts_config.sample_rate != 24000 or tts_config.output_format != PCM16_MONO_24K:
            return tts_config
        provider_rate = (
            target_format.sample_rate if target_format.sample_rate in (16000, 24000) else 24000
        )
        return replace(
            tts_config,
            sample_rate=provider_rate,
            output_format=target_format,
        )

    if isinstance(tts_config, ElevenLabsTTSConfig):
        if tts_config.output_format != "pcm_24000" or tts_config.audio_format != PCM16_MONO_24K:
            return tts_config
        return replace(
            tts_config,
            output_format=_closest_elevenlabs_output_format(target_format.sample_rate),
            audio_format=target_format,
        )

    return tts_config


@dataclass
class VoicemailDetectionConfig:
    """Provider-neutral voicemail / answering machine detection knobs.

    The shape mirrors Twilio's AMD parameters today because Twilio
    is the only supported outbound provider, but the names are
    provider-neutral so future Telnyx / Plivo / SIP backends can
    honor the same config without renaming.

    ``mode`` selects how aggressively the provider tries to classify:

    - ``"detect"``: classify answered-by (human/machine) as fast as possible
    - ``"detect_end_of_greeting"``: wait for the voicemail greeting to
      finish so the bot can leave a message (Twilio's
      ``DetectMessageEnd``). This is the default.

    ``detection_timeout_s`` is the ceiling for the classifier; after
    that the pipeline proceeds with whatever signal it has.
    ``speech_threshold_ms`` and ``speech_end_threshold_ms`` tune the
    provider's internal voice-onset / end detectors.
    ``silence_timeout_ms`` bounds how long the provider waits for any
    audio before giving up.
    """

    mode: Literal["detect", "detect_end_of_greeting"] = "detect_end_of_greeting"
    async_mode: bool = True
    detection_timeout_s: int = 30
    speech_threshold_ms: int = 2400
    speech_end_threshold_ms: int = 1200
    silence_timeout_ms: int = 5000

    def to_twilio_params(self) -> dict[str, Any]:
        """Render as the kwargs :class:`OutboundCallManager` expects today."""
        twilio_mode = "DetectMessageEnd" if self.mode == "detect_end_of_greeting" else "Enable"
        return {
            "amd_mode": twilio_mode,
            "async_amd": self.async_mode,
            "amd_timeout": self.detection_timeout_s,
            "speech_threshold": self.speech_threshold_ms,
            "speech_end_threshold": self.speech_end_threshold_ms,
            "silence_timeout": self.silence_timeout_ms,
        }


@dataclass
class OutboundCallConfig:
    """Configuration for outbound call manager."""

    from_number: str = ""
    # Voicemail / answering-machine detection.  Defaults are Twilio's
    # ``DetectMessageEnd`` posture — wait for the greeting to finish
    # so the bot can leave a message.  Pre-release code accepted the
    # flat Twilio fields directly; use
    # ``VoicemailDetectionConfig(...).to_twilio_params()`` when
    # migrating.
    voicemail_detection: VoicemailDetectionConfig = field(default_factory=VoicemailDetectionConfig)
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

    # Observability / reliability extras.  All default to on — they're
    # pure event-bus listeners with no external dependencies and give
    # the caller per-number answer rates, disposition breakdowns, and
    # a ready-to-use retry policy for failed Twilio attempts.
    enable_number_health: bool = True
    enable_disposition_tracker: bool = True
    enable_retry_strategy: bool = True
    retry_strategy: RetryStrategyConfig | None = None

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
    twilio_actions: TwilioSessionActionConfig | None = None


TransportConfig = (
    LocalTransportConfig
    | WebSocketTransportConfig
    | TwilioTransportConfig
    | WebRTCTransportConfig
    | WebTransportTransportConfig
    | Transport
)
_TRANSPORT_FACTORIES: dict[type[TransportConfig], Any] = {
    LocalTransportConfig: lambda config, event_bus: LocalTransport(config),
    WebSocketTransportConfig: lambda config, event_bus: WebSocketTransport(config),
    TwilioTransportConfig: lambda config, event_bus: TwilioTransport(
        config=config, event_bus=event_bus
    ),
    WebRTCTransportConfig: lambda config, event_bus: WebRTCTransport(config),
    WebTransportTransportConfig: lambda config, event_bus: WebTransportTransport(config),
}


class EasyConfigError(ValueError):
    """Raised when app config validation fails."""


_VALID_MCP_SCHEMES = ("stdio://", "sse://", "http://", "https://")
_VALID_DEBUG = {"off", "light", "full"}
_VALID_JOURNAL_BACKEND = {"sqlite", "sqlite+litestream", "libsql"}
_VALID_JOURNAL_RETENTION = {"archive", "delete"}


def _validate_common(
    *,
    debug: str,
    journal_backend: str,
    journal_retention: str,
    mcp_servers: list[str] | None = None,
    session_id: str | None = None,
    agent: Any | None = None,
    agent_model: str | None = None,
) -> None:
    """Validate the shared fields used by both session factories."""
    if debug not in _VALID_DEBUG:
        raise ValueError(f"Invalid debug={debug!r}. Must be one of {sorted(_VALID_DEBUG)}.")
    if journal_backend not in _VALID_JOURNAL_BACKEND:
        raise ValueError(
            f"Invalid journal_backend={journal_backend!r}. "
            f"Must be one of {sorted(_VALID_JOURNAL_BACKEND)}."
        )
    if journal_retention not in _VALID_JOURNAL_RETENTION:
        raise ValueError(
            f"Invalid journal_retention={journal_retention!r}. "
            f"Must be one of {sorted(_VALID_JOURNAL_RETENTION)}."
        )
    if mcp_servers is not None:
        for uri in mcp_servers:
            if not any(uri.startswith(scheme) for scheme in _VALID_MCP_SCHEMES):
                raise EasyConfigError(
                    f"Invalid MCP server URI: {uri!r}. "
                    f"Must start with one of {', '.join(_VALID_MCP_SCHEMES)}"
                )
    if session_id is not None and ("/" in session_id or "\\" in session_id or ".." in session_id):
        raise EasyConfigError(
            f"session_id must not contain path separators or '..': {session_id!r}"
        )
    if isinstance(agent, str):
        from urllib.parse import urlparse

        parsed = urlparse(agent)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            if agent_model is None:
                raise EasyConfigError(
                    "agent_model is required when agent is a URL string. "
                    "Set agent_model to the model identifier the remote "
                    "Responses API server should use."
                )


def _inject_agent_runtime(
    agent: Any,
    *,
    mcp_servers: tuple[str, ...] | list[str] = (),
    agent_model: str | None = None,
    remote_agent_api_key: str | None = None,
) -> None:
    """Push session-level MCP/model/key settings into the bridge.

    Unwraps an ``AgentRunner`` to reach the inner bridge.  Does not
    return a new agent — mutates ``_mcp_servers`` / ``_model`` /
    ``_api_key`` on the bridge in place, which is the shape bridges
    expect.
    """
    from easycat.integrations.agents.responses_api import RemoteResponsesAPIBridge

    inner = agent._agent if isinstance(agent, AgentRunner) else agent
    if hasattr(inner, "_mcp_servers"):
        # Always overwrite (even with empty tuple) so a bridge reused
        # across sessions doesn't leak a previous MCP list.
        inner._mcp_servers = list(mcp_servers)
    if isinstance(inner, RemoteResponsesAPIBridge):
        if agent_model:
            inner._model = agent_model
        if remote_agent_api_key:
            inner._api_key = remote_agent_api_key


def _provider_display_name(cfg: Any, kind: Literal["STT", "TTS"]) -> str:
    """Human-facing label for a provider config in error messages.

    Prefers the registered provider name from the STT/TTS
    :class:`~easycat._provider_catalog.ProviderCatalog` (e.g.
    ``"deepgram STT"``) so the missing-API-key error reads consistently
    for every registered provider. Falls back to the config class name
    when the config type isn't in the catalog (e.g. a custom config).
    """
    if kind == "STT":
        from easycat.stt.factory import _CATALOG as catalog
    else:
        from easycat.tts.factory import _CATALOG as catalog

    cfg_type = type(cfg)
    for provider_name, (_provider_cls, config_cls) in catalog.providers.items():
        if config_cls is cfg_type:
            return f"{provider_name} {kind}"
    return type(cfg).__name__.replace("Config", "")


@dataclass
class EasyConfig:
    """Top-level configuration for EasyCat sessions.

    Fields:
        mcp_servers: Optional list of MCP server URIs to pass through to
            agent bridges.  Accepted schemes: ``stdio://``, ``sse://``,
            ``http://``, ``https://``.  Frozen per session — mid-session
            changes are not supported.
    """

    openai_api_key: str | None = None
    stt: STTConfig | str | None = None
    tts: TTSConfig | str | None = None
    vad: VADConfig = field(default_factory=VADConfig)
    noise_reduction: NoiseReducerConfig | None = None
    echo_cancellation: EchoCancellationConfig | None = None
    enable_noise_reduction: bool = False
    enable_echo_cancellation: bool | None = None
    transport: TransportConfig = field(default_factory=LocalTransportConfig)
    turn_taking: TurnManagerConfig = field(default_factory=TurnManagerConfig)
    smart_turn: SmartTurnConfig = field(default_factory=SmartTurnConfig)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    telephony: TelephonyConfig | None = None
    agent: Any = None
    agent_model: str | None = None
    remote_agent_api_key: str | None = None
    agent_runner: AgentRunnerConfig | None = None
    # When True (default), a plain ``async run(text) -> str`` agent is
    # auto-wrapped in :class:`AgentRunner` so it gets timeout, history,
    # and cancellation handling out of the box.  Set to ``False`` only
    # when you are passing in a fully-constructed
    # :class:`ExternalAgentBridge` and want to drive it without the
    # ``AgentRunner`` defaults — useful for tests and for bridges that
    # implement their own retry/timeout policy.
    wrap_agent: bool = True
    strip_markdown: bool = False
    auto_align_tts_output_to_transport: bool = True
    output_processors: Sequence[LLMOutputProcessor] = ()
    session_actions: SessionActions | None = None
    action_executors: Sequence[SessionActionExecutor] = ()
    debug: Literal["off", "light", "full"] = "off"
    journal_backend: Literal["sqlite", "sqlite+litestream", "libsql"] = "sqlite"
    journal_retention: Literal["archive", "delete"] = "archive"
    mcp_servers: list[str] | None = None
    # When set, every session exports a timestamped debug bundle to this
    # directory on stop/shutdown — the "always be recording" flow so a
    # user who hits a real failure already has the bundle saved to disk
    # without flipping any switch.  Requires ``debug != "off"`` so the
    # journal actually exists.
    record_to: str | Path | None = None

    # Optional greeting text synthesized on the first
    # :class:`~easycat.events.CallAnswered`.  Makes the bot speak
    # first on both inbound and outbound calls — the canonical
    # outbound pattern and the FCC's preferred moment for an
    # AI-disclosure utterance.  Set to ``None`` (default) to preserve
    # user-speaks-first behaviour.
    greeting: str | None = None

    # Optional Do-Not-Call list reused for opt-out auto-detection.
    # Session-level opt-out detection runs on every STT final and, on
    # match, adds the caller's number here.  Attaching the same
    # ``DNCList`` across sessions lets the list persist in-process
    # without a database.
    dnc_list: Any | None = None

    # Telephony caller-ID exposure.  ``"tools_only"`` (default) keeps
    # the caller's phone number out of the LLM prompt but available to
    # tool code via ``session.call_identity``.  ``"system_message"``
    # prepends a short system note on every turn so the agent can
    # reason about the caller.  ``"off"`` hides it from both layers.
    # Internal telephony hooks still retain a private identity for
    # opt-out/DNC handling.
    # See :class:`easycat.session._types.CallIdentity` for the data
    # model and :class:`easycat.session._types.CallerIdExposure` for
    # the allowed literals.
    caller_id_exposure: Literal["off", "system_message", "tools_only"] = "tools_only"

    def __post_init__(self) -> None:
        _validate_common(
            debug=self.debug,
            journal_backend=self.journal_backend,
            journal_retention=self.journal_retention,
            mcp_servers=self.mcp_servers,
            agent=self.agent,
            agent_model=self.agent_model,
        )

        # Pick up OPENAI_API_KEY for the zero-config case so a bare
        # ``EasyConfig(agent=...)`` works when the env var is set —
        # the standard OpenAI SDK convention.  Resolved before string
        # parsing so ``stt="openai-realtime"`` honors the env var
        # without needing to be passed explicitly.
        if self.openai_api_key is None and (env_key := os.getenv("OPENAI_API_KEY")):
            self.openai_api_key = env_key

        # Resolve string-keyed provider shortcuts ("deepgram/flux" →
        # DeepgramSTTConfig(...)) before any downstream validation. Typed
        # configs still take precedence — users can pass a concrete
        # DeepgramSTTConfig and keep full control. A programmatic
        # ``openai_api_key`` is projected into the OpenAI env vars for
        # the duration of parsing so ``stt="openai"`` works without the
        # env var also being exported; the factory itself stays
        # provider-agnostic.
        with _openai_env_override(self.openai_api_key):
            if isinstance(self.stt, str):
                self.stt = parse_stt_string(self.stt)
            if isinstance(self.tts, str):
                self.tts = parse_tts_string(self.tts)

        if self.openai_api_key:
            if self.stt is None:
                # Default to the Realtime WebSocket STT: audio is streamed
                # as it arrives (sub-second stop-to-final), versus the
                # batch ``/v1/audio/transcriptions`` endpoint which waits
                # for end-of-turn to upload the whole utterance.
                self.stt = OpenAIRealtimeSTTConfig(api_key=self.openai_api_key)
            if self.tts is None:
                self.tts = OpenAITTSConfig(api_key=self.openai_api_key)
        if self.tts is not None and self.auto_align_tts_output_to_transport:
            self.tts = _align_tts_config_to_transport(self.tts, self.transport)
        if self.echo_cancellation is None:
            self.echo_cancellation = self._default_echo_cancellation_for_transport()
        if self.debug in ("light", "full"):
            self._apply_debug_defaults()
        self._validate()

    def _default_echo_cancellation_for_transport(self) -> EchoCancellationConfig:
        # ``enable_echo_cancellation`` is tri-state: None means "use the
        # transport default" (auto-enable for transports that typically have
        # a speaker loopback), while True/False explicitly force the flag
        # on or off regardless of transport.
        if self.enable_echo_cancellation is None:
            enable_aec = default_echo_cancellation_enabled(self.transport)
        else:
            enable_aec = self.enable_echo_cancellation
        return EchoCancellationConfig(enabled=enable_aec)

    def _apply_debug_defaults(self) -> None:
        """Enable verbose logging when debug mode is active.

        ``EASYCAT_LOG_LEVEL`` (``debug|info|warning|error``) overrides
        the default level so users can keep ``debug="light"`` wiring on
        while dialling the log verbosity up or down without code
        changes — mirrors ``LIVEKIT_LOG_LEVEL`` / ``UVICORN_LOG_LEVEL``.
        """
        if not logging.root.handlers:
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s %(name)s %(levelname)s %(message)s",
            )
        level = _resolve_easycat_log_level(default=logging.DEBUG)
        logging.getLogger("easycat").setLevel(level)
        logger.debug("EasyCat debug mode enabled (level=%s)", logging.getLevelName(level))

    def _validate(self) -> None:
        if self.stt is None:
            raise ValueError("STT configuration is required.")
        if self.tts is None:
            raise ValueError("TTS configuration is required.")
        for cfg, kind in ((self.stt, "STT"), (self.tts, "TTS")):
            if hasattr(cfg, "api_key") and not cfg.api_key:
                name = _provider_display_name(cfg, kind)
                raise ValueError(f"{name} requires an API key.")

    # ── Factory presets ──────────────────────────────────────────
    #
    # Classmethod shortcuts that pick sensible transport defaults for
    # the three canonical deployment surfaces (local mic / browser /
    # phone) and the text REPL used for agent iteration.  Users can
    # still override any field via keyword argument — the preset only
    # fills the transport default when the caller didn't supply one.
    # Documented in ``peripheral-dx-onboarding.md``.

    @classmethod
    def mic(cls, **kwargs: Any) -> EasyConfig:
        """Local-microphone preset — the default developer setup."""
        kwargs.setdefault("transport", LocalTransportConfig())
        return cls(**kwargs)

    @classmethod
    def browser(cls, **kwargs: Any) -> EasyConfig:
        """WebRTC-in-the-browser preset.

        Enables echo cancellation by default because browser clients
        loop transport audio back through the mic.
        """
        kwargs.setdefault("transport", WebRTCTransportConfig())
        kwargs.setdefault("enable_echo_cancellation", True)
        return cls(**kwargs)

    @classmethod
    def phone(cls, **kwargs: Any) -> EasyConfig:
        """Inbound telephony preset.

        Uses the Twilio Media Streams transport and leaves echo-cancel
        on its tri-state default (off for PSTN, which has no loopback).
        """
        kwargs.setdefault("transport", TwilioTransportConfig())
        return cls(**kwargs)


def _should_auto_turn_from_stt_final(config: EasyConfig) -> bool:
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


def _create_artifact_store(
    session_id: str, debug: str
) -> InMemoryArtifactStore | FilesystemArtifactStore | None:
    if debug == "off":
        return None
    if debug == "full":
        return FilesystemArtifactStore(session_id)
    return InMemoryArtifactStore()


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
        return replace(existing, **updates)

    merged = copy.copy(existing)
    for key, value in updates.items():
        setattr(merged, key, value)
    return merged


def create_session(config: EasyConfig) -> Session:
    """Create a fully wired Session from EasyConfig."""
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

        telephony_helpers = _create_telephony_helpers(
            event_bus,
            config.telephony,
            dnc_list=config.dnc_list,
        )
        action_executors = [*config.action_executors, *_create_action_executors(config.telephony)]

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
            session.call_identity = _merge_twilio_identity(session._call_identity, identity)

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

    if _outbound_sm is not None:
        _wire_outbound_pipeline(
            session,
            _outbound_sm,
            telephony_helpers,
            event_bus,
        )

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
        if session._call_identity is not None and session._call_identity.direction == "inbound":
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
        _maybe_launch_debugger_ui(session)

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
    original_shutdown = session.shutdown
    already_exported = False

    async def _export_bundle() -> None:
        nonlocal already_exported
        if already_exported:
            return
        already_exported = True
        try:
            record_to.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            path = record_to / f"{session.session_id}-{stamp}.zip"
            session.export_debug_bundle(str(path))
            logger.info("Recorded debug bundle to %s", path)
        except Exception:
            logger.exception("Failed to record debug bundle to %s", record_to)

    async def _wrapped_stop() -> None:
        try:
            await original_stop()
        finally:
            await _export_bundle()

    async def _wrapped_shutdown() -> None:
        try:
            await original_shutdown()
        finally:
            await _export_bundle()

    session.stop = _wrapped_stop  # type: ignore[method-assign]
    session.shutdown = _wrapped_shutdown  # type: ignore[method-assign]


def _maybe_launch_debugger_ui(session: Session) -> None:
    """Spin up the interactive debugger on localhost when debug="full".

    The debugger is an optional extra (``easycat[debugger]`` → aiohttp);
    when it isn't installed we log once and keep the session usable
    rather than crashing.  Pytest and CI runs are detected via
    ``PYTEST_CURRENT_TEST`` so the auto-launch never fights a test
    harness that already has the port or the loop.  Host/port
    overrides come from ``EASYCAT_DEBUGGER_PORT`` because the debugger
    UI is a local-dev convenience, not a production surface.
    """
    if os.getenv("PYTEST_CURRENT_TEST") or os.getenv("EASYCAT_DEBUGGER_DISABLE"):
        return
    # aiohttp is the real gate — the debugger module imports fine
    # without it, but the server fails the moment ``web.run_app`` is
    # called.  Probe explicitly so we log a clean skip message instead
    # of crashing a background thread.
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        logger.info(
            "debug='full' requested but easycat[debugger] is not installed; "
            "skipping auto-launch. `pip install easycat[debugger]` to enable."
        )
        return

    try:
        from easycat.debugger import serve_session
    except ImportError:
        logger.info(
            "debug='full' requested but the debugger module is unavailable; skipping auto-launch."
        )
        return

    try:
        port = int(os.getenv("EASYCAT_DEBUGGER_PORT", "8765"))
    except ValueError:
        port = 8765
    open_browser = os.getenv("EASYCAT_DEBUGGER_OPEN_BROWSER", "1") != "0"
    try:
        serve_session(
            session,
            port=port,
            open_browser=open_browser,
            in_thread=True,
        )
    except OSError as exc:
        logger.warning("Could not start debugger UI on port %s: %s", port, exc)
    except Exception:
        logger.exception("Debugger UI failed to start; continuing without it.")


@dataclass
class TextSessionConfig:
    """Configuration for a text-only Session (no audio pipeline).

    Mirrors the shared journal/debug/agent fields of :class:`EasyConfig`
    so both ``create_session`` and ``create_text_session`` accept a
    single config object of the ``create_*(config)`` shape. Audio-only
    fields (``stt``/``tts``/``vad``/``transport``/etc.) have no analogue
    here because text sessions never enter the audio pipeline; an
    :class:`EasyConfig` user moving to text can copy the shared fields
    across.

    Validated by the same :func:`_validate_common` as :class:`EasyConfig`.
    """

    agent: Any = None
    session_id: str | None = None
    debug: Literal["off", "light", "full"] = "off"
    journal_backend: Literal["sqlite", "sqlite+litestream", "libsql"] = "sqlite"
    journal_retention: Literal["archive", "delete"] = "archive"
    wrap_agent: bool = True
    agent_runner: AgentRunnerConfig | None = None
    agent_model: str | None = None
    remote_agent_api_key: str | None = None
    mcp_servers: list[str] | None = None

    def __post_init__(self) -> None:
        _validate_common(
            debug=self.debug,
            journal_backend=self.journal_backend,
            journal_retention=self.journal_retention,
            mcp_servers=self.mcp_servers,
            session_id=self.session_id,
            agent=self.agent,
            agent_model=self.agent_model,
        )


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
    loose keyword arguments. The two forms are mutually exclusive.

    The returned session supports :meth:`Session.send_text` for
    request/response agent interaction without STT, TTS, VAD, or
    transport.  Useful for testing agent logic and building text-based
    UIs on the same agent adapter stack.

    Raises :class:`RuntimeError` if the caller attempts to call
    :meth:`Session.start` on a text session.
    """
    if config is None:
        config = TextSessionConfig(
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
                try:
                    await self._hold_audio_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._hold_audio_task = None
        await self._session.replay_gated_audio(events)

    def play_hold_audio(self, text: str) -> None:
        async def _synthesize_hold() -> None:
            try:
                await self._session.synthesize_bypass(text)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Hold audio synthesis failed")

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("No running event loop — hold audio skipped")
            return

        # The lock is async-only; since this is a sync callback we just
        # do a best-effort swap — the flush side holds the lock and will
        # cancel whatever task reference it sees.
        self._hold_audio_task = loop.create_task(_synthesize_hold())


async def _run_agent_once(agent: Any, prompt: str) -> str:
    """Drive the agent once and return its final text response.

    Works whether ``agent`` is an :class:`AgentRunner` (has ``run()``) or
    a raw :class:`ExternalAgentBridge` (only implements ``invoke()``), so
    ``wrap_agent=False`` sessions still support agent-mode screening.
    """
    run_fn = getattr(agent, "run", None)
    if callable(run_fn):
        return await run_fn(prompt)
    accumulated = ""
    async for event in agent.invoke(AgentTurnInput.from_text(prompt), NULL_RECORDER):
        if event.kind == "text_delta" and event.text:
            accumulated += event.text
        elif event.kind == "done" and event.text:
            accumulated = event.text
    return accumulated


def _wire_outbound_pipeline(
    session: Session,
    sm: OutboundCallStateMachine,
    helpers: list[Any],
    event_bus: EventBus,
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
                response_text = await _run_agent_once(
                    session.agent,
                    f"The callee's phone is screening this call. "
                    f'Their screening prompt says: "{prompt}". '
                    f"Identify yourself briefly.",
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


def _create_telephony_helpers(
    event_bus: EventBus,
    config: TelephonyConfig | None,
    *,
    dnc_list: Any | None = None,
) -> list[Any]:
    helpers: list[Any] = []
    if config is None:
        return helpers

    if config.enable_dtmf_aggregator:
        helpers.append(DTMFAggregator(event_bus, config.dtmf_aggregator))

    if config.enable_voicemail_detector:
        helpers.append(VoicemailDetector(event_bus, config.voicemail_detector))

    if config.enable_outbound_call_manager and config.outbound:
        _create_outbound_helpers(event_bus, config.outbound, helpers, dnc_list=dnc_list)

    return helpers


def _create_action_executors(config: TelephonyConfig | None) -> list[SessionActionExecutor]:
    executors: list[SessionActionExecutor] = []
    if config is None:
        return executors
    if config.twilio_actions is not None:
        executors.append(TwilioSessionActionExecutor(config.twilio_actions))
    return executors


def _create_outbound_helpers(
    event_bus: EventBus,
    oc: OutboundCallConfig,
    helpers: list[Any],
    *,
    dnc_list: Any | None = None,
) -> None:
    """Build and wire the outbound call pipeline helpers."""
    # STT+AMD fusion classifier — must be wired before the state machine
    # so that raw AMD events are intercepted and re-emitted with source="fusion".
    fusion = STTAMDFusionClassifier(event_bus)
    helpers.append(fusion)

    # Post-screening voicemail detector — re-classifies after screening.
    post_screening_vm = PostScreeningVoicemailDetector(event_bus)
    helpers.append(post_screening_vm)

    # Disposition tracking must subscribe before the state machine: on
    # CallFailed, the tracker records the specific failure reason before
    # the state machine emits the terminal ENDED transition.
    if oc.enable_disposition_tracker:
        helpers.append(CallDispositionTracker(event_bus))

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
        classification_timeout_s=float(oc.voicemail_detection.detection_timeout_s),
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

    # Observability helpers — pure event-bus listeners, on by default.
    if oc.enable_number_health:
        helpers.append(NumberHealthMonitor(event_bus))

    # Outbound call manager (requires Twilio credentials).
    manager: OutboundCallManager | None = None
    if oc.twilio_account_sid and oc.twilio_auth_token:
        try:
            manager = OutboundCallManager(
                event_bus,
                from_number=oc.from_number,
                enable_realtime_transcription=oc.enable_realtime_transcription,
                twilio_account_sid=oc.twilio_account_sid,
                twilio_auth_token=oc.twilio_auth_token,
                twiml_url=oc.twiml_url,
                status_callback_url=oc.status_callback_url,
                **oc.voicemail_detection.to_twilio_params(),
            )
            manager.dnc_list = dnc_list
            helpers.append(manager)
        except ImportError:
            logger.warning("twilio package not installed — OutboundCallManager disabled")

    # Retry strategy — stateless object the caller asks
    # ``strategy.record_attempt(number, reason)`` to decide whether to
    # re-place a failed call.  We attach it to the manager (when
    # present) so app code can reach it via
    # ``session.outbound_call_manager.retry_strategy``.
    if oc.enable_retry_strategy and manager is not None:
        manager.retry_strategy = RetryStrategy(oc.retry_strategy)


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
