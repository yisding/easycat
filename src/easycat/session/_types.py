"""Session types: protocols, enums, and configuration dataclass."""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from easycat._bounded_queue import BoundedAudioQueue
from easycat.events import EventBus
from easycat.llm_output_processing import LLMOutputProcessor
from easycat.providers import (
    EchoCanceller,
    NoiseReducer,
    STTProvider,
    Transport,
    TTSProvider,
    VADProvider,
)
from easycat.session.actions import SessionActionExecutor, SessionActions
from easycat.timeouts import TimeoutConfig
from easycat.turn_manager import TurnManager, TurnManagerConfig, TurnManagerState

if TYPE_CHECKING:
    from easycat.runtime.artifacts import ArtifactStore
    from easycat.runtime.journal import ExecutionJournal

# ── Agent protocol (lightweight — agent adapters provide real implementations) ──


@runtime_checkable
class Agent(Protocol):
    """Minimal agent interface: receive text, produce text."""

    async def run(self, text: str) -> str: ...


@runtime_checkable
class SessionHelper(Protocol):
    """Lifecycle-managed session helper component."""

    def start(self) -> None: ...

    def stop(self) -> None: ...


# ── Turn state ─────────────────────────────────────────────────────


class TurnState(enum.Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    BOT_SPEAKING = "bot_speaking"


# ── Call identity ──────────────────────────────────────────────────


CallDirection = Literal["inbound", "outbound", "unknown"]


@dataclass(frozen=True)
class CallIdentity:
    """Caller / callee metadata for a telephony session.

    Populated from the Twilio ``<Stream>`` ``customParameters`` for
    inbound calls and from :meth:`OutboundCallManager.place_call` for
    outbound calls.  Exposed to the agent and to tools via
    :attr:`Session.call_identity`; visibility to tools and the agent
    prompt is controlled by :attr:`SessionConfig.caller_id_exposure`.

    ``caller_number`` is the far-end (the human), ``called_number`` is
    the near-end (the bot's DID).  ``display_name`` is the optional
    caller-ID name if the carrier provided one.  ``city`` / ``state``
    / ``zip_code`` / ``country`` carry Twilio's geographic metadata
    (``FromCity`` / ``FromState`` / ``FromZip`` / ``FromCountry`` on
    the voice webhook) when the inbound TwiML forwards them.
    ``custom_fields`` carries extra metadata the transport or app
    attached (e.g. CRM account id, SIP headers).
    """

    caller_number: str = ""
    called_number: str = ""
    direction: CallDirection = "unknown"
    display_name: str | None = None
    call_sid: str | None = None
    city: str | None = None
    state: str | None = None
    zip_code: str | None = None
    country: str | None = None
    custom_fields: dict[str, str] = field(default_factory=dict)


CallerIdExposure = Literal["off", "system_message", "tools_only"]


# Mapping from TurnManagerState to the Session-level TurnState.
_TM_TO_TURN_STATE: dict[TurnManagerState, TurnState] = {
    TurnManagerState.IDLE: TurnState.IDLE,
    TurnManagerState.USER_SPEAKING: TurnState.LISTENING,
    TurnManagerState.USER_PAUSED: TurnState.LISTENING,
    TurnManagerState.PROCESSING: TurnState.PROCESSING,
    TurnManagerState.BOT_SPEAKING: TurnState.BOT_SPEAKING,
}


# ── Session configuration ─────────────────────────────────────────


@dataclass
class SessionConfig:
    """Configuration for a Session."""

    stt: STTProvider | None = None
    tts: TTSProvider | None = None
    vad: VADProvider | None = None
    noise_reducer: NoiseReducer | None = None
    echo_canceller: EchoCanceller | None = None
    transport: Transport | None = None
    agent: Agent | None = None
    event_bus: EventBus | None = None
    turn_manager: TurnManager | None = None
    turn_manager_config: TurnManagerConfig | None = None
    timeout_config: TimeoutConfig | None = None
    journal: ExecutionJournal | None = None
    artifact_store: ArtifactStore | None = None
    session_id: str | None = None
    outbound_queue: BoundedAudioQueue | None = None
    telephony_helpers: Sequence[SessionHelper] = ()
    audio_gate: Callable[[], bool] | None = None

    # Pipeline flags
    enable_noise_reduction: bool = False
    enable_echo_cancellation: bool = False
    enable_vad: bool = True
    auto_turn_from_stt_final: bool = False
    strip_markdown: bool = False
    output_processors: Sequence[LLMOutputProcessor] = ()

    # Interruption behaviour.
    # "truncate" (default): truncate the assistant message to what was
    #   actually spoken and append "..." — compatible with all models.
    # "message": append an explicit system/developer message noting the
    #   interruption — clearer intent but requires model support for
    #   interleaved system messages.
    interruption_mode: Literal["truncate", "message"] = "truncate"
    # Latency budget used when estimating the interruption point.  This can
    # account for transport/network + receiver playback + VAD/ASR detection
    # lag so we don't overestimate what the user actually heard.
    interruption_latency_compensation_ms: int = 0
    # If the newest playback ack before cutoff is older than this threshold,
    # treat acks as stale and allow a bounded heuristic tail.
    interruption_ack_stale_ms: int = 500
    # Maximum extra playout budget (beyond acked bytes) to allow via timing
    # heuristic when playback acks are stale.
    interruption_ack_tail_cap_ms: int = 500

    # Agent-initiated session actions.
    session_actions: SessionActions | None = None
    action_executors: Sequence[SessionActionExecutor] = ()

    # Runtime mode for the session.
    runtime_mode: Literal["chained_pipeline", "text_session"] = "chained_pipeline"
    # Text-mode context to pass through to stages (optional, set by create_text_session).
    text_mode_context: dict[str, Any] | None = None

    # MCP servers surfaced to the agent stage recorder so bridges can
    # read them from ``RecorderContext.mcp_servers``.
    mcp_servers: tuple[str, ...] = ()

    # Opt-out auto-handling on STT finals.
    #
    # When enabled, every STT final transcript is checked against
    # :data:`easycat.telephony.compliance.OPT_OUT_PHRASES`.  On a
    # match, Session:
    #   1. Emits an :class:`~easycat.events.OptOutDetected` event
    #      (with the matched phrase and the caller's number).
    #   2. If :attr:`dnc_list` is set, adds ``call_identity.caller_number``
    #      to it so the number is blocked on subsequent ``place_call``
    #      attempts through ``OutboundCallManager``.
    #   3. If :attr:`session_actions` is set, enqueues an
    #      :class:`~easycat.session.actions.EndCallAction` with
    #      ``reason="opt_out"`` so the call terminates gracefully
    #      after the agent's current utterance finishes.
    #
    # Apps that want a custom policy can set
    # ``opt_out_detection=False`` and subscribe to ``STTFinal`` /
    # ``OptOutDetected`` themselves.
    opt_out_detection: bool = True
    opt_out_phrases: tuple[str, ...] | None = None
    dnc_list: Any | None = None

    # Greeting synthesized automatically on the first
    # :class:`~easycat.events.CallAnswered` so the bot speaks first —
    # the canonical pattern for outbound calls and a common ask for
    # inbound.  ``None`` (default) leaves the old behaviour (the agent
    # speaks only after the first user utterance).  The greeting text
    # flows through the configured TTS provider; callers that want
    # per-call templating (e.g. ``"Hi {caller_name}"``) can pass an
    # f-string formatted against ``session.call_identity``.
    greeting: str | None = None

    # Telephony caller / callee identity and exposure policy.
    #
    # ``call_identity`` carries the inbound caller's phone number
    # (and/or the outbound callee's) plus direction + optional carrier
    # metadata.  Transports populate it: ``TwilioTransport`` reads
    # ``start.customParameters`` on the ``<Stream>`` event,
    # ``OutboundCallManager.place_call`` sets direction="outbound"
    # with the dialed number.
    #
    # ``caller_id_exposure`` controls who sees it:
    #   - "system_message" injects a short system note into the agent's
    #     per-turn context ("The caller's phone number is +1…"). Good
    #     when the agent must reason about who's on the line (e.g.
    #     name checks, greetings by first name).
    #   - "tools_only" (default) keeps the identity off the LLM prompt
    #     but available to tool code via ``session.call_identity``.
    #     Right for PII-sensitive workflows.
    #   - "off" hides it from both layers.
    #     Internal telephony policy hooks still retain the private
    #     identity for DNC/opt-out handling.
    call_identity: CallIdentity | None = None
    caller_id_exposure: CallerIdExposure = "tools_only"
