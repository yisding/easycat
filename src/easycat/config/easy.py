"""The lightweight EasyCat config dataclasses — the first thing to read.

This is the "super easy" surface: the config dataclasses a newcomer sees
first (:class:`EasyConfig`, :class:`TextSessionConfig`, and the telephony
config trio) plus their validation. The session-building factories
(:func:`create_session` / :func:`create_text_session`) live in
:mod:`easycat.config._factory`, and the outbound/telephony runtime wiring
lives in :mod:`easycat.config._telephony_wiring` — both imported lazily so
touching :class:`EasyConfig` never drags in the Session class or the
telephony stack.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from easycat.echo_cancellation import EchoCancellationConfig
from easycat.errors import EASYCAT_E203
from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
from easycat.llm_output_processing import LLMOutputProcessor
from easycat.noise_reduction import NoiseReducerConfig
from easycat.providers import (
    EchoCanceller,
    NoiseReducer,
    STTProvider,
    Transport,
    TTSProvider,
    VADProvider,
)
from easycat.runtime.capabilities import default_echo_cancellation_enabled
from easycat.session.actions import SessionActionExecutor, SessionActions
from easycat.smart_turn import SmartTurnConfig, _validate_probability_threshold
from easycat.stt.factory import STTConfig, parse_stt_string
from easycat.stt.openai_realtime_provider import OpenAIRealtimeSTTConfig

# Lightweight, config-only dataclasses needed at *module* scope — for the
# ``TransportConfig`` union and ``field(default_factory=...)`` defaults. These
# submodule imports stay cheap because ``easycat.telephony`` /
# ``easycat.transports`` load their members lazily (PEP 562), so none of them
# drags in the rest of the telephony / transport stack. The heavier runtime
# classes (state machines, navigators, transport implementations, the outbound
# call manager, etc.) are imported lazily inside ``easycat.config._factory`` /
# ``easycat.config._telephony_wiring`` — so touching ``EasyConfig`` never pulls
# them in.
from easycat.telephony.dtmf import DTMFAggregatorConfig
from easycat.telephony.voicemail import VoicemailDetectorConfig
from easycat.timeouts import TimeoutConfig
from easycat.transports._webrtc_config import WebRTCTransportConfig
from easycat.transports.local import LocalTransportConfig
from easycat.transports.twilio_media import TwilioTransportConfig
from easycat.transports.websocket import WebSocketTransportConfig
from easycat.transports.webtransport import WebTransportTransportConfig
from easycat.tts.factory import TTSConfig, is_tts_config, parse_tts_string
from easycat.tts.openai_tts import OpenAITTSConfig
from easycat.turn_manager import TurnManagerConfig, TurnMode
from easycat.vad import VADConfig

if TYPE_CHECKING:
    # Annotation-only references to telephony runtime types. Kept out of the
    # module-level import set (which would re-trigger the telephony fan-out)
    # because ``from __future__ import annotations`` makes these lazy strings.
    from easycat.telephony.compliance import DNCStore
    from easycat.telephony.ivr import AgentCallback, DTMFDelivery
    from easycat.telephony.retry import RetryStrategyConfig
    from easycat.telephony.session_actions import TwilioSessionActionConfig

logger = logging.getLogger("easycat.config")

_MIN_VAD_PRE_ROLL_MARGIN_MS = 150


# ── Log-level helpers ───────────────────────────────────────────────


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
    doesn't silence the logger entirely.  Exposed at module scope so the
    single console-logging entry point
    (``easycat._logging.enable_console_logging``) applies one consistent
    ``EASYCAT_LOG_LEVEL`` policy across both
    ``EasyConfig._apply_debug_defaults`` and ``easycat.run``.
    """
    raw = os.getenv("EASYCAT_LOG_LEVEL", "").strip().lower()
    if not raw:
        return default
    return _EASYCAT_LOG_LEVELS.get(raw, default)


# ── Validation helpers ───────────────────────────────────────────────


class EasyConfigError(ValueError):
    """Raised when app config validation fails."""


_VALID_MCP_SCHEMES = ("stdio://", "sse://", "http://", "https://")
_VALID_DEBUG = {"off", "light", "full"}
_VALID_JOURNAL_BACKEND = {"sqlite", "sqlite+litestream", "libsql"}
_VALID_JOURNAL_REDACTION = {"secrets", "pii"}
_VALID_JOURNAL_RETENTION = {"archive", "delete"}
_SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _require_positive(name: str, value: float) -> None:
    """Raise ``ValueError`` if ``value`` is not strictly positive."""
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_non_negative(name: str, value: float) -> None:
    """Raise ``ValueError`` if ``value`` is negative."""
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _validate_on_agent_failure(
    policy: str | Callable[[Exception], str] | None,
) -> None:
    if policy is not None and not (isinstance(policy, str) or callable(policy)):
        raise ValueError("on_agent_failure must be text, a callable, or None")
    if isinstance(policy, str) and not policy.strip():
        raise ValueError("on_agent_failure text must not be empty")


def _validate_capture_audio(policy: bool | Callable[[], bool]) -> None:
    if not isinstance(policy, bool) and not callable(policy):
        raise ValueError("capture_audio must be a bool or zero-argument callable")
    predicate_call = type(policy).__call__ if callable(policy) else None
    if callable(policy) and (
        inspect.iscoroutinefunction(policy) or inspect.iscoroutinefunction(predicate_call)
    ):
        raise ValueError("capture_audio predicate must be synchronous")


def _validate_journal_capacity(capacity: int) -> None:
    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
        raise ValueError("journal_capacity must be a positive integer")


def _validate_journal_redaction(policy: str) -> None:
    if policy not in _VALID_JOURNAL_REDACTION:
        raise ValueError(
            f"Invalid journal_redaction={policy!r}. "
            f"Must be one of {sorted(_VALID_JOURNAL_REDACTION)}."
        )


def _validate_common(
    *,
    debug: str,
    journal_backend: str,
    journal_capacity: int,
    journal_redaction: str,
    journal_retention: str,
    mcp_servers: list[str] | None = None,
    session_id: str | None = None,
    agent: Any | None = None,
    agent_model: str | None = None,
    capture_audio: bool | Callable[[], bool] = True,
) -> None:
    """Validate the shared fields used by both session factories."""
    if debug not in _VALID_DEBUG:
        raise ValueError(f"Invalid debug={debug!r}. Must be one of {sorted(_VALID_DEBUG)}.")
    if journal_backend not in _VALID_JOURNAL_BACKEND:
        raise ValueError(
            f"Invalid journal_backend={journal_backend!r}. "
            f"Must be one of {sorted(_VALID_JOURNAL_BACKEND)}."
        )
    _validate_journal_capacity(journal_capacity)
    _validate_journal_redaction(journal_redaction)
    if journal_retention not in _VALID_JOURNAL_RETENTION:
        raise ValueError(
            f"Invalid journal_retention={journal_retention!r}. "
            f"Must be one of {sorted(_VALID_JOURNAL_RETENTION)}."
        )
    _validate_capture_audio(capture_audio)
    if mcp_servers is not None:
        for uri in mcp_servers:
            if not any(uri.startswith(scheme) for scheme in _VALID_MCP_SCHEMES):
                raise EasyConfigError(
                    f"Invalid MCP server URI: {uri!r}. "
                    f"Must start with one of {', '.join(_VALID_MCP_SCHEMES)}"
                )
    if session_id is not None:
        if not session_id.strip():
            raise EasyConfigError("session_id must not be empty")
        if _SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise EasyConfigError(
                "session_id must be 1-128 ASCII letters, digits, '.', '_', or '-', "
                f"starting with a letter or digit: {session_id!r}"
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


def _stt_uses_native_endpointing(stt: Any) -> bool:
    """Whether ``stt`` does its own turn endpointing (provider-side VAD).

    Shared by the smart-turn default here and
    ``_should_auto_turn_from_stt_final`` in the factory so both stay
    consistent: such providers derive turn boundaries from their own
    endpointing, so EasyCat should drive turns from STT finals and run
    neither smart-turn nor the Silero VAD it pulls in (which would otherwise
    double-endpoint and produce duplicate FINAL transcripts).

    Covers:
      - Deepgram **Flux** (native end-of-turn signal),
      - Cartesia **ink-2** (native semantic turn detection), and
      - ElevenLabs realtime with the built-in **VAD** commit strategy.
    """
    from easycat.stt.cartesia_provider import CartesiaSTTConfig
    from easycat.stt.deepgram_provider import DeepgramSTTConfig
    from easycat.stt.elevenlabs_provider import ElevenLabsSTTConfig

    if isinstance(stt, DeepgramSTTConfig):
        return stt.is_flux
    if isinstance(stt, CartesiaSTTConfig):
        return stt.resolved_model == "ink-2"
    if isinstance(stt, ElevenLabsSTTConfig):
        return stt.mode == "realtime" and stt.realtime_commit_strategy == "vad"
    return False


def _normalize_smart_turn_config(
    smart_turn: SmartTurnConfig | bool | None,
    *,
    sensitivity: float | None,
    transport: Any = None,
    stt_native_endpointing: bool = False,
) -> SmartTurnConfig:
    """Resolve EasyConfig's beginner-facing smart-turn shortcuts.

    When ``smart_turn`` is left unset (``None``) and no sensitivity is
    given, smart-turn defaults *on* for the local-microphone transport
    only: a local dev setup is the surface that most benefits from
    confident endpointing, and the bundled ONNX model is warmed up at
    startup so the first turn doesn't cold-stall.  Server and telephony
    presets keep smart-turn off by default — they pick their own
    endpointing strategy and shouldn't pay the warmup cost implicitly.

    A provider-side-endpointing STT (Deepgram Flux, Cartesia ink-2, or
    ElevenLabs realtime with the built-in VAD commit strategy) is the
    exception on the mic preset: it does its own endpointing, so smart-turn
    (and the Silero VAD it would pull in) stays off by default and the
    provider's native end-of-turn signal drives turns.  An explicit
    ``smart_turn=True`` / ``SmartTurnConfig`` still wins.
    """
    if isinstance(smart_turn, bool):
        if not smart_turn and sensitivity is not None:
            raise ValueError("smart_turn_sensitivity requires smart_turn=True.")
        config = SmartTurnConfig(enabled=smart_turn)
    elif smart_turn is None:
        enabled = sensitivity is not None or (
            isinstance(transport, LocalTransportConfig) and not stt_native_endpointing
        )
        config = SmartTurnConfig(enabled=enabled)
    elif isinstance(smart_turn, SmartTurnConfig):
        config = smart_turn
    else:
        raise ValueError("smart_turn must be a bool or SmartTurnConfig.")

    if sensitivity is None:
        return config

    value = _validate_probability_threshold("smart_turn_sensitivity", sensitivity)
    # Higher sensitivity means "treat lower completion probabilities as enough
    # to end the turn", so it maps inversely onto the provider threshold.
    return replace(config, enabled=True, threshold=1.0 - value)


def _inject_agent_runtime(
    agent: Any,
    *,
    mcp_servers: tuple[str, ...] | list[str] = (),
    agent_model: str | None = None,
    remote_agent_api_key: str | None = None,
) -> None:
    """Push session-level MCP/model/key settings into the bridge.

    Unwraps an ``AgentRunner`` to reach the inner bridge.  Does not
    return a new agent — applies settings to the bridge in place.

    Prefers the declared :meth:`ExternalAgentBridge.configure_runtime`
    surface when the bridge exposes it (every built-in bridge that
    consumes these settings does), so the wiring targets a documented
    contract instead of private attribute names.  Falls back to the
    historical private-attribute mutation for bridges that predate the
    method, keeping back-compat.
    """
    from easycat.integrations.agents.responses_api import RemoteResponsesAPIBridge

    inner = agent._agent if isinstance(agent, AgentRunner) else agent

    configure = getattr(inner, "configure_runtime", None)
    if callable(configure):
        # Always pass mcp_servers (even empty) so a bridge reused across
        # sessions doesn't leak a previous MCP list.
        configure(
            mcp_servers=list(mcp_servers),
            model=agent_model or None,
            api_key=remote_agent_api_key or None,
        )
        return

    # Back-compat path for bridges without configure_runtime.
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


# ── Telephony config dataclasses ─────────────────────────────────────


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

    def __post_init__(self) -> None:
        # ``detection_timeout_s`` flows into ``asyncio.sleep`` in the outbound
        # state machine with no runtime guard, so a non-positive value either
        # raises an uncaught ``ValueError`` (negative) or instantly
        # misclassifies the call (zero) — fail fast at construction instead.
        _require_positive("detection_timeout_s", self.detection_timeout_s)
        _require_non_negative("speech_threshold_ms", self.speech_threshold_ms)
        _require_non_negative("speech_end_threshold_ms", self.speech_end_threshold_ms)
        _require_non_negative("silence_timeout_ms", self.silence_timeout_ms)

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
    # Disabled by default: voicemail-to-human pickup requires explicit opt-in
    # and reliable inbound STT track metadata in the call state machine.
    voicemail_pickup_window_s: float = 0.0
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
        _require_positive("classification_gate_timeout_s", self.classification_gate_timeout_s)
        _require_positive("max_call_duration_s", self.max_call_duration_s)
        _require_positive("max_screening_turns", self.max_screening_turns)
        # The late/pickup windows are ``> 0``-guarded in the state machine
        # (a non-positive value simply disables the window), but reject
        # negatives for clarity since they are never meaningful.
        _require_non_negative("late_voicemail_window_s", self.late_voicemail_window_s)
        _require_non_negative("voicemail_pickup_window_s", self.voicemail_pickup_window_s)


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


# ── Session config dataclasses ───────────────────────────────────────


@dataclass(kw_only=True)
class _AgentSessionConfig:
    """Agent and journal fields shared by audio and text configs."""

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
    mcp_servers: list[str] | None = None
    debug: Literal["off", "light", "full"] = "light"
    journal_backend: Literal["sqlite", "sqlite+litestream", "libsql"] = "sqlite"
    journal_capacity: int = 10_000
    journal_redaction: Literal["secrets", "pii"] = "secrets"
    journal_retention: Literal["archive", "delete"] = "archive"
    warmup: bool = True
    debugger_autolaunch: bool = False
    capture_audio: bool | Callable[[], bool] = True
    capture_aec_reference: bool = False
    emergency_export: bool = False
    data_dir: str | Path | None = None


@dataclass(kw_only=True)
class EasyConfig(_AgentSessionConfig):
    """Top-level configuration for EasyCat sessions.

    Fields:
        stt / tts: Speech provider selection. Accepts provider shortcut
            strings (for example ``"deepgram/nova-2"``), concrete provider
            config dataclasses, or already-built provider instances that
            implement EasyCat's provider Protocols. Leave both unset with
            ``openai_api_key`` (or ``OPENAI_API_KEY``) to use the default
            OpenAI realtime STT + TTS chain.
        vad: ``VADConfig`` or a live ``VADProvider``.
        noise_reduction: ``NoiseReducerConfig`` or a live ``NoiseReducer``.
        echo_cancellation: ``EchoCancellationConfig`` or a live
            ``EchoCanceller``.
        smart_turn / smart_turn_sensitivity: Optional semantic end-of-turn
            detection.
        session_id: Optional caller-supplied runtime session id. When unset,
            EasyCat generates a ``session-...`` id.
        data_dir: Optional root for this session's journals and artifacts.
            When unset, the runtime falls back to ``EASYCAT_DATA_DIR`` or
            ``.easycat``.
        debug / journal_backend / journal_capacity / journal_redaction /
        journal_retention:
            Debug-journal settings.
        greeting / dnc_list / caller_id_exposure: Conversation and telephony
            policies.
        mcp_servers: Optional list of MCP server URIs to pass through to
            agent bridges.  Accepted schemes: ``stdio://``, ``sse://``,
            ``http://``, ``https://``.  Frozen per session — mid-session
            changes are not supported.
    """

    openai_api_key: str | None = None
    stt: STTConfig | STTProvider | str | None = None
    tts: TTSConfig | TTSProvider | str | None = None
    vad: VADConfig | VADProvider = field(default_factory=VADConfig)
    noise_reduction: NoiseReducerConfig | NoiseReducer | None = None
    echo_cancellation: EchoCancellationConfig | EchoCanceller | None = None
    enable_noise_reduction: bool = False
    enable_echo_cancellation: bool | None = None
    smart_turn: SmartTurnConfig | bool | None = None
    smart_turn_sensitivity: float | None = None
    transport: TransportConfig = field(default_factory=LocalTransportConfig)
    turn_taking: TurnManagerConfig = field(default_factory=TurnManagerConfig)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    telephony: TelephonyConfig | None = None
    strip_markdown: bool = False
    auto_align_tts_output_to_transport: bool = True
    output_processors: Sequence[LLMOutputProcessor] = ()
    session_actions: SessionActions | None = None
    action_executors: Sequence[SessionActionExecutor] = ()
    greeting: str | None = None
    dnc_list: DNCStore | None = None
    caller_id_exposure: Literal["off", "system_message", "tools_only"] = "tools_only"
    on_agent_failure: str | Callable[[Exception], str] | None = None
    session_id: str | None = None
    # When set, every session exports a timestamped debug bundle to this
    # directory on stop/shutdown — the "always be recording" flow so a
    # user who hits a real failure already has the bundle saved to disk
    # without flipping any switch.  Requires ``debug != "off"`` so the
    # journal actually exists.
    record_to: str | Path | None = None

    def __post_init__(self) -> None:
        _validate_common(
            debug=self.debug,
            journal_backend=self.journal_backend,
            journal_capacity=self.journal_capacity,
            journal_redaction=self.journal_redaction,
            journal_retention=self.journal_retention,
            mcp_servers=self.mcp_servers,
            session_id=self.session_id,
            agent=self.agent,
            agent_model=self.agent_model,
            capture_audio=self.capture_audio,
        )
        _validate_on_agent_failure(self.on_agent_failure)

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
        # ``openai_api_key`` is passed directly to the parser as a
        # per-call credential override, avoiding process-global
        # ``os.environ`` mutation during config construction.
        api_key_overrides = (
            {"OPENAI_API_KEY": self.openai_api_key} if self.openai_api_key else None
        )
        if isinstance(self.stt, str):
            self.stt = parse_stt_string(self.stt, api_key_overrides=api_key_overrides)
        if isinstance(self.tts, str):
            self.tts = parse_tts_string(self.tts, api_key_overrides=api_key_overrides)

        if self.openai_api_key:
            if self.stt is None:
                # Default to the Realtime WebSocket STT: audio is streamed
                # as it arrives (sub-second stop-to-final), versus the
                # batch ``/v1/audio/transcriptions`` endpoint which waits
                # for end-of-turn to upload the whole utterance.
                self.stt = OpenAIRealtimeSTTConfig(api_key=self.openai_api_key)
            if self.tts is None:
                self.tts = OpenAITTSConfig(api_key=self.openai_api_key)

        # Normalize smart-turn AFTER string STT shortcuts resolve to a typed
        # config.  The mic-preset default skips provider-side-endpointing STTs
        # (Deepgram Flux, Cartesia ink-2, ElevenLabs realtime VAD), which can
        # arrive either typed or as a ``"provider/model"`` string spec — only
        # recognizable once ``parse_stt_string`` has run above.  Computing it
        # before string resolution left the string form with smart-turn (and a
        # Silero VAD) on, diverging from the typed form.
        self.smart_turn = _normalize_smart_turn_config(
            self.smart_turn,
            sensitivity=self.smart_turn_sensitivity,
            transport=self.transport,
            stt_native_endpointing=_stt_uses_native_endpointing(self.stt),
        )

        # Catalog membership (not an isinstance against the built-in
        # ``TTSConfig`` union) so third-party configs registered via
        # ``register_tts_provider`` take the same alignment path.
        if is_tts_config(self.tts) and self.auto_align_tts_output_to_transport:
            from ._tts_alignment import align_tts_config_to_transport

            self.tts = align_tts_config_to_transport(cast(TTSConfig, self.tts), self.transport)
        if self.echo_cancellation is None:
            self.echo_cancellation = self._default_echo_cancellation_for_transport()
        elif self.enable_echo_cancellation is not None:
            if isinstance(self.echo_cancellation, EchoCancellationConfig):
                # Fold the explicit flag into the supplied config object so
                # ``enable_echo_cancellation`` is not silently dropped.
                self.echo_cancellation = replace(
                    self.echo_cancellation, enabled=self.enable_echo_cancellation
                )
            else:
                # Pre-built ``EchoCanceller`` instance: the flag cannot be
                # folded in, so warn on the conflict rather than ignore it.
                logger.warning(
                    "enable_echo_cancellation=%s ignored because a pre-built "
                    "EchoCanceller instance was supplied via echo_cancellation=",
                    self.enable_echo_cancellation,
                )
        if self.debug in ("light", "full"):
            self._apply_debug_defaults()
        self._warn_if_vad_pre_roll_is_too_short()
        self._validate()

    def _warn_if_vad_pre_roll_is_too_short(self) -> None:
        """Surface VAD/pre-roll combinations that can clip utterance onset."""
        if not isinstance(self.vad, VADConfig) or self.turn_taking.mode != TurnMode.VAD:
            return
        smart_turn_enabled = (
            isinstance(self.smart_turn, SmartTurnConfig) and self.smart_turn.enabled
        )
        voicemail_vad_enabled = bool(self.telephony and self.telephony.enable_voicemail_detector)
        if (
            _stt_uses_native_endpointing(self.stt)
            and not smart_turn_enabled
            and not voicemail_vad_enabled
        ):
            # This is the same native-endpointing path that
            # _should_auto_turn_from_stt_final() uses to disable EasyCat's VAD
            # stage. Neither pre-roll nor min_speech_duration participates.
            return
        required_ms = self.vad.min_speech_duration_ms + _MIN_VAD_PRE_ROLL_MARGIN_MS
        if self.turn_taking.pre_roll_ms >= required_ms:
            return
        logger.warning(
            "turn_taking.pre_roll_ms=%d is shorter than "
            "vad.min_speech_duration_ms=%d plus the %d ms onset margin; "
            "the start of each utterance may be clipped. Increase pre_roll_ms "
            "to at least %d or lower min_speech_duration_ms.",
            self.turn_taking.pre_roll_ms,
            self.vad.min_speech_duration_ms,
            _MIN_VAD_PRE_ROLL_MARGIN_MS,
            required_ms,
        )

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
        """Enable console logging when debug mode is active.

        ``EASYCAT_LOG_LEVEL`` (``debug|info|warning|error``) overrides
        the default level so users can keep ``debug="light"`` wiring on
        while dialling the log verbosity up or down without code
        changes — mirrors ``LIVEKIT_LOG_LEVEL`` / ``UVICORN_LOG_LEVEL``.
        The default is INFO (matching :func:`easycat.run`); DEBUG is only
        selected when ``EASYCAT_LOG_LEVEL`` explicitly requests it.
        """
        from easycat._logging import enable_console_logging

        enable_console_logging()
        level = logging.getLogger("easycat").level
        logger.debug("EasyCat debug mode enabled (level=%s)", logging.getLevelName(level))

    def _validate(self) -> None:
        # The #1 first-run mistake: no key resolved and nothing
        # configured.  Route it through the error catalog so the user
        # sees the missing env var (and its fix) instead of a symptom
        # they never touched.
        if (self.stt is None or self.tts is None) and not self.openai_api_key:
            raise EASYCAT_E203(var="OPENAI_API_KEY")
        if self.stt is None:
            raise ValueError("STT configuration is required.")
        if self.tts is None:
            raise ValueError("TTS configuration is required.")
        provider_configs: tuple[tuple[Any, Literal["STT", "TTS"]], ...] = (
            (self.stt, "STT"),
            (self.tts, "TTS"),
        )
        for cfg, kind in provider_configs:
            if hasattr(cfg, "api_key") and not cfg.api_key:
                # Keep the per-provider display-name ValueError here —
                # there is no (cfg, kind) -> env-var helper today, and the
                # None-branch fix above captures ~all of the leverage.
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
        """Local-microphone preset — the default developer setup.

        Next: pass ``stt=``/``tts=`` to swap providers by shortcut string,
        config dataclass, or provider instance. String/config providers need
        that provider's API key **and** its extra, e.g.
        ``stt="deepgram/nova-2"`` needs ``DEEPGRAM_API_KEY`` +
        ``easycat[deepgram]``. Pass ``vad=`` to pin or replace voice activity
        detection; use ``browser()``/``phone()`` to serve the same bot on
        another surface.
        """
        kwargs.setdefault("transport", LocalTransportConfig())
        return cls(**kwargs)

    @classmethod
    def browser(cls, **kwargs: Any) -> EasyConfig:
        """WebRTC-in-the-browser preset.

        Enables echo cancellation by default because browser clients
        loop transport audio back through the mic.

        Next: browser needs a server process + the ``easycat[webrtc]``
        extra — see ``examples/webrtc_server.py``.  Swapping ``stt=``/
        ``tts=`` accepts shortcut strings, config dataclasses, or provider
        instances; string/config providers need that provider's API key
        **and** its extra (e.g. ``stt="deepgram/nova-2"`` →
        ``DEEPGRAM_API_KEY`` + ``easycat[deepgram]``). Pass ``vad=`` to
        pin or replace voice activity detection.
        """
        kwargs.setdefault("transport", WebRTCTransportConfig())
        kwargs.setdefault("enable_echo_cancellation", True)
        return cls(**kwargs)

    @classmethod
    def phone(cls, **kwargs: Any) -> EasyConfig:
        """Inbound telephony preset.

        Uses the Twilio Media Streams transport and leaves echo-cancel
        on its tri-state default (off for PSTN, which has no loopback).

        Next: phone needs a server process + the ``easycat[telephony]``
        extra — see ``examples/twilio_app.py``.  Swapping ``stt=``/
        ``tts=`` accepts shortcut strings, config dataclasses, or provider
        instances; string/config providers need that provider's API key
        **and** its extra (e.g. ``stt="deepgram/nova-2"`` →
        ``DEEPGRAM_API_KEY`` + ``easycat[deepgram]``). Pass ``vad=`` to
        pin or replace voice activity detection.
        """
        kwargs.setdefault("transport", TwilioTransportConfig())
        return cls(**kwargs)


@dataclass(kw_only=True)
class TextSessionConfig(_AgentSessionConfig):
    """Configuration for a text-only Session (no audio pipeline).

    Mirrors the shared debug-journal and agent fields of :class:`EasyConfig`
    (both inherit :class:`_AgentSessionConfig`) so ``create_session`` and
    ``create_text_session`` accept a single config object of the
    ``create_*(config)`` shape. Audio-only fields
    (``stt``/``tts``/``vad``/``transport``/etc.) have no analogue here
    because text sessions never enter the audio pipeline.
    ``record_to=`` mirrors :class:`EasyConfig`: when debug journaling is
    enabled, clean teardown exports a timestamped debug bundle to that
    directory.

    Validated by the same :func:`_validate_common` as :class:`EasyConfig`.
    """

    session_id: str | None = None
    data_dir: str | Path | None = None
    # Match EasyConfig(record_to=...): text sessions can auto-export a
    # timestamped debug bundle on stop when debug journaling is enabled.
    record_to: str | Path | None = None

    def __post_init__(self) -> None:
        _validate_common(
            debug=self.debug,
            journal_backend=self.journal_backend,
            journal_capacity=self.journal_capacity,
            journal_redaction=self.journal_redaction,
            journal_retention=self.journal_retention,
            mcp_servers=self.mcp_servers,
            session_id=self.session_id,
            agent=self.agent,
            agent_model=self.agent_model,
            capture_audio=self.capture_audio,
        )

    @classmethod
    def from_kwargs(
        cls,
        config: TextSessionConfig | None,
        *,
        agent: Any = None,
        session_id: str | None = None,
        debug: Literal["off", "light", "full"] = "light",
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
    ) -> TextSessionConfig:
        """Resolve the config-or-loose-kwargs calling convention to one config.

        :func:`create_text_session` accepts either a fully-built
        ``TextSessionConfig`` or the legacy loose keyword arguments. The two
        forms are mutually exclusive: passing a ``config`` together with any
        non-default loose keyword raises :class:`ValueError`. Keeping the
        default table here, next to the dataclass fields it must track, keeps
        the factory body declarative and the field list maintained in one
        place.
        """
        if config is not None:
            loose = {
                "agent": (agent, None),
                "session_id": (session_id, None),
                "debug": (debug, "light"),
                "journal_backend": (journal_backend, "sqlite"),
                "journal_capacity": (journal_capacity, 10_000),
                "journal_redaction": (journal_redaction, "secrets"),
                "journal_retention": (journal_retention, "archive"),
                "warmup": (warmup, None),
                "wrap_agent": (wrap_agent, True),
                "agent_runner": (agent_runner, None),
                "agent_model": (agent_model, None),
                "remote_agent_api_key": (remote_agent_api_key, None),
                "mcp_servers": (mcp_servers, None),
                "record_to": (record_to, None),
                "capture_audio": (capture_audio, True),
                "data_dir": (data_dir, None),
                "emergency_export": (emergency_export, False),
            }
            supplied = [name for name, (value, default) in loose.items() if value != default]
            if supplied:
                raise ValueError(
                    "create_text_session() accepts either a TextSessionConfig or loose "
                    "keyword arguments, not both; remove the config argument or these "
                    f"keyword(s): {', '.join(sorted(supplied))}."
                )
            return config
        return cls(
            agent=agent,
            session_id=session_id,
            debug=debug,
            journal_backend=journal_backend,
            journal_capacity=journal_capacity,
            journal_redaction=journal_redaction,
            journal_retention=journal_retention,
            warmup=True if warmup is None else warmup,
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
