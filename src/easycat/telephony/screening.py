"""Call screening detection: pattern matching against STT transcripts."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum

from easycat.events import (
    CallAnswered,
    CallEnded,
    CallScreening,
    EventBus,
    ScreeningTimedOut,
    STTFinal,
    STTPartial,
    VoicemailDetected,
)

logger = logging.getLogger(__name__)

# Minimum transcript length before screening patterns are checked,
# to prevent false-positive triggers on short human utterances.
MIN_TRANSCRIPT_LENGTH = 30

# Default timeout (seconds) for agent-generated screening response.
AGENT_RESPONSE_TIMEOUT_S = 3.0

IOS_PATTERNS: list[str] = [
    "record your name",
    "reason for calling",
    "see if this person is available",
    "state your name and reason",
]

ANDROID_PATTERNS: list[str] = [
    "using a screening service",
    "say your name and why",
    "google call screen",
    "screening service from google",
    "will get a copy of this conversation",
]

CARRIER_PATTERNS: list[str] = [
    "caller id screening",
    "identify yourself",
]

THIRD_PARTY_PATTERNS: list[str] = [
    "press 1 to be connected",
    "press one to be connected",
]

# Patterns that should NOT match screening (early media, voicemail, etc.)
EARLY_MEDIA_PHRASES: list[str] = [
    "this call may be monitored",
    "please hold while we connect",
]

# Known screening-related phrases from the callee side (not conversational).
_SCREENING_FOLLOW_UP_PATTERNS: list[str] = [
    "can you tell me more",
    "what is this about",
    "who is this",
    "why are you calling",
    "could you explain",
    "please elaborate",
    "tell me more",
    "one moment",
]

# Shared stopwords for coherence/overlap scoring across telephony modules.
COHERENCE_STOPWORDS: frozenset[str] = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "was",
        "were",
        "i",
        "you",
        "we",
        "they",
        "it",
        "and",
        "or",
        "but",
        "in",
        "on",
        "to",
        "for",
        "of",
        "with",
        "at",
        "by",
        "from",
        "do",
        "does",
        "did",
        "have",
        "has",
        "had",
        "be",
        "been",
        "am",
        "this",
        "that",
        "my",
        "your",
        "me",
        "him",
        "her",
        "what",
        "so",
        "um",
        "uh",
        "oh",
    }
)


@dataclass
class ScreeningPatternSet:
    """Configurable pattern sets for screening detection."""

    ios: list[str] = field(default_factory=lambda: list(IOS_PATTERNS))
    android: list[str] = field(default_factory=lambda: list(ANDROID_PATTERNS))
    carrier: list[str] = field(default_factory=lambda: list(CARRIER_PATTERNS))
    third_party: list[str] = field(default_factory=lambda: list(THIRD_PARTY_PATTERNS))
    exclusions: list[str] = field(default_factory=lambda: list(EARLY_MEDIA_PHRASES))


def match_screening_platform(
    text: str,
    patterns: ScreeningPatternSet | None = None,
) -> str | None:
    """Match transcript text against screening patterns.

    Returns the platform string (``"ios"``, ``"android"``, ``"carrier"``,
    ``"third_party"``) or ``None`` if no match.
    """
    if patterns is None:
        patterns = ScreeningPatternSet()

    lower = text.lower()

    # Check exclusions first.
    for phrase in patterns.exclusions:
        if phrase in lower:
            return None

    for phrase in patterns.ios:
        if phrase in lower:
            return "ios"
    for phrase in patterns.android:
        if phrase in lower:
            return "android"
    for phrase in patterns.carrier:
        if phrase in lower:
            return "carrier"
    for phrase in patterns.third_party:
        if phrase in lower:
            return "third_party"
    return None


class ScreeningState(Enum):
    WAITING = "waiting"
    SCREENING_DETECTED = "screening_detected"
    RESPONDING = "responding"
    HUMAN_ANSWERED = "human_answered"
    VOICEMAIL = "voicemail"
    DECLINED = "declined"
    SCREENING_TIMEOUT = "screening_timeout"


@dataclass(frozen=True)
class ScreeningResponse:
    """Emitted when the detector decides to respond to screening."""

    text: str
    mode: str  # "static" | "agent"


def is_conversational(text: str) -> bool:
    """Return True if *text* looks like a human conversational utterance.

    Uses structural heuristics rather than hardcoded phrase lists so that
    novel phrasing and non-English greetings are handled correctly.

    The core insight (backed by Twilio AMD research and Bland AI's findings):
    humans answer with **short utterances** (1-6 words) then pause; screening
    AIs and IVRs produce **long, structured sentences** (8+ words), often
    interrogative.

    Decision order:
      1. Reject known screening platform prompts (iOS/Android/carrier).
      2. Reject long interrogative sentences (screening AI follow-ups).
      3. Accept short utterances (≤6 content words) that aren't screening.
      4. Reject everything else (long non-question = voicemail greeting, etc.).
    """
    lower = text.strip().lower()
    if not lower:
        return False

    # ── Step 1: Reject known screening / IVR prompts ─────────────
    if match_screening_platform(lower) is not None:
        return False

    # ── Step 2: Reject long interrogative / instructional sentences ──
    # Screening AIs ask follow-up questions; humans don't interrogate
    # the caller.  We detect this structurally rather than matching
    # specific phrases.
    words = lower.split()
    word_count = len(words)

    # Interrogative starters used by screening bots.  We only need to
    # recognise the *structure* ("can you ...", "could you ...") — not
    # specific questions — so this generalises across phrasings and
    # languages that borrow English question words.
    _INTERROGATIVE_STARTERS = (
        "can you",
        "could you",
        "would you",
        "will you",
        "what is",
        "what's",
        "what are",
        "why are",
        "why do",
        "why is",
        "who is",
        "who are",
        "who's",
        "please ",
    )
    if word_count >= 6 and any(lower.startswith(q) for q in _INTERROGATIVE_STARTERS):
        return False

    # Long sentences (8+ words) that aren't questions are almost never
    # a human pickup — they're voicemail greetings or IVR announcements.
    # However, we still need the phrase-list backstop for medium-length
    # screening follow-ups (6-7 words) like "one moment" or "tell me more".
    for pattern in _SCREENING_FOLLOW_UP_PATTERNS:
        if pattern in lower:
            return False

    # ── Step 3: Accept short utterances ──────────────────────────
    # Humans typically answer with 1-8 words: "Hello?", "Yeah",
    # "Go ahead", "This is John speaking", "Hi how can I help you".
    # Threshold of 8 words covers natural greetings (including
    # receptionist pickups like "Hello how can I help you today")
    # while excluding voicemail greetings and IVR announcements which
    # are almost always 9+ words.  Screening follow-ups in the 6-8
    # word range are caught by the interrogative-starter check above.
    if word_count <= 8:
        return True

    # ── Step 4: Reject longer utterances ─────────────────────────
    # 9+ word non-interrogative utterances that don't match screening
    # are likely voicemail greetings, carrier announcements, or other
    # non-conversational speech.
    return False


def check_coherence(callee_texts: list[str], bot_texts: list[str]) -> bool:
    """Lightweight coherence check between callee and bot utterances.

    Returns ``True`` if the conversation seems coherent (likely human),
    ``False`` if responses appear incoherent (likely answer bot).

    Uses simple keyword overlap as a first-pass heuristic. A more
    sophisticated version could use sentence embeddings.
    """
    if len(callee_texts) < 2:
        return True  # Not enough data to judge.

    incoherent_turns = 0
    for i, callee_text in enumerate(callee_texts):
        callee_words = set(callee_text.lower().split())
        # Compare with both the bot text that prompted this response
        # and the prior callee text for topical continuity.
        context_words: set[str] = set()
        if i < len(bot_texts):
            context_words |= set(bot_texts[i].lower().split())
        if i > 0:
            context_words |= set(callee_texts[i - 1].lower().split())

        callee_content = callee_words - COHERENCE_STOPWORDS
        context_content = context_words - COHERENCE_STOPWORDS

        if not callee_content or not context_content:
            continue

        overlap = callee_content & context_content
        if len(overlap) == 0:
            incoherent_turns += 1

    return incoherent_turns < 2


class CallScreeningDetector:
    """Detects call screening by subscribing to STT partial transcripts.

    Emits :class:`CallScreening` when a screening prompt is detected.
    Optionally emits :class:`ScreeningResponse` with the identification text.
    After detection, tracks outcome: human pickup, voicemail, or declined.
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        call_sid: str = "",
        enabled: bool = True,
        screening_response: str = "",
        screening_use_agent: bool = False,
        agent_timeout_s: float = AGENT_RESPONSE_TIMEOUT_S,
        max_screening_turns: int = 3,
        patterns: ScreeningPatternSet | None = None,
        track_filter: str | None = None,
    ) -> None:
        self._event_bus = event_bus
        self._call_sid = call_sid
        self._enabled = enabled
        self._screening_response = screening_response
        self._screening_use_agent = screening_use_agent
        self._agent_timeout_s = agent_timeout_s
        self._max_screening_turns = max_screening_turns
        self._patterns = patterns or ScreeningPatternSet()
        self._track_filter = track_filter

        self._state = ScreeningState.WAITING
        self._detected = False
        self._call_answered = False
        self._pending_screening: tuple[str, str] | None = None  # (call_sid, platform)
        self._accumulated_text = ""
        self._screening_turns = 0
        self._started = False
        self._agent_timeout_task: asyncio.Task[None] | None = None

        # Multi-turn tracking.
        self._callee_texts: list[str] = []
        self._bot_texts: list[str] = []

    @property
    def state(self) -> ScreeningState:
        return self._state

    @property
    def screening_turns(self) -> int:
        return self._screening_turns

    def start(self) -> None:
        if not self._enabled:
            return
        self._event_bus.subscribe(CallAnswered, self._on_call_answered)
        self._event_bus.subscribe(STTPartial, self._on_stt_partial)
        self._event_bus.subscribe(STTFinal, self._on_stt_final)
        self._event_bus.subscribe(VoicemailDetected, self._on_voicemail)
        self._event_bus.subscribe(CallEnded, self._on_call_ended)
        self._started = True

    def stop(self) -> None:
        if self._started:
            self._event_bus.unsubscribe(CallAnswered, self._on_call_answered)
            self._event_bus.unsubscribe(STTPartial, self._on_stt_partial)
            self._event_bus.unsubscribe(STTFinal, self._on_stt_final)
            self._event_bus.unsubscribe(VoicemailDetected, self._on_voicemail)
            self._event_bus.unsubscribe(CallEnded, self._on_call_ended)
        self._cancel_agent_timeout()
        self._started = False
        self._reset_internal()

    def reset(self) -> None:
        self._cancel_agent_timeout()
        self._reset_internal()

    def _reset_internal(self) -> None:
        self._state = ScreeningState.WAITING
        self._detected = False
        self._call_answered = False
        self._pending_screening = None
        self._accumulated_text = ""
        self._screening_turns = 0
        self._callee_texts = []
        self._bot_texts = []

    def _cancel_agent_timeout(self) -> None:
        if self._agent_timeout_task and not self._agent_timeout_task.done():
            self._agent_timeout_task.cancel()
        self._agent_timeout_task = None

    def notify_agent_responded(self) -> None:
        """Signal that the agent has delivered its screening reply.

        Cancels the static-response fallback timer so the caller does not
        receive a duplicate reply.
        """
        self._cancel_agent_timeout()

    def record_bot_utterance(self, text: str) -> None:
        """Record a bot utterance for multi-turn coherence tracking."""
        self._bot_texts.append(text)

    def record_screening_turn(self, callee_text: str) -> None:
        """Record a screening turn from the callee side.

        Increments the turn counter and checks for max turns / coherence.
        """
        self._screening_turns += 1
        self._callee_texts.append(callee_text)

    async def _on_call_answered(self, event: CallAnswered) -> None:
        self._call_answered = True
        # If screening was detected before the call was answered (early media),
        # emit the deferred CallScreening event now.
        if self._pending_screening is not None:
            call_sid, platform = self._pending_screening
            self._pending_screening = None
            await self._emit_screening(call_sid, platform)

    async def _on_stt_partial(self, event: STTPartial) -> None:
        if self._detected:
            return

        # Track filtering: only analyze inbound (callee) audio.
        # If track_filter is set, skip events that either lack a track
        # attribute or carry a different track — prevents bot-side
        # transcripts from triggering false screening matches.
        if self._track_filter:
            if getattr(event, "track", None) != self._track_filter:
                return

        # Always use the latest partial — STT providers may revise/correct earlier text.
        text = event.text
        self._accumulated_text = text

        if len(self._accumulated_text) < MIN_TRANSCRIPT_LENGTH:
            return

        platform = match_screening_platform(self._accumulated_text, self._patterns)
        if platform is None:
            return

        self._detected = True
        self._state = ScreeningState.SCREENING_DETECTED

        if self._call_answered:
            await self._emit_screening(self._call_sid, platform)
        else:
            # Defer emission until CallAnswered arrives so the state machine
            # is in a state that can process the screening event.
            self._pending_screening = (self._call_sid, platform)

    async def _emit_screening(self, call_sid: str, platform: str) -> None:
        """Emit CallScreening and optional response events."""
        await self._event_bus.emit(CallScreening(call_sid=call_sid, platform=platform))

        # Emit screening response if configured.
        if self._screening_use_agent:
            self._state = ScreeningState.RESPONDING
            await self._event_bus.emit(ScreeningResponse(text="", mode="agent"))
            # Start agent timeout — fall back to static response if agent is slow.
            self._agent_timeout_task = asyncio.create_task(self._agent_timeout_fallback())
        elif self._screening_response:
            self._state = ScreeningState.RESPONDING
            await self._event_bus.emit(
                ScreeningResponse(text=self._screening_response, mode="static")
            )

    async def _agent_timeout_fallback(self) -> None:
        """Fall back to static response if agent doesn't respond in time."""
        try:
            await asyncio.sleep(self._agent_timeout_s)
            # Only emit fallback if still in a state that expects a response.
            if self._state == ScreeningState.RESPONDING and self._screening_response:
                await self._event_bus.emit(
                    ScreeningResponse(text=self._screening_response, mode="static")
                )
        except asyncio.CancelledError:
            pass

    async def _on_stt_final(self, event: STTFinal) -> None:
        """Handle final transcript after screening detected."""
        if not self._detected:
            return
        if self._state in (
            ScreeningState.HUMAN_ANSWERED,
            ScreeningState.VOICEMAIL,
            ScreeningState.DECLINED,
            ScreeningState.SCREENING_TIMEOUT,
        ):
            return

        text = event.text.strip()
        if not text:
            return

        # Track filtering for multi-turn.
        if self._track_filter and hasattr(event, "track"):
            if getattr(event, "track", None) != self._track_filter:
                return

        # Check if this looks like a human answering (conversational speech)
        # *before* enforcing the turn limit, so a human picking up on the
        # last allowed exchange is classified as HUMAN_ANSWERED, not timeout.
        if is_conversational(text):
            self._state = ScreeningState.HUMAN_ANSWERED
            self._cancel_agent_timeout()
            return

        # Record as a screening turn.
        self.record_screening_turn(text)

        # Check max screening turns.
        if self._screening_turns >= self._max_screening_turns:
            self._state = ScreeningState.SCREENING_TIMEOUT
            logger.info("Max screening turns (%d) reached", self._max_screening_turns)
            await self._event_bus.emit(ScreeningTimedOut(call_sid=self._call_sid))
            return

    async def _on_voicemail(self, event: VoicemailDetected) -> None:
        """Handle voicemail detection after screening."""
        if not self._detected:
            return
        if self._state in (
            ScreeningState.HUMAN_ANSWERED,
            ScreeningState.VOICEMAIL,
            ScreeningState.DECLINED,
            ScreeningState.SCREENING_TIMEOUT,
        ):
            return
        if event.result == "machine":
            self._state = ScreeningState.VOICEMAIL
            self._cancel_agent_timeout()

    async def _on_call_ended(self, event: CallEnded) -> None:
        """Handle call ended during screening — callee declined."""
        if not self._detected:
            return
        if self._state in (
            ScreeningState.HUMAN_ANSWERED,
            ScreeningState.VOICEMAIL,
            ScreeningState.DECLINED,
            ScreeningState.SCREENING_TIMEOUT,
        ):
            return
        self._state = ScreeningState.DECLINED
        self._cancel_agent_timeout()

    def is_coherent(self) -> bool:
        """Check whether the multi-turn screening conversation is coherent."""
        return check_coherence(self._callee_texts, self._bot_texts)
