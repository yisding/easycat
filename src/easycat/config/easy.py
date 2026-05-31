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

import logging
import os
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from easycat.echo_cancellation import EchoCancellationConfig
from easycat.errors import EASYCAT_E203
from easycat.integrations.agents._agent_runner import AgentRunner, AgentRunnerConfig
from easycat.llm_output_processing import LLMOutputProcessor
from easycat.noise_reduction import NoiseReducerConfig
from easycat.providers import Transport
from easycat.runtime.capabilities import default_echo_cancellation_enabled
from easycat.session.actions import SessionActionExecutor, SessionActions
from easycat.smart_turn import SmartTurnConfig
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
from easycat.transports.local import LocalTransportConfig
from easycat.transports.twilio_media import TwilioTransportConfig
from easycat.transports.webrtc import WebRTCTransportConfig
from easycat.transports.websocket import WebSocketTransportConfig
from easycat.transports.webtransport import WebTransportTransportConfig
from easycat.tts.factory import TTSConfig, parse_tts_string
from easycat.tts.openai_tts import OpenAITTSConfig
from easycat.turn_manager import TurnManagerConfig
from easycat.vad import VADConfig

if TYPE_CHECKING:
    # Annotation-only references to telephony runtime types. Kept out of the
    # module-level import set (which would re-trigger the telephony fan-out)
    # because ``from __future__ import annotations`` makes these lazy strings.
    from easycat.telephony.ivr import AgentCallback, DTMFDelivery
    from easycat.telephony.retry import RetryStrategyConfig
    from easycat.telephony.session_actions import TwilioSessionActionConfig

logger = logging.getLogger("easycat.config")


# ── OpenAI env-var / log-level helpers ───────────────────────────────


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
_VALID_JOURNAL_RETENTION = {"archive", "delete"}


def _require_positive(name: str, value: float) -> None:
    """Raise ``ValueError`` if ``value`` is not strictly positive."""
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _require_non_negative(name: str, value: float) -> None:
    """Raise ``ValueError`` if ``value`` is negative."""
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


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
    """Shared agent / journal / debug fields for both session configs.

    Extracted so :class:`EasyConfig` (audio sessions) and
    :class:`TextSessionConfig` (text-only sessions) declare the
    agent/journal/debug knobs once instead of copying them.

    Both this base and its subclasses are ``@dataclass(kw_only=True)``:
    a base dataclass injects its fields *before* the subclass's in the
    generated ``__init__``, so without ``kw_only`` a positional
    ``EasyConfig("sk-...")`` would silently mis-bind ``"sk-..."`` to
    ``agent`` instead of ``openai_api_key``.  Keyword-only construction
    makes that a loud ``TypeError`` and decouples the public field order
    from this internal split.
    """

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
    debug: Literal["off", "light", "full"] = "off"
    journal_backend: Literal["sqlite", "sqlite+litestream", "libsql"] = "sqlite"
    journal_retention: Literal["archive", "delete"] = "archive"
    mcp_servers: list[str] | None = None


@dataclass(kw_only=True)
class EasyConfig(_AgentSessionConfig):
    """Top-level configuration for EasyCat sessions.

    Fields:
        enable_noise_reduction: Opt-in noise reduction. Defaults to
            ``False``, so the out-of-the-box pipeline does **not** denoise
            mic input. Set ``True`` (or pass an explicit ``noise_reduction``
            config) to wire the reducer; note auto-mode still falls back to
            a passthrough reducer unless Krisp or RNNoise is installed.
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
    strip_markdown: bool = False
    auto_align_tts_output_to_transport: bool = True
    output_processors: Sequence[LLMOutputProcessor] = ()
    session_actions: SessionActions | None = None
    action_executors: Sequence[SessionActionExecutor] = ()
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
            from ._tts_alignment import align_tts_config_to_transport

            self.tts = align_tts_config_to_transport(self.tts, self.transport)
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
        for cfg, kind in ((self.stt, "STT"), (self.tts, "TTS")):
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

        Next: pass ``stt=``/``tts=`` to swap providers (each needs that
        provider's API key **and** its extra, e.g.
        ``stt="deepgram/nova-2"`` needs ``DEEPGRAM_API_KEY`` +
        ``easycat[deepgram]``); use ``browser()``/``phone()`` to serve
        the same bot on another surface.
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
        ``tts=`` providers needs that provider's API key **and** its
        extra (e.g. ``stt="deepgram/nova-2"`` → ``DEEPGRAM_API_KEY`` +
        ``easycat[deepgram]``).
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
        ``tts=`` providers needs that provider's API key **and** its
        extra (e.g. ``stt="deepgram/nova-2"`` → ``DEEPGRAM_API_KEY`` +
        ``easycat[deepgram]``).
        """
        kwargs.setdefault("transport", TwilioTransportConfig())
        return cls(**kwargs)


@dataclass(kw_only=True)
class TextSessionConfig(_AgentSessionConfig):
    """Configuration for a text-only Session (no audio pipeline).

    Mirrors the shared journal/debug/agent fields of :class:`EasyConfig`
    (both inherit :class:`_AgentSessionConfig`) so ``create_session`` and
    ``create_text_session`` accept a single config object of the
    ``create_*(config)`` shape. Audio-only fields
    (``stt``/``tts``/``vad``/``transport``/etc.) have no analogue here
    because text sessions never enter the audio pipeline.

    Validated by the same :func:`_validate_common` as :class:`EasyConfig`.
    """

    session_id: str | None = None

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

    @classmethod
    def from_kwargs(
        cls,
        config: TextSessionConfig | None,
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
                "debug": (debug, "off"),
                "journal_backend": (journal_backend, "sqlite"),
                "journal_retention": (journal_retention, "archive"),
                "wrap_agent": (wrap_agent, True),
                "agent_runner": (agent_runner, None),
                "agent_model": (agent_model, None),
                "remote_agent_api_key": (remote_agent_api_key, None),
                "mcp_servers": (mcp_servers, None),
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
            journal_retention=journal_retention,
            wrap_agent=wrap_agent,
            agent_runner=agent_runner,
            agent_model=agent_model,
            remote_agent_api_key=remote_agent_api_key,
            mcp_servers=mcp_servers,
        )
