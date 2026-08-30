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
import math
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypedDict, Unpack, cast

from easycat._credentials import has_usable_credential
from easycat._numeric import is_finite_number
from easycat._session_id import validate_session_id
from easycat.echo_cancellation import (
    EchoCancellationConfig,
    is_echo_canceller_config,
    parse_echo_canceller_string,
)
from easycat.errors import EASYCAT_E203, EasyCatError, EasyConfigError
from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
from easycat.llm_output_processing import LLMOutputProcessor
from easycat.noise_reduction import NoiseReducerConfig, parse_noise_reducer_string
from easycat.providers import (
    EchoCanceller,
    NoiseReducer,
    STTProvider,
    Transport,
    TTSProvider,
    VADProvider,
)
from easycat.runtime.capabilities import default_echo_cancellation_enabled
from easycat.session._types import _validate_caller_id_exposure
from easycat.session.actions import SessionActionExecutor, SessionActions
from easycat.smart_turn import SmartTurnConfig, _validate_probability_threshold
from easycat.stt.factory import STTConfig, STTProviderConfig, parse_stt_string
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
from easycat.transports.telnyx_media import TelnyxTransportConfig
from easycat.transports.twilio_media import TwilioTransportConfig
from easycat.transports.websocket import WebSocketTransportConfig
from easycat.transports.webtransport import WebTransportTransportConfig
from easycat.tts.factory import TTSConfig, TTSProviderConfig, is_tts_config, parse_tts_string
from easycat.tts.openai_tts import OpenAITTSConfig
from easycat.turn_manager import TurnManagerConfig, TurnMode
from easycat.vad import VADConfig, parse_vad_string

if TYPE_CHECKING:
    # Annotation-only references to telephony runtime types. Kept out of the
    # module-level import set (which would re-trigger the telephony fan-out)
    # because ``from __future__ import annotations`` makes these lazy strings.
    from easycat.telephony.compliance import DNCStore
    from easycat.telephony.ivr import AgentCallback, DTMFDelivery
    from easycat.telephony.retry import RetryStrategyConfig
    from easycat.telephony.session_actions import (
        TelnyxSessionActionConfig,
        TwilioSessionActionConfig,
    )

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


_VALID_MCP_SCHEMES = ("stdio://", "sse://", "http://", "https://")
_VALID_DEBUG = {"off", "light", "full"}
_VALID_HANDLER_ERROR_POLICY = {"continue", "raise"}
_VALID_JOURNAL_BACKEND = {"sqlite", "sqlite+litestream", "libsql"}
_VALID_JOURNAL_REDACTION = {"secrets", "pii"}
_VALID_JOURNAL_RETENTION = {"archive", "delete"}
_VALID_VOICEMAIL_MODES = {"detect", "detect_end_of_greeting"}


def _require_positive(name: str, value: object) -> None:
    """Require a finite built-in number greater than zero."""
    if not is_finite_number(value) or value <= 0:
        raise EasyConfigError(f"{name} must be positive and finite")


def _require_non_negative(name: str, value: object) -> None:
    """Require a finite built-in number greater than or equal to zero."""
    if not is_finite_number(value) or value < 0:
        raise EasyConfigError(f"{name} must be non-negative and finite")


def _require_positive_integer(name: str, value: object) -> None:
    """Require a positive integer suitable for a counter or timer."""
    if not is_finite_number(value) or not isinstance(value, int) or value <= 0:
        raise EasyConfigError(f"{name} must be positive and a finite integer")


def _require_non_negative_integer(name: str, value: object) -> None:
    """Require a non-negative integer suitable for a provider parameter."""
    if not is_finite_number(value) or not isinstance(value, int) or value < 0:
        raise EasyConfigError(f"{name} must be non-negative and a finite integer")


def _require_boolean(name: str, value: object) -> None:
    if not isinstance(value, bool):
        raise EasyConfigError(f"{name} must be a boolean")


def _validate_on_agent_failure(
    policy: str | Callable[[Exception], str] | None,
) -> None:
    if policy is not None and not (isinstance(policy, str) or callable(policy)):
        raise EasyConfigError("on_agent_failure must be text, a callable, or None")
    if isinstance(policy, str) and not policy.strip():
        raise EasyConfigError("on_agent_failure text must not be empty")


def _validate_capture_audio(policy: bool | Callable[[], bool]) -> None:
    if not isinstance(policy, bool) and not callable(policy):
        raise EasyConfigError("capture_audio must be a bool or zero-argument callable")
    predicate_call = type(policy).__call__ if callable(policy) else None
    if callable(policy) and (
        inspect.iscoroutinefunction(policy) or inspect.iscoroutinefunction(predicate_call)
    ):
        raise EasyConfigError("capture_audio predicate must be synchronous")


def _validate_journal_capacity(capacity: int) -> None:
    if not isinstance(capacity, int) or isinstance(capacity, bool) or capacity <= 0:
        raise EasyConfigError("journal_capacity must be a positive integer")


def _validate_journal_redaction(policy: str) -> None:
    if not isinstance(policy, str) or policy not in _VALID_JOURNAL_REDACTION:
        raise EasyConfigError(
            f"Invalid journal_redaction={policy!r}. "
            f"Must be one of {sorted(_VALID_JOURNAL_REDACTION)}."
        )


def _validate_event_dispatch(
    slow_handler_threshold_s: float | None,
    handler_error_policy: str,
) -> None:
    if slow_handler_threshold_s is not None and (
        isinstance(slow_handler_threshold_s, bool)
        or not isinstance(slow_handler_threshold_s, int | float)
        or not math.isfinite(slow_handler_threshold_s)
        or slow_handler_threshold_s < 0
    ):
        raise EasyConfigError("slow_handler_threshold_s must be non-negative and finite")
    if (
        not isinstance(handler_error_policy, str)
        or handler_error_policy not in _VALID_HANDLER_ERROR_POLICY
    ):
        raise EasyConfigError(
            f"Invalid handler_error_policy={handler_error_policy!r}. "
            f"Must be one of {sorted(_VALID_HANDLER_ERROR_POLICY)}."
        )


def _validate_common(
    *,
    debug: str,
    slow_handler_threshold_s: float | None,
    handler_error_policy: str,
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
    if not isinstance(debug, str) or debug not in _VALID_DEBUG:
        raise EasyConfigError(f"Invalid debug={debug!r}. Must be one of {sorted(_VALID_DEBUG)}.")
    _validate_event_dispatch(slow_handler_threshold_s, handler_error_policy)
    if not isinstance(journal_backend, str) or journal_backend not in _VALID_JOURNAL_BACKEND:
        raise EasyConfigError(
            f"Invalid journal_backend={journal_backend!r}. "
            f"Must be one of {sorted(_VALID_JOURNAL_BACKEND)}."
        )
    _validate_journal_capacity(journal_capacity)
    _validate_journal_redaction(journal_redaction)
    if not isinstance(journal_retention, str) or journal_retention not in _VALID_JOURNAL_RETENTION:
        raise EasyConfigError(
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
        try:
            validate_session_id(session_id)
        except ValueError as exc:
            raise EasyConfigError(str(exc)) from exc
    if isinstance(agent, str):
        from urllib.parse import urlparse

        parsed = urlparse(agent)
        if parsed.scheme in ("http", "https") and parsed.netloc and agent_model is None:
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

    The answer comes from the open STT provider catalog, so third-party
    providers can declare the same ``native_endpointing`` capability as the
    built-ins instead of falling through a closed config-type check.
    """
    from easycat.stt.factory import _CATALOG

    return "native_endpointing" in _CATALOG.capabilities_for_config(stt)


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
            raise EasyConfigError("smart_turn_sensitivity requires smart_turn=True.")
        config = SmartTurnConfig(enabled=smart_turn)
    elif smart_turn is None:
        enabled = sensitivity is not None or (
            isinstance(transport, LocalTransportConfig) and not stt_native_endpointing
        )
        config = SmartTurnConfig(enabled=enabled)
    elif isinstance(smart_turn, SmartTurnConfig):
        if not smart_turn.enabled and sensitivity is not None:
            raise EasyConfigError("smart_turn_sensitivity requires smart_turn=True.")
        config = smart_turn
    else:
        raise EasyConfigError("smart_turn must be a bool or SmartTurnConfig.")

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
    if isinstance(cfg, STTProviderConfig | TTSProviderConfig):
        return f"{cfg.provider} {kind}"
    if kind == "STT":
        from easycat.stt.factory import _CATALOG as catalog
    else:
        from easycat.tts.factory import _CATALOG as catalog

    cfg_type = type(cfg)
    for provider_name, (_provider_cls, config_cls) in catalog.providers.items():
        if config_cls is cfg_type:
            return f"{provider_name} {kind}"
    return type(cfg).__name__.replace("Config", "")


def _provider_requires_api_key(cfg: Any, kind: Literal["STT", "TTS"]) -> bool:
    """Consult the open catalog instead of assuming every ``api_key`` field is required."""
    if kind == "STT":
        from easycat.stt.factory import _CATALOG as catalog
    else:
        from easycat.tts.factory import _CATALOG as catalog

    catalog.discover()
    if isinstance(cfg, STTProviderConfig | TTSProviderConfig):
        provider_name = catalog.validate_name(cfg.provider)
        return catalog.env_vars[provider_name] is not None
    cfg_type = type(cfg)
    for provider_name, (_provider_cls, config_cls) in catalog.providers.items():
        if config_cls is cfg_type:
            return catalog.env_vars[provider_name] is not None
    # Preserve the historical conservative behavior for unknown custom config
    # objects while allowing registered keyless providers through.
    return hasattr(cfg, "api_key")


def _resolve_named_provider_config(
    config: STTProviderConfig | TTSProviderConfig,
    kind: Literal["STT", "TTS"],
    api_key_overrides: dict[str, str] | None,
) -> Any:
    """Resolve a named wrapper to its concrete config without creating a client."""
    if kind == "STT":
        from easycat.stt.factory import _CATALOG as catalog
    else:
        from easycat.tts.factory import _CATALOG as catalog
    provider_name = catalog.validate_name(config.provider)
    _provider_cls, config_cls = catalog.providers[provider_name]
    kwargs = dict(config.params or {})
    env_var = catalog.env_vars[provider_name]
    resolved_key = config.api_key
    if not has_usable_credential(resolved_key) and env_var is not None:
        resolved_key = (api_key_overrides or {}).get(env_var) or os.getenv(env_var)
    if has_usable_credential(resolved_key):
        kwargs["api_key"] = resolved_key
    try:
        return config_cls(**kwargs)
    except EasyCatError:
        raise
    except (TypeError, ValueError) as exc:
        raise EasyConfigError(
            f"Invalid params for {provider_name!r} {kind} provider: {exc}"
        ) from exc


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
        self.validate()

    def validate(self) -> None:
        """Validate mutable answering-machine policy before provider dispatch."""
        if not isinstance(self.mode, str) or self.mode not in _VALID_VOICEMAIL_MODES:
            raise EasyConfigError(
                f"Invalid voicemail_detection.mode={self.mode!r}. "
                f"Must be one of {sorted(_VALID_VOICEMAIL_MODES)}."
            )
        _require_boolean("async_mode", self.async_mode)
        # ``detection_timeout_s`` flows into ``asyncio.sleep`` in the outbound
        # state machine with no runtime guard, so a non-positive value either
        # raises an uncaught ``ValueError`` (negative) or instantly
        # misclassifies the call (zero) — fail fast at construction instead.
        _require_positive_integer("detection_timeout_s", self.detection_timeout_s)
        _require_non_negative_integer("speech_threshold_ms", self.speech_threshold_ms)
        _require_non_negative_integer("speech_end_threshold_ms", self.speech_end_threshold_ms)
        _require_non_negative_integer("silence_timeout_ms", self.silence_timeout_ms)

    def to_twilio_params(self) -> dict[str, Any]:
        """Render as the kwargs :class:`OutboundCallManager` expects today."""
        # The dataclass is intentionally mutable for back-compat. Revalidate at
        # this external-policy boundary so a post-construction typo cannot fall
        # through to Twilio's more aggressive ``Enable`` mode.
        self.validate()
        twilio_mode = "DetectMessageEnd" if self.mode == "detect_end_of_greeting" else "Enable"
        return {
            "amd_mode": twilio_mode,
            "async_amd": self.async_mode,
            "amd_timeout": self.detection_timeout_s,
            "speech_threshold": self.speech_threshold_ms,
            "speech_end_threshold": self.speech_end_threshold_ms,
            "silence_timeout": self.silence_timeout_ms,
        }

    def to_telnyx_params(self) -> dict[str, Any]:
        """Render as Telnyx ``answering_machine_detection`` dial parameters."""
        # Revalidate at this external-policy boundary, mirroring
        # ``to_twilio_params``.
        self.validate()
        telnyx_mode = "greeting_end" if self.mode == "detect_end_of_greeting" else "detect"
        return {"answering_machine_detection": telnyx_mode}


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
    provider: Literal["twilio", "telnyx"] = "twilio"
    telnyx_api_key: str = field(default="", repr=False)
    telnyx_connection_id: str = ""
    telnyx_webhook_url: str = ""
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
        self.validate()

    def validate(self) -> None:
        """Revalidate mutable outbound policy before runtime wiring."""
        if not isinstance(self.voicemail_detection, VoicemailDetectionConfig):
            raise EasyConfigError(
                "voicemail_detection must be a VoicemailDetectionConfig instance."
            )
        self.voicemail_detection.validate()
        if self.provider not in ("twilio", "telnyx"):
            raise EasyConfigError(
                f"Invalid outbound provider={self.provider!r}. Must be 'twilio' or 'telnyx'."
            )
        if self.provider == "telnyx" and not self.telnyx_connection_id:
            raise EasyConfigError(
                "outbound.telnyx_connection_id is required when provider='telnyx'."
            )
        if self.provider == "telnyx" and not self.telnyx_api_key:
            raise EasyConfigError(
                "outbound.telnyx_api_key is required when provider='telnyx'."
            )
        if self.retry_strategy is not None:
            from easycat.telephony.retry import RetryStrategyConfig

            if not isinstance(self.retry_strategy, RetryStrategyConfig):
                raise EasyConfigError(
                    "retry_strategy must be a RetryStrategyConfig instance or None."
                )
            try:
                self.retry_strategy.validate()
            except ValueError as exc:
                raise EasyConfigError(f"Invalid retry_strategy: {exc}") from exc
        for name in (
            "enable_screening_detection",
            "screening_use_agent",
            "enable_realtime_transcription",
            "classification_gate",
            "enable_number_health",
            "enable_disposition_tracker",
            "enable_retry_strategy",
        ):
            _require_boolean(name, getattr(self, name))
        if not self.from_number or not self.from_number.strip():
            raise EasyConfigError(
                "outbound.from_number must be a non-empty E.164 number."
            )
        _require_positive("classification_gate_timeout_s", self.classification_gate_timeout_s)
        _require_positive("max_call_duration_s", self.max_call_duration_s)
        _require_positive_integer("max_screening_turns", self.max_screening_turns)
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
    telnyx_actions: TelnyxSessionActionConfig | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Revalidate mutable telephony switches and nested outbound policy."""
        for name in (
            "enable_dtmf_aggregator",
            "enable_voicemail_detector",
            "enable_outbound_call_manager",
        ):
            _require_boolean(name, getattr(self, name))
        if self.enable_outbound_call_manager and self.outbound is None:
            raise EasyConfigError(
                "telephony.outbound is required when enable_outbound_call_manager=True."
            )
        if self.telnyx_actions is not None:
            from easycat.telephony.session_actions import TelnyxSessionActionConfig

            if not isinstance(self.telnyx_actions, TelnyxSessionActionConfig):
                raise EasyConfigError(
                    "telephony.telnyx_actions must be a TelnyxSessionActionConfig instance."
                )
        if self.twilio_actions is not None:
            from easycat.telephony.session_actions import TwilioSessionActionConfig

            if not isinstance(self.twilio_actions, TwilioSessionActionConfig):
                raise EasyConfigError(
                    "telephony.twilio_actions must be a TwilioSessionActionConfig instance."
                )
        if self.outbound is None:
            return
        if not isinstance(self.outbound, OutboundCallConfig):
            raise EasyConfigError("telephony.outbound must be an OutboundCallConfig instance.")
        self.outbound.validate()


TransportConfig = (
    LocalTransportConfig
    | WebSocketTransportConfig
    | TwilioTransportConfig
    | TelnyxTransportConfig
    | WebRTCTransportConfig
    | WebTransportTransportConfig
    | Transport
)


# ── Session config dataclasses ───────────────────────────────────────


class _AgentSessionPresetKwargs(TypedDict, total=False):
    """Typed keyword surface shared by the audio-session presets.

    Keep this in field order with :class:`_AgentSessionConfig`. The mapping is
    used only for static checking; preset implementations still forward the
    original keyword dictionary into the dataclass constructor unchanged.
    """

    agent: Any
    agent_model: str | None
    remote_agent_api_key: str | None
    agent_runner: AgentRunnerConfig | None
    wrap_agent: bool
    mcp_servers: list[str] | None
    debug: Literal["off", "light", "full"]
    slow_handler_threshold_s: float | None
    handler_error_policy: Literal["continue", "raise"]
    journal_backend: Literal["sqlite", "sqlite+litestream", "libsql"]
    journal_capacity: int
    journal_redaction: Literal["secrets", "pii"]
    journal_retention: Literal["archive", "delete"]
    warmup: bool
    debugger_autolaunch: bool
    capture_audio: bool | Callable[[], bool]
    capture_aec_reference: bool
    emergency_export: bool
    data_dir: str | Path | None


class _EasyConfigPresetKwargs(_AgentSessionPresetKwargs, total=False):
    """Every keyword accepted by ``EasyConfig.mic/browser/phone``.

    Provider-bearing fields deliberately remain ``Any``. Third-party provider
    config classes are discovered at runtime and therefore cannot be represented
    by EasyCat's closed built-in config unions. All finite policy and scalar
    fields stay precise so editors can complete them and type checkers can catch
    misspellings, invalid literals, and wrong value types before startup.
    """

    openai_api_key: str | None
    stt: Any
    tts: Any
    vad: Any
    noise_reduction: Any
    echo_cancellation: Any
    enable_noise_reduction: bool
    enable_echo_cancellation: bool | None
    smart_turn: SmartTurnConfig | bool | None
    smart_turn_sensitivity: float | None
    transport: TransportConfig
    turn_taking: TurnManagerConfig
    timeouts: TimeoutConfig
    telephony: TelephonyConfig | None
    strip_markdown: bool
    auto_align_tts_output_to_transport: bool
    output_processors: Sequence[LLMOutputProcessor]
    session_actions: SessionActions | None
    action_executors: Sequence[SessionActionExecutor]
    greeting: str | None
    dnc_list: DNCStore | None
    caller_id_exposure: Literal["off", "system_message", "tools_only"]
    on_agent_failure: str | Callable[[Exception], str] | None
    session_id: str | None
    record_to: str | Path | None


@dataclass(kw_only=True)
class _AgentSessionConfig:
    """Agent and journal fields shared by audio and text configs."""

    agent: Any = None
    agent_model: str | None = None
    remote_agent_api_key: str | None = field(default=None, repr=False)
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
    slow_handler_threshold_s: float | None = 0.005
    handler_error_policy: Literal["continue", "raise"] = "continue"
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

    def _validate_common_fields(self) -> None:
        """Revalidate mutable fields at each public session-build boundary."""
        _validate_common(
            debug=self.debug,
            slow_handler_threshold_s=self.slow_handler_threshold_s,
            handler_error_policy=self.handler_error_policy,
            journal_backend=self.journal_backend,
            journal_capacity=self.journal_capacity,
            journal_redaction=self.journal_redaction,
            journal_retention=self.journal_retention,
            mcp_servers=self.mcp_servers,
            session_id=getattr(self, "session_id", None),
            agent=self.agent,
            agent_model=self.agent_model,
            capture_audio=self.capture_audio,
        )


@dataclass(kw_only=True)
class EasyConfig(_AgentSessionConfig):
    """Top-level configuration for EasyCat sessions.

    Fields:
        stt / tts: Speech provider selection. Accepts provider shortcut
            strings (for example ``"deepgram/nova-2"``), concrete provider
            config dataclasses, named ``STTProviderConfig`` /
            ``TTSProviderConfig`` wrappers, or already-built provider instances
            that implement EasyCat's provider Protocols. Leave both unset with
            ``openai_api_key`` (or ``OPENAI_API_KEY``) to use the default OpenAI
            realtime STT + TTS chain.
        vad: A built-in/registered shortcut string, ``VADConfig``, registered
            config, or live ``VADProvider``.
        noise_reduction: A built-in/registered shortcut string,
            ``NoiseReducerConfig``, registered config, or live ``NoiseReducer``.
        echo_cancellation: A built-in/registered shortcut string,
            ``EchoCancellationConfig``, registered config, or live
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
        slow_handler_threshold_s / handler_error_policy: Inline EventBus
            diagnostics and failure handling.
        greeting / dnc_list / caller_id_exposure: Conversation and telephony
            policies.
        mcp_servers: Optional list of MCP server URIs to pass through to
            agent bridges.  Accepted schemes: ``stdio://``, ``sse://``,
            ``http://``, ``https://``.  Frozen per session — mid-session
            changes are not supported.
    """

    openai_api_key: str | None = field(default=None, repr=False)
    stt: STTConfig | STTProviderConfig | STTProvider | str | None = None
    tts: TTSConfig | TTSProviderConfig | TTSProvider | str | None = None
    vad: VADConfig | VADProvider | str = field(default_factory=VADConfig)
    noise_reduction: NoiseReducerConfig | NoiseReducer | str | None = None
    echo_cancellation: EchoCancellationConfig | EchoCanceller | str | None = None
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
        self._validate_common_fields()
        _validate_on_agent_failure(self.on_agent_failure)

        # Pick up OPENAI_API_KEY for the zero-config case so a bare
        # ``EasyConfig(agent=...)`` works when the env var is set —
        # the standard OpenAI SDK convention.  Resolved before string
        # parsing so ``stt="openai-realtime"`` honors the env var
        # without needing to be passed explicitly.
        if self.openai_api_key is None:
            env_key = os.getenv("OPENAI_API_KEY")
            if has_usable_credential(env_key):
                self.openai_api_key = env_key

        # Resolve string-keyed provider shortcuts ("deepgram/flux" →
        # DeepgramSTTConfig(...)) before any downstream validation. Typed
        # configs still take precedence — users can pass a concrete
        # DeepgramSTTConfig and keep full control. A programmatic
        # ``openai_api_key`` is passed directly to the parser as a
        # per-call credential override, avoiding process-global
        # ``os.environ`` mutation during config construction.
        api_key_overrides = (
            {"OPENAI_API_KEY": self.openai_api_key}
            if has_usable_credential(self.openai_api_key)
            else None
        )
        self._resolve_provider_shortcuts(api_key_overrides)

        if has_usable_credential(self.openai_api_key):
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
            elif is_echo_canceller_config(self.echo_cancellation):
                logger.warning(
                    "enable_echo_cancellation=%s ignored because a registered "
                    "echo-canceller config was supplied via echo_cancellation=",
                    self.enable_echo_cancellation,
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

    def _validate_for_session(self) -> None:
        """Validate mutable config immediately before allocating session resources."""
        self._validate_common_fields()
        _validate_on_agent_failure(self.on_agent_failure)
        if not isinstance(self.timeouts, TimeoutConfig):
            raise EasyConfigError("timeouts must be a TimeoutConfig instance.")
        self.timeouts.validate()
        if self.agent is None:
            raise EasyConfigError(
                "agent is required for an audio session. Pass agent=... to EasyConfig."
            )
        api_key_overrides = (
            {"OPENAI_API_KEY": self.openai_api_key}
            if has_usable_credential(self.openai_api_key)
            else None
        )
        self._resolve_provider_shortcuts(api_key_overrides)
        # Recompute smart_turn after STT shortcut resolution so a post-construction
        # stt mutation (e.g. to a native-endpointing provider) does not leave double
        # endpointing on (gh 1027).
        self.smart_turn = _normalize_smart_turn_config(
            self.smart_turn,
            sensitivity=self.smart_turn_sensitivity,
            transport=self.transport,
            stt_native_endpointing=_stt_uses_native_endpointing(self.stt),
        )
        self.turn_taking.validate()
        if self.telephony is not None:
            if not isinstance(self.telephony, TelephonyConfig):
                raise EasyConfigError("telephony must be a TelephonyConfig instance or None.")
            self.telephony.validate()
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

    def _resolve_provider_shortcuts(self, api_key_overrides: dict[str, str] | None) -> None:
        """Resolve every named audio-stage provider before validation/planning."""
        if isinstance(self.stt, str):
            self.stt = parse_stt_string(self.stt, api_key_overrides=api_key_overrides)
        elif isinstance(self.stt, STTProviderConfig):
            self.stt = _resolve_named_provider_config(
                self.stt,
                "STT",
                api_key_overrides,
            )
        if isinstance(self.tts, str):
            self.tts = parse_tts_string(self.tts, api_key_overrides=api_key_overrides)
        elif isinstance(self.tts, TTSProviderConfig):
            self.tts = _resolve_named_provider_config(
                self.tts,
                "TTS",
                api_key_overrides,
            )
        if isinstance(self.vad, str):
            self.vad = parse_vad_string(self.vad)
        if isinstance(self.noise_reduction, str):
            self.noise_reduction = parse_noise_reducer_string(self.noise_reduction)
        if isinstance(self.echo_cancellation, str):
            self.echo_cancellation = parse_echo_canceller_string(self.echo_cancellation)

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
        try:
            _validate_caller_id_exposure(self.caller_id_exposure)
        except ValueError as exc:
            raise EasyConfigError(str(exc)) from exc
        # The #1 first-run mistake: no key resolved and nothing
        # configured.  Route it through the error catalog so the user
        # sees the missing env var (and its fix) instead of a symptom
        # they never touched.
        if (self.stt is None or self.tts is None) and not has_usable_credential(
            self.openai_api_key
        ):
            raise EASYCAT_E203(var="OPENAI_API_KEY")
        if self.stt is None:
            raise EasyConfigError("STT configuration is required.")
        if self.tts is None:
            raise EasyConfigError("TTS configuration is required.")
        provider_configs: tuple[tuple[Any, Literal["STT", "TTS"]], ...] = (
            (self.stt, "STT"),
            (self.tts, "TTS"),
        )
        for cfg, kind in provider_configs:
            if _provider_requires_api_key(cfg, kind) and not has_usable_credential(
                getattr(cfg, "api_key", None)
            ):
                # Also check ambient env var so typed configs match string/named-wrapper behavior (gh 1018).
                from easycat._credentials import has_usable_credential as _has_cred
                import os as _os

                env_ok = False
                try:
                    if kind == "STT":
                        from easycat.stt.factory import _CATALOG as _catalog
                    else:
                        from easycat.tts.factory import _CATALOG as _catalog

                    _catalog.discover()
                    # Resolve provider name for this cfg type
                    provider_name = None
                    if hasattr(cfg, "provider"):
                        try:
                            provider_name = _catalog.validate_name(cfg.provider)  # type: ignore[arg-type]
                        except Exception:
                            provider_name = None
                    if provider_name is None:
                        for pname, (_pcls, ccls) in _catalog.providers.items():
                            if ccls is type(cfg):
                                provider_name = pname
                                break
                    if provider_name is not None:
                        env_var = _catalog.env_vars.get(provider_name)
                        if env_var and _has_cred(_os.getenv(env_var)):
                            env_ok = True
                except Exception:
                    env_ok = False
                if env_ok:
                    # Inject ambient credential so create_session succeeds without string parsing (gh 1041 review).
                    try:
                        cfg.api_key = _os.getenv(env_var or "") or cfg.api_key  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    continue
                name = _provider_display_name(cfg, kind)
                raise EasyConfigError(f"{name} requires an API key.")

    # ── Factory presets ──────────────────────────────────────────
    #
    # Classmethod shortcuts that pick sensible transport defaults for
    # the three canonical deployment surfaces (local mic / browser /
    # phone) and the text REPL used for agent iteration.  Users can
    # still override any field via keyword argument — the preset only
    # fills the transport default when the caller didn't supply one.

    @classmethod
    def mic(cls, **kwargs: Unpack[_EasyConfigPresetKwargs]) -> EasyConfig:
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
    def browser(cls, **kwargs: Unpack[_EasyConfigPresetKwargs]) -> EasyConfig:
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
    def phone(
        cls,
        provider: Literal["twilio", "telnyx"] = "twilio",
        **kwargs: Unpack[_EasyConfigPresetKwargs],
    ) -> EasyConfig:
        """Inbound telephony preset.

        Uses the Twilio Media Streams transport by default (``provider="twilio"``);
        pass ``provider="telnyx"`` for the Telnyx media-streams transport. Echo-
        cancel stays on its tri-state default (off for PSTN, which has no
        loopback).

        Next: phone needs a server process + the ``easycat[telephony]``
        (Twilio) or ``easycat[telnyx]`` extra — see ``examples/twilio_app.py``.
        Swapping ``stt=``/``tts=`` accepts shortcut strings, config dataclasses,
        or provider instances; string/config providers need that provider's API
        key **and** its extra (e.g. ``stt="deepgram/nova-2"`` →
        ``DEEPGRAM_API_KEY`` + ``easycat[deepgram]``). Pass ``vad=`` to pin or
        replace voice activity detection.
        """
        kwargs.setdefault(
            "transport",
            TelnyxTransportConfig() if provider == "telnyx" else TwilioTransportConfig(),
        )
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
        self._validate_common_fields()

    def _validate_for_session(self) -> None:
        """Revalidate a possibly mutated config before allocating resources."""
        self._validate_common_fields()

    @classmethod
    def from_kwargs(
        cls,
        config: TextSessionConfig | None,
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
    ) -> TextSessionConfig:
        """Resolve the config-or-loose-kwargs calling convention to one config.

        :func:`create_text_session` accepts either a fully-built
        ``TextSessionConfig`` or the legacy loose keyword arguments. The two
        forms are mutually exclusive: passing a ``config`` together with any
        non-default loose keyword raises :class:`EasyConfigError`. Keeping the
        default table here, next to the dataclass fields it must track, keeps
        the factory body declarative and the field list maintained in one
        place.
        """
        if config is not None:
            loose = {
                "agent": (agent, None),
                "session_id": (session_id, None),
                "debug": (debug, "light"),
                "slow_handler_threshold_s": (slow_handler_threshold_s, 0.005),
                "handler_error_policy": (handler_error_policy, "continue"),
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
                raise EasyConfigError(
                    "create_text_session() accepts either a TextSessionConfig or loose "
                    "keyword arguments, not both; remove the config argument or these "
                    f"keyword(s): {', '.join(sorted(supplied))}."
                )
            return config
        return cls(
            agent=agent,
            session_id=session_id,
            debug=debug,
            slow_handler_threshold_s=slow_handler_threshold_s,
            handler_error_policy=handler_error_policy,
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
