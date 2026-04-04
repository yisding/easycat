"""Session types: protocols, enums, and configuration dataclass."""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from easycat.bounded_queue import BoundedAudioQueue
from easycat.events import EventBus
from easycat.llm_output_processing import LLMOutputProcessor
from easycat.metrics import MetricsCollector
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
from easycat.tracing import Tracer
from easycat.turn_manager import TurnManager, TurnManagerConfig, TurnManagerState

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
    metrics: MetricsCollector | None = None
    tracer: Tracer | None = None
    outbound_queue: BoundedAudioQueue | None = None
    telephony_helpers: Sequence[SessionHelper] = ()
    audio_gate: Callable[[], bool] | None = None

    # Pipeline flags
    enable_noise_reduction: bool = True
    enable_echo_cancellation: bool = True
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
