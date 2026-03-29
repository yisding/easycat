"""Session: the core runtime for a single voice conversation.

Manages the voice pipeline lifecycle, wires provider stages together,
and handles turn state and cancellation. Supports both basic and
streaming agent interfaces with incremental TTS synthesis.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import re
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import uuid4

import pysbd

from easycat._span_manager import SpanManager
from easycat.agent_runner import AgentStreamEventType
from easycat.audio_format import AudioChunk
from easycat.bounded_queue import BoundedAudioQueue, DropPolicy
from easycat.cancel import CancelToken
from easycat.echo_cancellation import PassthroughAEC
from easycat.events import (
    AgentDelta,
    AgentFinal,
    AudioIn,
    Error,
    EventBus,
    EventHandler,
    Interruption,
    PlaybackMarkAck,
    ReconnectSuccess,
    STTEventType,
    STTFinal,
    STTPartial,
    ToolCallDelta,
    ToolCallResult,
    ToolCallStarted,
    TurnEnded,
    TurnStarted,
)
from easycat.health_check import PeriodicHealthChecker
from easycat.llm_output_processing import (
    LLMOutputProcessor,
    apply_output_processors,
)
from easycat.metrics import (
    AGENT_LATENCY,
    ERRORS,
    INTERRUPTIONS,
    RECONNECTS,
    STT_LATENCY,
    MetricsCollector,
)
from easycat.noise_reduction import PassthroughNoiseReducer
from easycat.providers import (
    EchoCanceller,
    NoiseReducer,
    PlaybackAckTransport,
    STTProvider,
    Transport,
    TTSProvider,
    VADProvider,
)
from easycat.strip_markdown import strip_markdown
from easycat.stubs import (
    NoopAgent,
    NoopSTT,
    NoopTransport,
    NoopTTS,
    NoopVAD,
)
from easycat.timeouts import (
    AgentTimeoutError,
    STTTimeoutError,
    TimeoutConfig,
    TTSTimeoutError,
    with_agent_timeout,
)
from easycat.tracing import SpanStatus, Tracer
from easycat.tts.input import TTSInput, strip_ssml_tags
from easycat.tts_synthesizer import TTSSynthesizer
from easycat.turn_manager import TurnManager, TurnManagerConfig, TurnManagerState

logger = logging.getLogger(__name__)

# Sentence boundary detection via pySBD.
_SENTENCE_SEGMENTER = pysbd.Segmenter(language="en", clean=False, char_span=True)


def _span_bounds(span: object) -> tuple[int, int]:
    if isinstance(span, tuple) and len(span) == 2:
        return span
    start = getattr(span, "start", None)
    end = getattr(span, "end", None)
    if start is None or end is None:
        raise TypeError(f"Unexpected span type from pySBD: {span!r}")
    return int(start), int(end)


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


# ── Helpers ────────────────────────────────────────────────────────


def _split_at_sentence_boundaries(text: str) -> tuple[str, str]:
    """Split text at the last sentence boundary.

    Returns (ready_text, remaining_buffer). ``ready_text`` contains complete
    sentences to send to TTS; ``remaining_buffer`` holds any trailing text
    that hasn't reached a sentence boundary yet.

    Only splits when pySBD detects multiple sentences — all but the last are
    returned as ready.  Single-span text is always buffered; the caller is
    responsible for flushing the final buffer when the LLM stream finishes.
    """
    spans = _SENTENCE_SEGMENTER.segment(text)
    if len(spans) <= 1:
        return "", text
    last_start, _ = _span_bounds(spans[-1])
    return text[:last_start], text[last_start:]


def _truncate_partial_text_to_boundary(text: str, chars: int) -> str:
    """Trim a partial text estimate to a safer boundary.

    If the proportional cut lands in the middle of a word, trim back to the
    nearest non-word boundary so interruption context looks less noisy.

    Note: ``_is_word_char`` treats all alphanumeric characters (including CJK
    and other non-Latin scripts) as word characters, so this function is
    effectively a no-op for languages without whitespace word boundaries.
    """
    if chars <= 0:
        return ""
    if chars >= len(text):
        return text

    prefix = text[:chars]
    next_char = text[chars]

    # If the cut already lands on a boundary (e.g. whitespace/punctuation),
    # keep the proportional estimate as-is.
    if (not _is_word_char(prefix[-1])) or (not _is_word_char(next_char)):
        return prefix

    # Otherwise trim back to the nearest safe boundary in the prefix.
    for i in range(len(prefix) - 1, -1, -1):
        if not _is_word_char(prefix[i]):
            return prefix[: i + 1]

    # Single long token with no internal boundary; keep the proportional cut.
    return prefix


def _estimate_text_spoken(
    tts_chunks: list[tuple[str, int]],
    audio_bytes_sent: int,
) -> str:
    """Estimate what text the user heard based on TTS audio delivered.

    Each entry in *tts_chunks* is ``(text, audio_bytes_produced)`` for a
    sentence-level TTS call.  *audio_bytes_sent* is the total audio bytes
    that were actually sent to the transport before the barge-in flush.

    Walks through the chunks in order, subtracting each chunk's audio from
    the bytes-sent budget.  When the budget runs out mid-chunk, the text is
    proportionally estimated (assumes roughly linear text → audio mapping
    within a single sentence — not perfect, but a practical heuristic).
    """
    if not tts_chunks or audio_bytes_sent <= 0:
        return ""

    remaining = audio_bytes_sent
    spoken = ""
    for chunk_text, chunk_audio in tts_chunks:
        if chunk_audio <= 0:
            # No audio produced for this chunk (e.g. cancelled before any
            # data) — skip.
            continue
        if remaining >= chunk_audio:
            # This entire chunk was delivered.
            spoken += chunk_text
            remaining -= chunk_audio
        else:
            # Partial chunk — estimate by fraction of audio delivered.
            fraction = remaining / chunk_audio
            chars = int(len(chunk_text) * fraction)
            spoken += _truncate_partial_text_to_boundary(chunk_text, chars)
            break
    return spoken


def _all_tts_audio_delivered(
    tts_chunks: list[tuple[str, int, bool]], audio_bytes_delivered: int
) -> bool:
    """Whether all synthesized TTS audio has been delivered/heard.

    ``audio_bytes_delivered`` should be the estimated bytes actually
    delivered/heard/acknowledged, not merely bytes written to the transport.
    """
    if not tts_chunks:
        return False
    if not all(completed for _, _, completed in tts_chunks):
        return False
    total_audio = sum(max(chunk_audio, 0) for _, chunk_audio, _ in tts_chunks)
    return audio_bytes_delivered >= total_audio


def _chunk_has_speech_energy(chunk: AudioChunk, *, threshold: int = 500) -> bool:
    """Heuristic speech gate for STT-driven turns when VAD is disabled.

    Computes the peak absolute PCM sample value for mono 16-bit chunks and
    compares it to ``threshold``. This filters continuous silent/background
    frames (e.g. telephony keepalive silence) so they don't spuriously start
    turns.
    """
    if chunk.format.sample_width != 2:
        return bool(chunk.data)

    data = chunk.data
    if len(data) < 2:
        return False

    peak = 0
    for i in range(0, len(data) - 1, 2):
        sample = int.from_bytes(data[i : i + 2], "little", signed=True)
        mag = abs(sample)
        if mag > peak:
            peak = mag
            if peak >= threshold:
                return True
    return False


def _audio_bytes_likely_heard(
    send_log: list[tuple[float, int, float]],
    cutoff_time: float | None,
) -> int:
    """Estimate bytes likely heard by ``cutoff_time``.

    ``send_log`` entries are ``(send_time, bytes_sent, chunk_duration_ms)``.
    Chunks are modeled as serial playback with a virtual playout cursor:
    each chunk starts at ``max(send_time, previous_chunk_end)`` and then
    plays linearly over its own duration.
    """
    if not send_log:
        return 0
    if cutoff_time is None:
        return sum(max(size, 0) for _, size, _ in send_log)

    heard = 0
    playout_cursor: float | None = None

    for send_time, size, duration_ms in send_log:
        size = max(size, 0)
        if size == 0:
            continue

        start_time = send_time
        if playout_cursor is not None and playout_cursor > start_time:
            start_time = playout_cursor

        if duration_ms <= 0:
            if start_time <= cutoff_time:
                heard += size
            continue

        duration_s = duration_ms / 1000.0
        end_time = start_time + duration_s
        playout_cursor = end_time

        elapsed_ms = (cutoff_time - start_time) * 1000.0
        if elapsed_ms <= 0:
            continue
        if elapsed_ms >= duration_ms:
            heard += size
            continue
        heard += int(size * (elapsed_ms / duration_ms))
    return heard


def _audio_bytes_acknowledged(
    playback_ack_log: list[tuple[float, int]],
    cutoff_time: float | None,
) -> int:
    """Return acknowledged bytes at or before ``cutoff_time``."""
    if not playback_ack_log:
        return 0
    if cutoff_time is None:
        return max(0, playback_ack_log[-1][1])

    acknowledged = 0
    for ack_time, acked_bytes in playback_ack_log:
        if ack_time > cutoff_time:
            break
        acknowledged = max(acknowledged, max(acked_bytes, 0))
    return acknowledged


def _audio_bytes_per_second_from_send_log(send_log: list[tuple[float, int, float]]) -> float:
    """Estimate playout bytes/second from send-log durations."""
    total_bytes = 0
    total_duration_ms = 0.0
    for _, size, duration_ms in send_log:
        size = max(size, 0)
        if size <= 0 or duration_ms <= 0:
            continue
        total_bytes += size
        total_duration_ms += duration_ms
    if total_bytes <= 0 or total_duration_ms <= 0:
        return 0.0
    return (total_bytes * 1000.0) / total_duration_ms


def _latest_playback_ack_time(
    playback_ack_log: list[tuple[float, int]],
    cutoff_time: float | None,
) -> float | None:
    """Return the latest ack timestamp at or before ``cutoff_time``."""
    latest: float | None = None
    for ack_time, _ in playback_ack_log:
        if cutoff_time is not None and ack_time > cutoff_time:
            break
        latest = ack_time
    return latest


def _audio_bytes_likely_heard_hybrid(
    send_log: list[tuple[float, int, float]],
    playback_ack_log: list[tuple[float, int]],
    cutoff_time: float | None,
    *,
    ack_stale_ms: int,
    ack_tail_cap_ms: int,
) -> int:
    """Estimate heard bytes using playback acks with heuristic stale fallback."""
    heuristic_heard = _audio_bytes_likely_heard(send_log, cutoff_time)
    if cutoff_time is None or not playback_ack_log:
        return heuristic_heard

    acked_heard = _audio_bytes_acknowledged(playback_ack_log, cutoff_time)
    if acked_heard <= 0:
        return heuristic_heard

    # Fresh-ack path: acknowledgements cap the timing estimate.
    heard = min(heuristic_heard, acked_heard)

    latest_ack_time = _latest_playback_ack_time(playback_ack_log, cutoff_time)
    if latest_ack_time is None:
        return heard

    bytes_per_second = _audio_bytes_per_second_from_send_log(send_log)
    if bytes_per_second <= 0:
        return heard

    ack_age_ms = max(0.0, (cutoff_time - latest_ack_time) * 1000.0)
    unacked_tail_bytes = max(0, heuristic_heard - acked_heard)
    unacked_tail_ms = (unacked_tail_bytes / bytes_per_second) * 1000.0
    is_stale = ack_age_ms > ack_stale_ms or unacked_tail_ms > ack_stale_ms
    if not is_stale:
        return heard

    if ack_tail_cap_ms <= 0:
        return heard
    tail_cap_bytes = int(bytes_per_second * (ack_tail_cap_ms / 1000.0))
    if tail_cap_bytes <= 0:
        return heard
    return min(heuristic_heard, acked_heard + tail_cap_bytes)


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _has_unclosed_single_emphasis(text: str, delimiter: str) -> bool:
    """Detect unclosed single-char emphasis delimiters (* / _) in text."""
    open_count = 0
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch == "\\":
            i += 2  # Skip escaped character (if any).
            continue
        if ch != delimiter:
            i += 1
            continue

        prev_char = text[i - 1] if i > 0 else ""
        next_char = text[i + 1] if i + 1 < length else ""

        # Ignore repeated runs (** / __ / *** / ___); those are handled elsewhere.
        if prev_char == delimiter or next_char == delimiter:
            i += 1
            continue

        is_open = (not _is_word_char(prev_char)) and bool(next_char) and not next_char.isspace()
        is_close = bool(prev_char) and not prev_char.isspace() and (not _is_word_char(next_char))

        if is_close and open_count > 0:
            open_count -= 1
        elif is_open:
            open_count += 1
        i += 1

    return open_count > 0


def _has_unclosed_markdown_link_or_image(text: str) -> bool:
    """Detect incomplete markdown link/image spans in ``text``.

    The check is intentionally conservative for streaming safety: any trailing
    ``[label]`` without a resolved destination is treated as still-open until a
    non-link continuation is observed.
    """

    label_depth = 0
    awaiting_destination = False
    destination_depth = 0
    i = 0
    length = len(text)

    while i < length:
        ch = text[i]

        if ch == "\\":
            i += 2
            continue

        if destination_depth > 0:
            if ch == "(":
                destination_depth += 1
            elif ch == ")":
                destination_depth -= 1
            i += 1
            continue

        if label_depth > 0:
            if ch == "[":
                label_depth += 1
            elif ch == "]":
                label_depth -= 1
                if label_depth == 0:
                    awaiting_destination = True
            i += 1
            continue

        if awaiting_destination:
            if ch.isspace():
                i += 1
                continue
            if ch == "(":
                awaiting_destination = False
                destination_depth = 1
                i += 1
                continue
            # Closed bracket not followed by destination: not a markdown link.
            awaiting_destination = False
            continue

        if ch == "[":
            label_depth = 1
        i += 1

    return label_depth > 0 or awaiting_destination or destination_depth > 0


def _has_unclosed_markdown_delimiters(text: str) -> bool:
    """Best-effort check for unfinished markdown spans in a rolling buffer.

    The streaming path defers sentence emission while markdown delimiters are
    still open so later deltas cannot rewrite already-emitted text.
    """

    fenced_count = text.count("```")
    if fenced_count % 2 == 1:
        return True

    # Remove fenced blocks so inline delimiter counts are not distorted.
    normalized = re.sub(r"```[\s\S]*?```", "", text)

    # Inline backticks only (exclude fenced markers already handled above).
    inline_tick_count = normalized.count("`")
    if inline_tick_count % 2 == 1:
        return True

    # Remove closed inline-code spans so markdown chars inside code do not
    # affect emphasis/link-state tracking.
    normalized = re.sub(r"`[^`]*`", "", normalized)

    for delimiter in ("**", "__", "~~"):
        if normalized.count(delimiter) % 2 == 1:
            return True

    if _has_unclosed_markdown_link_or_image(normalized):
        return True

    return _has_unclosed_single_emphasis(normalized, "*") or _has_unclosed_single_emphasis(
        normalized, "_"
    )


_PAUSE_MARKER = ""
_PAUSE_CHARS_PER_SECOND = 14.0


def _text_for_spoken_estimation(payload: TTSInput) -> str:
    """Return plain spoken text for interruption accounting.

    Interruption text estimation compares audio-byte progress against text
    length. SSML markup should not count toward spoken-character estimates,
    so SSML payloads are normalized to plain text here.
    """

    if payload.format == "ssml":
        return strip_ssml_tags(payload.text)
    return payload.text


def _text_for_estimation_timeline(payload: TTSInput) -> str:
    """Return text used for interruption timeline estimation.

    For SSML payloads, explicit ``<break .../>`` pauses are expanded into
    synthetic marker characters so byte->text interpolation accounts for
    non-spoken silence regions.
    """

    if payload.format != "ssml":
        return payload.text

    def _break_repl(match: re.Match[str]) -> str:
        attrs = match.group(1)
        ms_match = re.search(
            r"""time\s*=\s*(['"])\s*(\d+)\s*ms\s*\1""",
            attrs,
            flags=re.IGNORECASE,
        )
        ms = int(ms_match.group(2)) if ms_match else 0
        count = max(1, round((ms / 1000.0) * _PAUSE_CHARS_PER_SECOND)) if ms > 0 else 1
        return _PAUSE_MARKER * count

    with_markers = re.sub(r"<break\b([^>]*)/>", _break_repl, payload.text, flags=re.IGNORECASE)
    return strip_ssml_tags(with_markers)


def _cleanup_estimation_text(text: str) -> str:
    """Remove synthetic pause markers from estimated spoken text."""

    return text.replace(_PAUSE_MARKER, "")


def _replace_last_assistant_text(agent: Any, text: str) -> None:
    """Update the last assistant message in the agent's chat history.

    Works with :class:`AgentRunner` and :class:`BaseAgentAdapter` (or any
    object that exposes ``replace_last_assistant_text``).  Silently does
    nothing when the method is unavailable.
    """
    fn = getattr(agent, "replace_last_assistant_text", None)
    if callable(fn):
        fn(text)


# ── Session ────────────────────────────────────────────────────────


class Session:
    """One voice session (per call / per websocket client).

    Manages the full pipeline: Audio In -> Noise Reduction -> VAD -> STT ->
    Agent -> TTS -> Audio Out. Each stage is a pluggable provider.

    When the configured agent supports streaming (has a ``run_streaming``
    method), the session consumes text deltas incrementally and begins
    TTS synthesis on sentence boundaries for lower latency.
    """

    def __init__(self, config: SessionConfig | None = None) -> None:
        cfg = config or SessionConfig()

        # Providers (fall back to no-op stubs)
        self.stt = cfg.stt or NoopSTT()
        self.tts = cfg.tts or NoopTTS()
        self.vad = cfg.vad or NoopVAD()
        self.noise_reducer = cfg.noise_reducer or PassthroughNoiseReducer()
        self.echo_canceller = cfg.echo_canceller or PassthroughAEC()
        self.transport = cfg.transport or NoopTransport()
        self.agent: Agent = cfg.agent or NoopAgent()

        noops = []
        if isinstance(self.stt, NoopSTT):
            noops.append("stt")
        if isinstance(self.tts, NoopTTS):
            noops.append("tts")
        if cfg.enable_vad and isinstance(self.vad, NoopVAD):
            noops.append("vad")
        if isinstance(self.noise_reducer, PassthroughNoiseReducer) and cfg.enable_noise_reduction:
            noops.append("noise_reducer")
        if isinstance(self.transport, NoopTransport):
            noops.append("transport")
        if isinstance(self.agent, NoopAgent):
            noops.append("agent")
        if noops:
            raise ValueError(
                "SessionConfig must provide non-noop implementations for: " + ", ".join(noops)
            )

        # Event system
        self.event_bus = cfg.event_bus or EventBus()

        # Attach event bus to providers that accept it
        self._maybe_attach_event_bus(self.stt)
        self._maybe_attach_event_bus(self.tts)
        self._maybe_attach_event_bus(self.transport)

        # Pipeline flags
        self._enable_noise_reduction = cfg.enable_noise_reduction
        self._enable_aec = cfg.enable_echo_cancellation and not isinstance(
            self.echo_canceller, PassthroughAEC
        )
        self._enable_vad = cfg.enable_vad
        self._auto_turn_from_stt_final = cfg.auto_turn_from_stt_final
        self._interruption_mode = cfg.interruption_mode
        self._interruption_latency_compensation_ms = max(
            0, cfg.interruption_latency_compensation_ms
        )
        self._interruption_ack_stale_ms = max(0, cfg.interruption_ack_stale_ms)
        self._interruption_ack_tail_cap_ms = max(0, cfg.interruption_ack_tail_cap_ms)
        self._strip_markdown = cfg.strip_markdown
        self._output_processors: list[LLMOutputProcessor] = list(cfg.output_processors)

        # Turn manager — single source of truth for turn state
        self._turn_manager = cfg.turn_manager or TurnManager(
            self.event_bus,
            config=cfg.turn_manager_config,
            cancel_turn_callback=self._cancel_for_barge_in,
        )
        self.event_bus.subscribe(TurnStarted, self._on_turn_started)
        self.event_bus.subscribe(TurnEnded, self._schedule_turn_ended)
        self.event_bus.subscribe(PlaybackMarkAck, self._on_playback_mark_ack)

        # Reliability/observability config
        self._timeout_config = cfg.timeout_config or TimeoutConfig()
        self._metrics = cfg.metrics
        self._spans = SpanManager(tracer=cfg.tracer)

        # Backpressure (outbound audio queue)
        self._outbound_queue_external = cfg.outbound_queue is not None
        self._outbound_queue_max_size = 200
        self._outbound_queue_policy = DropPolicy.DROP_OLDEST
        self._outbound_queue_name = "outbound_audio"
        self._outbound_queue = cfg.outbound_queue or BoundedAudioQueue(
            max_size=self._outbound_queue_max_size,
            policy=self._outbound_queue_policy,
            name=self._outbound_queue_name,
        )
        self._outbound_task: asyncio.Task[None] | None = None
        self._tts_synth = TTSSynthesizer(
            tts=self.tts,
            event_bus=self.event_bus,
            outbound_queue=self._outbound_queue,
            spans=self._spans,
            metrics=self._metrics,
            timeout_config=self._timeout_config,
            correlation_ids=lambda: (self.session_id, self._current_turn_id),
            audio_gate=cfg.audio_gate,
        )
        self._audio_gate = cfg.audio_gate
        self._health_checkers: list[PeriodicHealthChecker] = []
        self._telephony_helpers: list[SessionHelper] = list(cfg.telephony_helpers)

        # Metrics counters
        if self._metrics:
            self.event_bus.subscribe(
                Interruption, lambda e: self._metrics.increment_counter(INTERRUPTIONS)
            )
            self.event_bus.subscribe(
                ReconnectSuccess, lambda e: self._metrics.increment_counter(RECONNECTS)
            )
            self.event_bus.subscribe(Error, lambda e: self._metrics.increment_counter(ERRORS))

        # State
        self._is_running = False
        self._pipeline_task: asyncio.Task[None] | None = None
        self._stt_task: asyncio.Task[None] | None = None
        self._current_tts_task: asyncio.Task[None] | None = None
        self._stt_final_future: asyncio.Future[str] | None = None

        # Cooperative cancellation: one token per turn
        self._cancel_token: CancelToken | None = None

        # STT stream started for current turn
        self._stt_active = False
        self._auto_turn_speech_frames = 0

        # Timing markers for metrics
        self._turn_end_time: float | None = None
        self._stt_final_time: float | None = None
        self._first_agent_time: float | None = None
        self._first_tts_audio_time: float | None = None

        # Audio bytes actually sent to the transport during this turn.
        # Used to estimate which portion of the agent response the user
        # heard before a barge-in.
        self._turn_audio_bytes_sent: int = 0
        self._turn_audio_send_log: deque[tuple[float, int, float]] = deque(maxlen=10_000)
        self._turn_playback_mark_to_bytes: dict[str, int] = {}
        self._turn_playback_ack_log: deque[tuple[float, int]] = deque(maxlen=10_000)
        self._playback_mark_seq: int = 0
        self._playback_mark_bytes_interval: int = 4_000  # throttle: ~125ms at 16kHz/16-bit
        self._bytes_since_last_mark: int = 0
        self._last_barge_in_time: float | None = None

        self._playback_ack_transport: PlaybackAckTransport | None = None
        if isinstance(self.transport, PlaybackAckTransport):
            self._playback_ack_transport = self.transport

        self.session_id = f"session-{uuid4().hex[:12]}"
        self._current_turn_id: str | None = None
        self._turn_manager.bind_session(self.session_id)

    def _with_correlation(self, event: Any) -> Any:
        """Attach session/turn identifiers to events when supported."""
        if not hasattr(event, "session_id") and not hasattr(event, "turn_id"):
            return event
        kwargs: dict[str, Any] = {}
        if hasattr(event, "session_id") and getattr(event, "session_id", None) is None:
            kwargs["session_id"] = self.session_id
        if hasattr(event, "turn_id") and getattr(event, "turn_id", None) is None:
            kwargs["turn_id"] = self._current_turn_id
        return replace(event, **kwargs) if kwargs else event

    async def _emit(self, event: Any) -> None:
        await self.event_bus.emit(self._with_correlation(event))

    def _reset_turn_state(self) -> None:
        """Clear turn correlation state and reset the turn manager."""
        self._current_turn_id = None
        self._auto_turn_speech_frames = 0
        self._turn_manager.reset()

    # ── Properties ─────────────────────────────────────────────

    def subscribe_event(self, event_type: type, handler: EventHandler) -> None:
        """Subscribe to a session event via the underlying EventBus."""
        self.event_bus.subscribe(event_type, handler)

    def subscribe_events(
        self, event_types: tuple[type, ...] | list[type], handler: EventHandler
    ) -> list[tuple[type, EventHandler]]:
        """Subscribe a single handler to multiple event types at once.

        Accepts any of the event group tuples from :mod:`easycat.events`
        (e.g. ``ALL_EVENTS``, ``STT_EVENTS``) or an ad-hoc sequence.

        Returns a list of ``(event_type, handler)`` registrations that can be
        passed to :meth:`unsubscribe_handlers`.
        """
        registrations: list[tuple[type, EventHandler]] = []
        for event_type in event_types:
            self.event_bus.subscribe(event_type, handler)
            registrations.append((event_type, handler))
        return registrations

    def unsubscribe_event(self, event_type: type, handler: EventHandler) -> None:
        """Unsubscribe a handler previously attached with ``subscribe_event``."""
        self.event_bus.unsubscribe(event_type, handler)

    def subscribe_agent_events(
        self,
        *,
        on_delta: EventHandler | None = None,
        on_final: EventHandler | None = None,
        on_tool_started: EventHandler | None = None,
        on_tool_delta: EventHandler | None = None,
        on_tool_result: EventHandler | None = None,
    ) -> list[tuple[type, EventHandler]]:
        """Subscribe handlers for agent and tool-call events in one call.

        Returns a list of ``(event_type, handler)`` registrations that can be
        passed to :meth:`unsubscribe_handlers`.
        """
        registrations: list[tuple[type, EventHandler]] = []

        for event_type, handler in (
            (AgentDelta, on_delta),
            (AgentFinal, on_final),
            (ToolCallStarted, on_tool_started),
            (ToolCallDelta, on_tool_delta),
            (ToolCallResult, on_tool_result),
        ):
            if handler is None:
                continue
            self.event_bus.subscribe(event_type, handler)
            registrations.append((event_type, handler))

        return registrations

    def unsubscribe_handlers(self, registrations: list[tuple[type, EventHandler]]) -> None:
        """Unsubscribe a batch of event handlers from prior registrations."""
        for event_type, handler in registrations:
            self.event_bus.unsubscribe(event_type, handler)

    @property
    def turn_state(self) -> TurnState:
        """Session-level turn state, derived from the TurnManager."""
        return _TM_TO_TURN_STATE.get(self._turn_manager.state, TurnState.IDLE)

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def is_speaking(self) -> bool:
        return self._turn_manager.state in (
            TurnManagerState.USER_SPEAKING,
            TurnManagerState.USER_PAUSED,
        )

    @property
    def is_bot_speaking(self) -> bool:
        return self._turn_manager.state == TurnManagerState.BOT_SPEAKING

    @property
    def outbound_queue(self) -> BoundedAudioQueue:
        return self._outbound_queue

    @property
    def tts_synth(self) -> TTSSynthesizer:
        return self._tts_synth

    @property
    def cancel_token(self) -> CancelToken | None:
        return self._cancel_token

    # ── Lifecycle ──────────────────────────────────────────────

    async def start(self) -> None:
        """Initialize providers and begin the audio receive loop."""
        if self._is_running:
            return
        self._is_running = True

        await self.transport.connect()
        if not self._outbound_queue_external:
            self._outbound_queue = BoundedAudioQueue(
                max_size=self._outbound_queue_max_size,
                policy=self._outbound_queue_policy,
                name=self._outbound_queue_name,
            )
            self._tts_synth._outbound_queue = self._outbound_queue
        # Start periodic health checks for providers that support it
        self._health_checkers = []
        for name, provider in (
            ("stt", self.stt),
            ("tts", self.tts),
            ("transport", self.transport),
        ):
            if hasattr(provider, "health_check"):
                checker = PeriodicHealthChecker(
                    provider,
                    provider_name=name,
                    event_bus=self.event_bus,
                )
                checker.start()
                self._health_checkers.append(checker)
        for helper in self._telephony_helpers:
            helper.start()
        self._outbound_task = asyncio.create_task(self._drain_outbound_audio())
        self._pipeline_task = asyncio.create_task(self._run_pipeline())

    async def stop(self) -> None:
        """Gracefully stop the session: finish current turn, close providers."""
        if not self._is_running:
            return
        self._is_running = False

        if self._cancel_token:
            self._cancel_token.cancel()

        if self._pipeline_task and not self._pipeline_task.done():
            self._pipeline_task.cancel()
            try:
                await self._pipeline_task
            except asyncio.CancelledError:
                logger.debug(
                    "TTS processing task was cancelled; ensuring"
                    " BotStoppedSpeaking is emitted if needed."
                )

        await self._cancel_stt()
        await self._cancel_tts()
        for checker in self._health_checkers:
            await checker.stop()
        self._health_checkers = []
        self._stop_helpers()
        self._spans.finish_all(SpanStatus.CANCELLED)
        self._outbound_queue.close()
        if self._outbound_task and not self._outbound_task.done():
            self._outbound_task.cancel()
            try:
                await self._outbound_task
            except asyncio.CancelledError:
                pass
        await self.transport.disconnect()
        self._reset_turn_state()

    async def shutdown(self) -> None:
        """Force-close everything and release resources."""
        self._is_running = False

        if self._cancel_token:
            self._cancel_token.cancel()

        tasks: list[asyncio.Task[Any]] = []
        if self._pipeline_task and not self._pipeline_task.done():
            self._pipeline_task.cancel()
            tasks.append(self._pipeline_task)
        if self._stt_task and not self._stt_task.done():
            self._stt_task.cancel()
            tasks.append(self._stt_task)
        if self._current_tts_task and not self._current_tts_task.done():
            self._current_tts_task.cancel()
            tasks.append(self._current_tts_task)
        if self._outbound_task and not self._outbound_task.done():
            self._outbound_task.cancel()
            tasks.append(self._outbound_task)

        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        for checker in self._health_checkers:
            await checker.stop()
        self._health_checkers = []
        self._stop_helpers()
        self._spans.finish_all(SpanStatus.CANCELLED)
        self._outbound_queue.close()
        await self.transport.disconnect()
        self._reset_turn_state()

    # ── Cancellation ───────────────────────────────────────────

    async def cancel_turn(self, *, barge_in: bool = False) -> None:
        """Trigger cancel token, abort STT/agent/TTS, reset turn state.

        If barge_in is True, emits an Interruption event.
        """
        if self._cancel_token:
            self._cancel_token.cancel()

        if barge_in:
            self._last_barge_in_time = time.monotonic()
            await self._emit(Interruption())

        await self._cancel_stt()
        await self._cancel_tts()
        self._outbound_queue.flush_for_new_turn()

        if not barge_in:
            self._reset_turn_state()

        self._spans.finish_all(SpanStatus.CANCELLED)

    async def cancel_tts_playback(self) -> None:
        """Stop TTS provider and flush outbound audio."""
        if self._cancel_token:
            self._cancel_token.cancel()

        await self._cancel_tts()
        self._outbound_queue.flush_for_new_turn()
        if self._turn_manager.state == TurnManagerState.BOT_SPEAKING:
            self._reset_turn_state()

    async def reset_state(self) -> None:
        """Cancel everything and return to idle/listening state.

        Also clears agent conversation history if the agent supports it.
        """
        if self._cancel_token:
            self._cancel_token.cancel()

        await self._cancel_stt()
        await self._cancel_tts()
        self._outbound_queue.flush_for_new_turn()

        # Clear agent history if supported (e.g., AgentRunner)
        if hasattr(self.agent, "clear_history"):
            self.agent.clear_history()

        # Reset turn manager state
        self._reset_turn_state()

        self._spans.finish_all(SpanStatus.CANCELLED)

    # ── Push-to-talk helpers ───────────────────────────────────

    async def start_turn(self) -> None:
        """Manually start a user turn (push-to-talk mode)."""
        await self._turn_manager.start_turn()

    async def end_turn(self) -> None:
        """Manually end the current user turn (push-to-talk mode)."""
        await self._turn_manager.end_turn()

    # ── TurnManager callbacks ──────────────────────────────────

    async def _cancel_for_barge_in(self) -> None:
        """Cancel current turn due to barge-in (called by TurnManager)."""
        await self.cancel_turn(barge_in=True)

    async def _on_turn_started(self, event: TurnStarted) -> None:
        """Handle TurnStarted from TurnManager: start STT and prime pre-roll."""
        if not self._is_running:
            return

        self._current_turn_id = event.turn_id

        # Reset timing markers for this turn
        self._turn_end_time = None
        self._stt_final_time = None
        self._first_agent_time = None
        self._first_tts_audio_time = None
        self._turn_audio_bytes_sent = 0
        self._turn_audio_send_log.clear()
        self._turn_playback_mark_to_bytes.clear()
        self._turn_playback_ack_log.clear()
        self._bytes_since_last_mark = 0
        self._last_barge_in_time = None

        # Initialize tracing for this turn
        self._spans.begin_turn()

        # Establish a new cancel token from TurnManager
        self._cancel_token = self._turn_manager.cancel_token or CancelToken()

        self._auto_turn_speech_frames = 0

        # Start STT stream
        try:
            await self.stt.start_stream()
            self._stt_active = True
            self._start_stt_event_task()
        except Exception as exc:
            logger.exception("Failed to start STT stream")
            await self._emit(Error(exception=exc, context="stt_start"))
            self._stt_active = False
            return

        # Prime STT with pre-roll frames captured by TurnManager
        for chunk in self._turn_manager.turn_audio:
            await self.stt.send_audio(chunk)

    def _stop_helpers(self) -> None:
        """Stop attached helper components that own event subscriptions/state."""
        for helper in self._telephony_helpers:
            try:
                helper.stop()
            except Exception:
                logger.debug("Error stopping session helper", exc_info=True)

    def _schedule_turn_ended(self, event: TurnEnded) -> None:
        """Schedule end-of-turn processing without blocking other handlers."""
        if self._current_tts_task and not self._current_tts_task.done():
            self._current_tts_task.cancel()
        self._current_tts_task = asyncio.create_task(self._on_turn_ended(event))
        self._current_tts_task.add_done_callback(self._log_task_exception)

    async def _on_turn_ended(self, event: TurnEnded) -> None:
        """Handle TurnEnded from TurnManager: finalize STT and run agent/TTS."""
        if not self._is_running:
            return
        if self._turn_manager.state != TurnManagerState.PROCESSING:
            return
        self._turn_end_time = event.timestamp
        if not self._auto_turn_from_stt_final:
            self._spans.start(Tracer.STT)
        await self._handle_end_of_speech()

    @staticmethod
    def _log_task_exception(task: asyncio.Task[object]) -> None:
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Background task failed")

    def _start_stt_event_task(self) -> None:
        """Start background consumption of provider-scoped STT events."""
        if self._stt_task and not self._stt_task.done():
            self._stt_task.cancel()
        loop = asyncio.get_running_loop()
        self._stt_final_future = loop.create_future()

        async def _consume() -> None:
            saw_final = False
            try:
                async for stt_event in self.stt.events():
                    if self._cancel_token and self._cancel_token.is_cancelled:
                        break
                    if stt_event.type == STTEventType.PARTIAL:
                        await self._emit(STTPartial(text=stt_event.text, track=stt_event.track))
                    elif stt_event.type == STTEventType.FINAL:
                        saw_final = True
                        await self._emit(STTFinal(text=stt_event.text, track=stt_event.track))
                        self._stt_final_time = time.monotonic()
                        if self._metrics and self._turn_end_time is not None:
                            self._metrics.record_latency(
                                STT_LATENCY,
                                (self._stt_final_time - self._turn_end_time) * 1000,
                            )
                        self._spans.finish(Tracer.STT)
                        if self._stt_final_future and not self._stt_final_future.done():
                            self._stt_final_future.set_result(stt_event.text)
                        if self._auto_turn_from_stt_final:
                            await self._turn_manager.end_turn()
                        break
            except Exception as exc:
                logger.exception("STT event loop error")
                await self._emit(Error(exception=exc, context="stt_events"))
                if self._stt_final_future and not self._stt_final_future.done():
                    self._stt_final_future.set_result("")
            finally:
                if self._stt_final_future and not self._stt_final_future.done():
                    self._stt_final_future.set_result("")
                if not saw_final:
                    self._spans.finish(Tracer.STT, SpanStatus.CANCELLED)

        self._stt_task = asyncio.create_task(_consume())

    # ── Pipeline ───────────────────────────────────────────────

    async def _run_pipeline(self) -> None:
        """Main audio receive loop: Transport -> Noise Reduction -> AEC -> VAD -> STT.

        On STT final -> Agent -> TTS -> Transport audio out.
        """
        try:
            async for chunk in self.transport.receive_audio():
                if not self._is_running:
                    break

                await self._emit(AudioIn(chunk=chunk))

                # Stage 1: Noise reduction (optional)
                if self._enable_noise_reduction:
                    self._spans.start(Tracer.NOISE_REDUCTION)
                    noise_reduction_status = SpanStatus.OK
                    try:
                        chunk = await self.noise_reducer.process(chunk)
                    except asyncio.CancelledError:
                        noise_reduction_status = SpanStatus.CANCELLED
                        raise
                    except Exception as exc:
                        self._spans.finish_with_error(Tracer.NOISE_REDUCTION, exc)
                        raise
                    finally:
                        if self._spans.has(Tracer.NOISE_REDUCTION):
                            self._spans.finish(Tracer.NOISE_REDUCTION, noise_reduction_status)

                # Stage 2: Echo cancellation (optional)
                if self._enable_aec:
                    chunk = await self.echo_canceller.process(chunk)

                # Stage 3: VAD (optional)
                if self._enable_vad:
                    self._spans.start(Tracer.VAD)
                    vad_status = SpanStatus.OK
                    try:
                        async for vad_event in self.vad.process(chunk):
                            vad_event = self._with_correlation(vad_event)
                            await self._emit(vad_event)
                            await self._turn_manager.on_vad_event(vad_event)
                    except asyncio.CancelledError:
                        vad_status = SpanStatus.CANCELLED
                        raise
                    except Exception as exc:
                        self._spans.finish_with_error(Tracer.VAD, exc)
                        raise
                    finally:
                        if self._spans.has(Tracer.VAD):
                            self._spans.finish(Tracer.VAD, vad_status)

                # TurnManager always sees raw audio frames for pre-roll buffering
                self._turn_manager.on_audio_frame(chunk)

                # Stage 4: Feed audio to STT (if listening)
                started_turn_from_chunk = False
                if self._auto_turn_from_stt_final and not self._stt_active:
                    if self._turn_manager.state == TurnManagerState.IDLE:
                        if _chunk_has_speech_energy(chunk):
                            self._auto_turn_speech_frames += 1
                        else:
                            self._auto_turn_speech_frames = 0

                        if self._auto_turn_speech_frames >= 2:
                            await self._turn_manager.start_turn()
                            self._auto_turn_speech_frames = 0
                            started_turn_from_chunk = self._stt_active
                    else:
                        self._auto_turn_speech_frames = 0

                if self.is_speaking and self._stt_active and not started_turn_from_chunk:
                    await self.stt.send_audio(chunk)

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.exception("Pipeline error")
            await self._emit(Error(exception=exc, context="pipeline"))

    async def _handle_end_of_speech(self) -> None:
        """Called when VAD signals end of speech: finalize STT, run agent, synthesize TTS."""
        if self._stt_active:
            await self.stt.end_stream()
            self._stt_active = False

        token = self._cancel_token

        transcript = ""
        if self._stt_final_future is not None:
            try:
                if self._timeout_config and self._timeout_config.stt_timeout:
                    transcript = await asyncio.wait_for(
                        self._stt_final_future,
                        timeout=self._timeout_config.stt_timeout,
                    )
                else:
                    transcript = await self._stt_final_future
            except TimeoutError:
                err = STTTimeoutError("stt", self._timeout_config.stt_timeout)
                await self._emit(Error(exception=err, context="stt_timeout"))
                self._spans.finish_with_error(Tracer.STT, err)
                self._reset_turn_state()
                return
            except Exception:
                transcript = ""
            finally:
                self._stt_final_future = None

        if not transcript or (token and token.is_cancelled):
            self._spans.finish("turn", SpanStatus.CANCELLED)
            self._reset_turn_state()
            return

        # Route to streaming or basic agent path
        if hasattr(self.agent, "run_streaming"):
            await self._run_streaming_agent(transcript, token)
        else:
            await self._run_basic_agent(transcript, token)

    # ── Agent invocation helper ────────────────────────────────

    async def _invoke_agent(self, transcript: str) -> str:
        """Invoke the basic agent with optional timeout. Returns the response."""
        if self._timeout_config and self._timeout_config.agent_timeout:
            return await with_agent_timeout(
                self.agent.run(transcript),
                timeout=self._timeout_config.agent_timeout,
                event_bus=self.event_bus,
            )
        return await self.agent.run(transcript)

    # ── Basic agent path ───────────────────────────────────────

    async def _run_basic_agent(self, transcript: str, token: CancelToken | None) -> None:
        """Non-streaming agent path: invoke run(), emit events, synthesize TTS."""
        self._spans.start(Tracer.AGENT)
        agent_status = SpanStatus.OK
        try:
            agent_response = await self._invoke_agent(transcript)
        except asyncio.CancelledError:
            agent_status = SpanStatus.CANCELLED
            raise
        except AgentTimeoutError:
            self._spans.finish(Tracer.AGENT, SpanStatus.ERROR)
            self._spans.finish("turn", SpanStatus.ERROR)
            self._reset_turn_state()
            return
        except Exception as exc:
            logger.exception("Agent error")
            await self._emit(Error(exception=exc, context="agent"))
            self._spans.finish_with_error(Tracer.AGENT, exc)
            self._spans.finish_with_error("turn", exc)
            self._reset_turn_state()
            return
        finally:
            if self._spans.has(Tracer.AGENT):
                self._spans.finish(Tracer.AGENT, agent_status)

        if token and token.is_cancelled:
            self._spans.finish("turn", SpanStatus.CANCELLED)
            self._reset_turn_state()
            return

        if self._strip_markdown:
            stripped = strip_markdown(agent_response, normalize_code_spans=True)
            if stripped != agent_response:
                agent_response = stripped
                _replace_last_assistant_text(self.agent, stripped)

        await self._emit(AgentDelta(text=agent_response))
        # Expose structured output from adapters that support it, but avoid
        # duplicating plain-text responses in `structured_output`.
        agent_structured = None
        agent_last_output = getattr(self.agent, "last_output", None)
        agent_output_type = getattr(self.agent, "output_type", None)
        if agent_output_type is not None or not isinstance(agent_last_output, str):
            agent_structured = agent_last_output
        await self._emit(AgentFinal(text=agent_response, structured_output=agent_structured))

        if self._metrics and self._stt_final_time is not None:
            self._metrics.record_latency(
                AGENT_LATENCY,
                (time.monotonic() - self._stt_final_time) * 1000,
            )

        await self._synthesize_tts(
            self._prepare_tts_payload(agent_response, is_streaming=False, is_final=True), token
        )

    # ── Streaming agent path ───────────────────────────────────

    async def _run_streaming_agent(self, transcript: str, token: CancelToken | None) -> None:
        """Streaming agent path with incremental TTS on sentence boundaries.

        Runs agent stream consumption and TTS synthesis concurrently:
        - Agent task: consumes stream events, emits EasyCat events, and queues
          complete sentences for TTS synthesis.
        - TTS task: dequeues text chunks and synthesizes them sequentially.
        """
        tts_queue: asyncio.Queue[TTSInput | None] = asyncio.Queue()
        turn_id = self._current_turn_id
        accumulated_text = ""
        structured_output: Any = None
        agent_error: BaseException | None = None
        interrupted = False
        tts_playback_started = False

        # Per-chunk TTS accounting: list of (text, audio_bytes_produced).
        # Populated by _process_tts so we can map audio-bytes-sent back to
        # text to estimate what the user actually heard before barge-in.
        tts_chunks: list[tuple[str, int, bool]] = []

        self._spans.start(Tracer.AGENT)

        async def _consume_agent() -> None:
            nonlocal accumulated_text, structured_output, agent_error, interrupted
            text_buffer = ""
            pending_tool_calls = 0

            async def _flush_pending_tts_buffer() -> None:
                nonlocal text_buffer
                if text_buffer.strip():
                    if self._strip_markdown:
                        text_buffer = strip_markdown(
                            text_buffer,
                            normalize_code_spans=True,
                        )
                    payload = self._prepare_tts_payload(
                        text_buffer,
                        is_streaming=True,
                        is_final=True,
                    )
                    if payload.text.strip():
                        await tts_queue.put(payload)
                text_buffer = ""

            try:
                async for event in self.agent.run_streaming(transcript, cancel_token=token):
                    if token and token.is_cancelled:
                        if not interrupted:
                            interrupted = True
                        # Let in-flight tool calls complete before stopping.
                        # NOTE: When the agent is an AgentRunner, the runner
                        # also drains tool calls internally — that's fine.
                        # This session-level drain ensures EasyCat events
                        # (ToolCallStarted/Result) are emitted to the event
                        # bus and text processing is skipped, regardless of
                        # the agent type.
                        if pending_tool_calls > 0:
                            if event.type == AgentStreamEventType.TOOL_RESULT:
                                pending_tool_calls = max(0, pending_tool_calls - 1)
                                await self._emit(
                                    ToolCallResult(call_id=event.call_id, result=event.result)
                                )
                                if pending_tool_calls <= 0:
                                    break
                            elif event.type == AgentStreamEventType.TOOL_STARTED:
                                pending_tool_calls += 1
                                await self._emit(
                                    ToolCallStarted(
                                        tool_name=event.tool_name, call_id=event.call_id
                                    )
                                )
                            elif event.type == AgentStreamEventType.TOOL_DELTA:
                                await self._emit(
                                    ToolCallDelta(call_id=event.call_id, delta=event.text)
                                )
                            elif event.type == AgentStreamEventType.DONE:
                                if event.text:
                                    accumulated_text = event.text
                                if event.structured_output is not None:
                                    structured_output = event.structured_output
                                break
                            # Skip text deltas during drain
                            continue
                        else:
                            # No tool calls in flight — stop immediately
                            break

                    if event.type == AgentStreamEventType.TEXT_DELTA:
                        accumulated_text += event.text
                        await self._emit(AgentDelta(text=event.text))
                        if self._first_agent_time is None:
                            self._first_agent_time = time.monotonic()
                            if self._metrics and self._stt_final_time is not None:
                                self._metrics.record_latency(
                                    AGENT_LATENCY,
                                    (self._first_agent_time - self._stt_final_time) * 1000,
                                )

                        if self._strip_markdown:
                            text_buffer += event.text
                            if _has_unclosed_markdown_delimiters(text_buffer):
                                continue

                            stripped_window = strip_markdown(
                                text_buffer,
                                trim=False,
                                normalize_code_spans=True,
                            )
                            ready, remaining = _split_at_sentence_boundaries(stripped_window)
                            if ready:
                                payload = self._prepare_tts_payload(
                                    ready,
                                    is_streaming=True,
                                    is_final=False,
                                )
                                if payload.text.strip():
                                    await tts_queue.put(payload)
                            text_buffer = remaining
                        else:
                            text_buffer += event.text

                            ready, text_buffer = _split_at_sentence_boundaries(text_buffer)
                            if ready:
                                payload = self._prepare_tts_payload(
                                    ready,
                                    is_streaming=True,
                                    is_final=False,
                                )
                                if payload.text.strip():
                                    await tts_queue.put(payload)

                    elif event.type == AgentStreamEventType.TOOL_STARTED:
                        pending_tool_calls += 1
                        await self._emit(
                            ToolCallStarted(tool_name=event.tool_name, call_id=event.call_id)
                        )
                    elif event.type == AgentStreamEventType.TOOL_DELTA:
                        await self._emit(ToolCallDelta(call_id=event.call_id, delta=event.text))
                    elif event.type == AgentStreamEventType.TOOL_RESULT:
                        pending_tool_calls = max(0, pending_tool_calls - 1)
                        await self._emit(
                            ToolCallResult(call_id=event.call_id, result=event.result)
                        )
                    elif event.type == AgentStreamEventType.DONE:
                        if event.text:
                            accumulated_text = event.text
                        if event.structured_output is not None:
                            structured_output = event.structured_output
                        # Flush any tail immediately so TTS can start before
                        # stream teardown/adapter cleanup completes.
                        await _flush_pending_tts_buffer()
            except Exception as exc:
                agent_error = exc
                logger.exception("Agent streaming error")
                await self._emit(Error(exception=exc, context="agent"))
            finally:
                stream_succeeded = agent_error is None and (not token or not token.is_cancelled)
                if stream_succeeded:
                    await _flush_pending_tts_buffer()
                await tts_queue.put(None)  # sentinel to stop TTS task

        async def _process_tts() -> None:
            nonlocal tts_playback_started
            started = False
            try:
                while True:
                    payload = await tts_queue.get()
                    if payload is None:
                        break
                    if token and token.is_cancelled:
                        # Cancellation can land after dequeue but before synthesis.
                        # Preserve this chunk as incomplete so interruption
                        # accounting does not treat the turn as fully delivered.
                        tts_chunks.append((_text_for_estimation_timeline(payload), 0, False))
                        break

                    if not started:
                        # When the classification gate is closed, audio is
                        # buffered.  Don't enter BOT_SPEAKING so callee
                        # speech during CLASSIFYING isn't treated as barge-in.
                        gated = self._audio_gate is not None and self._audio_gate()
                        if not gated:
                            await self._turn_manager.bot_started_speaking()
                            tts_playback_started = True
                        started = True

                    result = await self._tts_synth.synthesize(
                        payload,
                        token,
                        turn_end_time=self._turn_end_time,
                        # When gated the turn manager stays in PROCESSING
                        # (we skipped bot_started_speaking), so the
                        # BOT_SPEAKING check would exit immediately.  Pass
                        # None so the synth loop buffers all audio.
                        is_active=(
                            None
                            if self._audio_gate is not None and self._audio_gate()
                            else lambda: (
                                self._turn_manager.state == TurnManagerState.BOT_SPEAKING
                            )
                        ),
                        record_latency=self._first_tts_audio_time is None,
                    )
                    tts_chunks.append(
                        (
                            _text_for_estimation_timeline(payload),
                            result.audio_bytes,
                            result.completed,
                        )
                    )
                    if result.first_audio_time is not None and self._first_tts_audio_time is None:
                        self._first_tts_audio_time = result.first_audio_time
            except asyncio.CancelledError:
                pass
            except TTSTimeoutError:
                await self._cancel_tts()
            except Exception:
                logger.exception("TTS streaming error")

            # Drain any queued-but-unsynthesized text so that
            # _all_tts_audio_delivered sees them as incomplete and
            # does not suppress notify_interruption.
            while not tts_queue.empty():
                remaining = tts_queue.get_nowait()
                if remaining is not None:
                    tts_chunks.append((_text_for_estimation_timeline(remaining), 0, False))

            if started and self._turn_manager.state == TurnManagerState.BOT_SPEAKING:
                await self._turn_manager.bot_stopped_speaking()
                self._spans.finish("turn")
            elif started and not tts_playback_started:
                # Gated: TTS was buffered, reset to IDLE so callee speech
                # can start new turns while waiting for classification.
                self._reset_turn_state()

        # Run agent consumption and TTS synthesis concurrently
        agent_task = asyncio.create_task(_consume_agent())
        tts_task = asyncio.create_task(_process_tts())

        try:
            if self._timeout_config and self._timeout_config.agent_timeout:
                await with_agent_timeout(
                    agent_task,
                    timeout=self._timeout_config.agent_timeout,
                    event_bus=self.event_bus,
                )
            else:
                await agent_task
        except Exception as exc:
            agent_error = exc
            if not agent_task.done():
                agent_task.cancel()
            if not tts_task.done():
                tts_task.cancel()
        finally:
            if agent_error:
                self._spans.finish_with_error(Tracer.AGENT, agent_error)
            else:
                self._spans.finish(Tracer.AGENT)

        stream_succeeded = agent_error is None and not (token and token.is_cancelled)

        if self._strip_markdown and accumulated_text and stream_succeeded:
            stripped = strip_markdown(accumulated_text, normalize_code_spans=True)
            if stripped != accumulated_text:
                accumulated_text = stripped
                _replace_last_assistant_text(self.agent, stripped)

        # Emit AgentFinal after agent stream is fully consumed
        if accumulated_text and stream_succeeded:
            await self._emit(
                AgentFinal(text=accumulated_text, structured_output=structured_output)
            )

        try:
            await tts_task
        except asyncio.CancelledError:
            pass

        # Both tasks are done.  If the user barged in, estimate what they
        # actually heard by comparing audio bytes sent to the transport
        # against the per-chunk audio produced by TTS.
        cancelled_during_playback = bool(token and token.is_cancelled and tts_playback_started)
        if (interrupted or cancelled_during_playback) and hasattr(
            self.agent, "notify_interruption"
        ):
            cutoff_time = token.cancelled_at if token is not None else None
            if cutoff_time is None:
                cutoff_time = self._last_barge_in_time
            if cutoff_time is not None and self._interruption_latency_compensation_ms > 0:
                cutoff_time -= self._interruption_latency_compensation_ms / 1000.0
            heard_bytes = _audio_bytes_likely_heard_hybrid(
                list(self._turn_audio_send_log),
                list(self._turn_playback_ack_log),
                cutoff_time,
                ack_stale_ms=self._interruption_ack_stale_ms,
                ack_tail_cap_ms=self._interruption_ack_tail_cap_ms,
            )

            if not _all_tts_audio_delivered(tts_chunks, heard_bytes):
                text_spoken = _cleanup_estimation_text(
                    _estimate_text_spoken(
                        [(text, audio_bytes) for text, audio_bytes, _ in tts_chunks],
                        heard_bytes,
                    )
                )
                try:
                    self.agent.notify_interruption(
                        text_spoken,
                        mode=self._interruption_mode,
                    )
                except Exception:
                    logger.debug("Failed to notify agent of interruption", exc_info=True)

        # If a newer turn started (e.g. barge-in), avoid clobbering its state.
        if self._current_turn_id == turn_id:
            # If agent errored or was cancelled with no TTS started, ensure idle.
            if self._turn_manager.state != TurnManagerState.IDLE:
                self._reset_turn_state()
            self._current_turn_id = None
            status = SpanStatus.ERROR if agent_error else SpanStatus.OK
            self._spans.finish("turn", status)

    def _prepare_tts_payload(self, text: str, *, is_streaming: bool, is_final: bool) -> TTSInput:
        payload = TTSInput(text=text, format="plain")
        payload = apply_output_processors(
            payload,
            self._output_processors,
            is_final=is_final,
            is_streaming=is_streaming,
        )
        if payload.format == "ssml" and not getattr(self.tts, "supports_ssml", False):
            return TTSInput(text=strip_ssml_tags(payload.text), format="plain")
        return payload

    # ── TTS synthesis helper ───────────────────────────────────

    async def _synthesize_tts(self, payload: TTSInput | str, token: CancelToken | None) -> None:
        """Synthesize TTS for a complete payload and emit audio events."""
        if isinstance(payload, str):
            payload = self._prepare_tts_payload(payload, is_streaming=False, is_final=True)
        turn_id = self._current_turn_id
        # When the classification gate is closed, audio is buffered (not sent
        # to the transport).  Don't transition the turn manager through
        # BOT_SPEAKING so that the replayed audio triggers the correct state
        # transitions later via the gate-flush callback.
        gated = self._audio_gate is not None and self._audio_gate()
        if not gated:
            await self._turn_manager.bot_started_speaking()
        try:
            result = await self._tts_synth.synthesize(
                payload,
                token,
                turn_end_time=self._turn_end_time,
                # When gated, the turn manager stays in PROCESSING (we skipped
                # bot_started_speaking), so the BOT_SPEAKING check would exit
                # immediately. Pass None so the synth loop runs to completion
                # and all audio gets buffered for replay when the gate opens.
                is_active=(
                    None
                    if gated
                    else lambda: self._turn_manager.state == TurnManagerState.BOT_SPEAKING
                ),
            )
            if result.first_audio_time is not None:
                self._first_tts_audio_time = result.first_audio_time
        except (asyncio.CancelledError, TTSTimeoutError):
            pass
        finally:
            try:
                if (
                    not gated
                    and self._current_turn_id == turn_id
                    and self._turn_manager.state == TurnManagerState.BOT_SPEAKING
                ):
                    await self._turn_manager.bot_stopped_speaking()
                    self._spans.finish("turn")
                elif gated and self._current_turn_id == turn_id:
                    # Gated opener TTS is buffered — reset to IDLE so the
                    # callee's speech can start new turns while we wait for
                    # classification.  The gate-flush callback replays the
                    # buffered audio later.
                    self._reset_turn_state()
            finally:
                if self._current_turn_id == turn_id:
                    self._current_turn_id = None

    # ── Internal helpers ───────────────────────────────────────

    async def _drain_outbound_audio(self) -> None:
        """Send queued outbound audio to the transport with backpressure."""
        while True:
            if not self._is_running and self._outbound_queue.empty():
                break
            try:
                chunk = await self._outbound_queue.get()
            except asyncio.QueueEmpty:
                break
            try:
                await self.transport.send_audio(chunk)
                if self._enable_aec:
                    self.echo_canceller.feed_reference(chunk)
                sent_size = len(chunk.data)
                self._turn_audio_bytes_sent += sent_size
                self._turn_audio_send_log.append((time.monotonic(), sent_size, chunk.duration_ms))
                self._bytes_since_last_mark += sent_size
                if (
                    sent_size > 0
                    and self._playback_ack_transport is not None
                    and self._bytes_since_last_mark >= self._playback_mark_bytes_interval
                ):
                    self._bytes_since_last_mark = 0
                    await self._send_playback_mark()
                elif (
                    sent_size > 0
                    and self._bytes_since_last_mark > 0
                    and self._playback_ack_transport is not None
                    and self._turn_manager.state != TurnManagerState.BOT_SPEAKING
                    and self._outbound_queue.empty()
                ):
                    # Best-effort trailing mark while the session is still
                    # running.  The post-loop trailing mark (below) is the
                    # reliable fallback at shutdown; this path provides a
                    # timely ack for the final playback position mid-session.
                    self._bytes_since_last_mark = 0
                    await self._send_playback_mark()
            except Exception:
                logger.exception("Failed to send audio to transport")

        # Send a final mark for any trailing bytes that didn't reach the
        # throttle threshold, so the last playback position gets acked.
        if self._bytes_since_last_mark > 0 and self._playback_ack_transport is not None:
            self._bytes_since_last_mark = 0
            await self._send_playback_mark()

    async def _send_playback_mark(self) -> None:
        if self._playback_ack_transport is None:
            return

        self._playback_mark_seq += 1
        requested_mark_name = f"ec_playback_{self._playback_mark_seq}"
        self._turn_playback_mark_to_bytes[requested_mark_name] = self._turn_audio_bytes_sent
        try:
            mark_name = await self._playback_ack_transport.send_playback_mark(
                name=requested_mark_name
            )
            if mark_name != requested_mark_name:
                acked_bytes = self._turn_playback_mark_to_bytes.pop(requested_mark_name, None)
                if acked_bytes is not None:
                    self._turn_playback_mark_to_bytes[mark_name] = acked_bytes
        except Exception:
            self._turn_playback_mark_to_bytes.pop(requested_mark_name, None)
            logger.debug("Failed to send playback mark", exc_info=True)

    def _on_playback_mark_ack(self, event: PlaybackMarkAck) -> None:
        """Track acknowledged playout byte positions for the active turn."""
        acked_bytes = self._turn_playback_mark_to_bytes.pop(event.mark_name, None)
        if acked_bytes is None:
            return
        if self._turn_playback_ack_log and acked_bytes < self._turn_playback_ack_log[-1][1]:
            acked_bytes = self._turn_playback_ack_log[-1][1]
        self._turn_playback_ack_log.append((event.timestamp, acked_bytes))

    def _maybe_attach_event_bus(self, provider: Any) -> None:
        """Attach the session EventBus to provider configs that support it."""
        attached = False
        cfg = getattr(provider, "_config", None)
        if cfg is not None and hasattr(cfg, "event_bus") and getattr(cfg, "event_bus") is None:
            try:
                setattr(cfg, "event_bus", self.event_bus)
                attached = True
            except Exception:
                pass
        has_unset_bus = hasattr(provider, "_event_bus") and getattr(provider, "_event_bus") is None
        if not attached and has_unset_bus:
            try:
                setattr(provider, "_event_bus", self.event_bus)
            except Exception:
                pass

    async def _cancel_stt(self) -> None:
        try:
            await self.stt.end_stream()
        except Exception:
            pass
        self._stt_active = False
        self._auto_turn_speech_frames = 0
        if self._stt_task and not self._stt_task.done():
            self._stt_task.cancel()
            try:
                await self._stt_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._stt_final_future and not self._stt_final_future.done():
            self._stt_final_future.set_result("")
        self._stt_final_future = None

    async def _cancel_tts(self) -> None:
        await self._tts_synth.cancel()
        if self._current_tts_task and not self._current_tts_task.done():
            self._current_tts_task.cancel()
            try:
                await self._current_tts_task
            except (asyncio.CancelledError, Exception):
                pass
