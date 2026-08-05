"""Call screening detection: pattern matching against STT transcripts."""

from __future__ import annotations

__all__ = [
    "CallScreeningDetector",
    "ScreeningPatternSet",
    "ScreeningResponse",
    "ScreeningState",
    "check_coherence",
    "coherence_score",
    "is_conversational",
    "match_screening_platform",
    "screening_patterns_for_languages",
]

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from easycat.events import (
    CallAnswered,
    CallEnded,
    CallInitiated,
    CallScreening,
    EventBus,
    ScreeningResponse,
    ScreeningTimedOut,
    STTFinal,
    STTPartial,
    VoicemailDetected,
)
from easycat.runtime.scope import BackgroundTaskScope
from easycat.telephony.voicemail import _CallBoundaryAcceptor

logger = logging.getLogger(__name__)
_AGENT_RESPONSE_TIMER_MEMBER = "screening_agent_response_timeout"

ScreeningPlatform = Literal["ios", "android", "carrier", "third_party"]

# Default timeout (seconds) for agent-generated screening response.
AGENT_RESPONSE_TIMEOUT_S = 3.0

IOS_PATTERNS: list[str] = [
    "record your name",
    "reason for calling",
    "see if this person is available",
    "state your name and reason",
    "if you record your name",
    "name and reason for calling",
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
    "does not accept unidentified calls",
    "does not accept calls from unidentified",
    "anonymous call rejection",
]

THIRD_PARTY_PATTERNS: list[str] = [
    "press 1 to be connected",
    "press one to be connected",
    "nomorobo",
    "youmail",
    "robokiller",
    "truecaller",
]

# ── Per-language pattern registries ──────────────────────────────
# Key substrings from localized iOS 26 screening prompts.
# iOS 26 supports: en, es, fr, de, pt, ja, ko, zh (Mandarin/Cantonese).

_IOS_PATTERNS_BY_LANG: dict[str, list[str]] = {
    "en": IOS_PATTERNS,
    "es": [
        "grabe su nombre",
        "motivo de la llamada",
        "si esta persona está disponible",
        "diga su nombre y el motivo",
        "razón de su llamada",
    ],
    "fr": [
        "enregistrez votre nom",
        "raison de votre appel",
        "si cette personne est disponible",
        "indiquez votre nom et la raison",
        "motif de votre appel",
    ],
    "de": [
        "ihren namen aufnehmen",
        "grund ihres anrufs",
        "ob diese person verfügbar ist",
        "nennen sie ihren namen",
        "grund ihres anrufes",
    ],
    "pt": [
        "grave seu nome",
        "motivo da ligação",
        "se essa pessoa está disponível",
        "diga seu nome e o motivo",
        "razão da chamada",
    ],
    "ja": [
        "お名前と",
        "お電話の理由",
        "対応可能か確認",
        "録音してください",
    ],
    "ko": [
        "이름과",
        "전화하시는 이유",
        "통화 가능한지",
        "녹음해 주시면",
    ],
    "zh": [
        "录下您的名字",
        "来电原因",
        "是否有空",
        "请说明您的姓名",
    ],
}

# Localized Google Call Screen prompts.
_ANDROID_PATTERNS_BY_LANG: dict[str, list[str]] = {
    "en": ANDROID_PATTERNS,
    "es": [
        "servicio de filtrado",
        "diga su nombre y por qué",
        "filtrado de llamadas de google",
        "recibirá una copia de esta conversación",
    ],
    "fr": [
        "service de filtrage",
        "dites votre nom et pourquoi",
        "filtrage d'appels de google",
        "recevra une copie de cette conversation",
    ],
    "de": [
        "einen anruffilter",
        "sagen sie ihren namen und warum",
        "anruffilter von google",
        "erhält eine kopie dieses gesprächs",
    ],
    "pt": [
        "serviço de triagem",
        "diga seu nome e por que",
        "triagem de chamadas do google",
        "receberá uma cópia desta conversa",
    ],
    "ja": [
        "通話スクリーニング",
        "お名前とご用件を",
        "googleの通話スクリーニング",
        "会話のコピーが届きます",
    ],
}

# Localized carrier patterns.
_CARRIER_PATTERNS_BY_LANG: dict[str, list[str]] = {
    "en": CARRIER_PATTERNS,
    "es": [
        "filtrado de identificador",
        "no acepta llamadas no identificadas",
    ],
    "fr": [
        "filtrage d'identité",
        "n'accepte pas les appels non identifiés",
    ],
    "de": [
        "anrufer-id-überprüfung",
        "akzeptiert keine unbekannten anrufe",
    ],
    "pt": [
        "triagem de identificação",
        "não aceita chamadas não identificadas",
    ],
}

_LOCALIZED_PATTERN_CATALOGS: tuple[Mapping[str, Sequence[str]], ...] = (
    _IOS_PATTERNS_BY_LANG,
    _ANDROID_PATTERNS_BY_LANG,
    _CARRIER_PATTERNS_BY_LANG,
)

# Patterns that should NOT match screening (early media, voicemail, etc.)
EARLY_MEDIA_PHRASES: list[str] = [
    "this call may be monitored",
    "call may be recorded",
    "please hold while we connect",
    "your call is important",
]

# Known screening-related phrases from the callee side (not conversational).
# Only include phrases that are clearly automated screening prompts — short
# interrogative/imperative phrases like "who is this" or "why are you calling"
# are common human handoff utterances and must NOT be blocked here, otherwise
# OutboundCallStateMachine stays stuck in SCREENING when a real person picks up.
_SCREENING_FOLLOW_UP_PATTERNS: list[str] = [
    "can you tell me more",
    "could you explain",
    "please elaborate",
    "tell me more",
]

# Interrogative starters used by screening bots.  Recognised structurally
# ("can you ...", "could you ...") so this generalises across phrasings.
_INTERROGATIVE_STARTERS: tuple[str, ...] = (
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
        "just",
        "like",
        "well",
        "okay",
        "ok",
        "yeah",
        "yes",
        "no",
        "not",
        "actually",
        "basically",
    }
)


def _normalize_screening_patterns(patterns: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(pattern.strip().casefold() for pattern in patterns))
    if any(not pattern for pattern in normalized):
        raise ValueError("screening patterns must not be empty")
    return normalized


@dataclass(frozen=True, slots=True)
class ScreeningPatternSet:
    """Immutable, normalized pattern policy for screening detection.

    Input sequences are stripped, case-folded, deduplicated, and stored as
    tuples. Blank patterns are rejected because an empty substring would match
    every transcript.
    """

    ios: Sequence[str] = field(default_factory=lambda: tuple(IOS_PATTERNS))
    android: Sequence[str] = field(default_factory=lambda: tuple(ANDROID_PATTERNS))
    carrier: Sequence[str] = field(default_factory=lambda: tuple(CARRIER_PATTERNS))
    third_party: Sequence[str] = field(default_factory=lambda: tuple(THIRD_PARTY_PATTERNS))
    exclusions: Sequence[str] = field(default_factory=lambda: tuple(EARLY_MEDIA_PHRASES))

    def __post_init__(self) -> None:
        object.__setattr__(self, "ios", _normalize_screening_patterns(self.ios))
        object.__setattr__(self, "android", _normalize_screening_patterns(self.android))
        object.__setattr__(self, "carrier", _normalize_screening_patterns(self.carrier))
        object.__setattr__(self, "third_party", _normalize_screening_patterns(self.third_party))
        object.__setattr__(self, "exclusions", _normalize_screening_patterns(self.exclusions))

    def _platform_patterns(
        self,
    ) -> tuple[tuple[ScreeningPlatform, Sequence[str]], ...]:
        """Return match groups in their authoritative precedence order."""
        return (
            ("ios", self.ios),
            ("android", self.android),
            ("carrier", self.carrier),
            ("third_party", self.third_party),
        )


_DEFAULT_SCREENING_PATTERNS = ScreeningPatternSet()


def _localized_patterns_for_languages(
    patterns_by_language: Mapping[str, Sequence[str]],
    languages: set[str],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            pattern
            for language in sorted(languages)
            for pattern in patterns_by_language.get(language, ())
        )
    )


def screening_patterns_for_languages(
    languages: Sequence[str] | None = None,
) -> ScreeningPatternSet:
    """Build a :class:`ScreeningPatternSet` with patterns for the given languages.

    Args:
        languages: BCP-47 language codes (e.g. ``["en", "es", "fr"]``).
            ``None`` includes **all** available languages.

    Returns:
        A ``ScreeningPatternSet`` whose ``ios``, ``android``, and ``carrier``
        sequences contain deduplicated patterns for every requested language.
        Third-party and exclusion patterns are language-independent.
    """
    if languages is None:
        langs = {language for catalog in _LOCALIZED_PATTERN_CATALOGS for language in catalog}
    else:
        langs = {code.split("-")[0].casefold() for code in languages}

    return ScreeningPatternSet(
        ios=_localized_patterns_for_languages(_IOS_PATTERNS_BY_LANG, langs),
        android=_localized_patterns_for_languages(_ANDROID_PATTERNS_BY_LANG, langs),
        carrier=_localized_patterns_for_languages(_CARRIER_PATTERNS_BY_LANG, langs),
    )


def match_screening_platform(
    text: str,
    patterns: ScreeningPatternSet | None = None,
    *,
    _pre_casefolded: bool = False,
) -> ScreeningPlatform | None:
    """Match transcript text against screening patterns.

    Returns the platform string (``"ios"``, ``"android"``, ``"carrier"``,
    ``"third_party"``) or ``None`` if no match.
    """
    if patterns is None:
        patterns = _DEFAULT_SCREENING_PATTERNS

    normalized = text if _pre_casefolded else text.casefold()

    if any(phrase in normalized for phrase in patterns.exclusions):
        return None

    for platform, platform_patterns in patterns._platform_patterns():
        if any(phrase in normalized for phrase in platform_patterns):
            return platform
    return None


class ScreeningState(Enum):
    WAITING = "waiting"
    SCREENING_DETECTED = "screening_detected"
    RESPONDING = "responding"
    HUMAN_ANSWERED = "human_answered"
    VOICEMAIL = "voicemail"
    DECLINED = "declined"
    SCREENING_TIMEOUT = "screening_timeout"


_TERMINAL_SCREENING_STATES = frozenset(
    {
        ScreeningState.HUMAN_ANSWERED,
        ScreeningState.VOICEMAIL,
        ScreeningState.DECLINED,
        ScreeningState.SCREENING_TIMEOUT,
    }
)


def is_conversational(
    text: str,
    patterns: ScreeningPatternSet | None = None,
    *,
    max_words: int = 8,
) -> bool:
    """Return True if *text* looks like a human conversational utterance.

    Uses structural heuristics rather than hardcoded phrase lists so that
    novel phrasing and non-English greetings are handled correctly.

    Args:
        text: Transcript text to classify.
        patterns: Screening pattern set for exclusion checks.
        max_words: Maximum word count to accept as conversational (default 8).
            Utterances with more words are rejected as likely voicemail/IVR.

    The core insight (backed by Twilio AMD research and Bland AI's findings):
    humans answer with **short utterances** (1-6 words) then pause; screening
    AIs and IVRs produce **long, structured sentences** (8+ words), often
    interrogative.

    Decision order:
      1. Reject known screening platform prompts (iOS/Android/carrier).
      2. Reject long interrogative sentences (screening AI follow-ups).
      3. Accept short utterances (≤ *max_words*) that aren't screening.
      4. Reject everything else (long non-question = voicemail greeting, etc.).
    """
    normalized = text.strip().casefold()
    if not normalized:
        return False

    # ── Step 1: Reject known screening / IVR prompts ─────────────
    if match_screening_platform(normalized, patterns, _pre_casefolded=True) is not None:
        return False

    # ── Step 2: Reject long interrogative / instructional sentences ──
    # Screening AIs ask follow-up questions; humans don't interrogate
    # the caller.  We detect this structurally rather than matching
    # specific phrases.
    words = normalized.split()
    word_count = len(words)

    if word_count >= 6 and any(normalized.startswith(q) for q in _INTERROGATIVE_STARTERS):
        return False

    # Long sentences (8+ words) that aren't questions are almost never
    # a human pickup — they're voicemail greetings or IVR announcements.
    # However, we still need the phrase-list backstop for medium-length
    # screening follow-ups (6-7 words) like "one moment" or "tell me more".
    for pattern in _SCREENING_FOLLOW_UP_PATTERNS:
        if pattern in normalized:
            return False

    # ── Step 3: Accept short utterances ──────────────────────────
    # Humans typically answer with 1-8 words: "Hello?", "Yeah",
    # "Go ahead", "This is John speaking", "Hi how can I help you".
    # Default threshold of 8 words covers natural greetings (including
    # receptionist pickups like "Hello how can I help you today")
    # while excluding voicemail greetings and IVR announcements which
    # are almost always 9+ words.  Screening follow-ups in the 6-8
    # word range are caught by the interrogative-starter check above.
    # ── Step 4: Reject longer utterances ─────────────────────────
    # Utterances exceeding max_words that don't match screening
    # are likely voicemail greetings, carrier announcements, or other
    # non-conversational speech.
    return word_count <= max_words


def coherence_score(callee_texts: list[str], bot_texts: list[str]) -> float:
    """Compute keyword-overlap coherence score between callee and bot utterances.

    Returns a float in [0.0, 1.0] where 1.0 = fully coherent.
    """
    if len(callee_texts) < 2:
        return 1.0

    total_overlap = 0.0
    comparisons = 0

    for i, callee_text in enumerate(callee_texts):
        callee_words = set(callee_text.lower().split()) - COHERENCE_STOPWORDS
        context_words: set[str] = set()
        if i < len(bot_texts):
            context_words |= set(bot_texts[i].lower().split()) - COHERENCE_STOPWORDS
        if i > 0:
            context_words |= set(callee_texts[i - 1].lower().split()) - COHERENCE_STOPWORDS

        if not callee_words or not context_words:
            continue

        overlap = len(callee_words & context_words)
        max_possible = min(len(callee_words), len(context_words))
        total_overlap += overlap / max_possible if max_possible > 0 else 0
        comparisons += 1

    return total_overlap / comparisons if comparisons > 0 else 1.0


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
        call_boundary_acceptor: _CallBoundaryAcceptor | None = None,
    ) -> None:
        _validate_positive_number("agent_timeout_s", agent_timeout_s)
        _validate_positive_int("max_screening_turns", max_screening_turns)

        self._event_bus = event_bus
        self._call_sid = call_sid
        self._enabled = enabled
        self._screening_response = screening_response
        self._screening_use_agent = screening_use_agent
        self._agent_timeout_s = agent_timeout_s
        self._max_screening_turns = max_screening_turns
        self._patterns = patterns if patterns is not None else _DEFAULT_SCREENING_PATTERNS
        self._track_filter = track_filter
        self._call_boundary_acceptor = call_boundary_acceptor

        self._state = ScreeningState.WAITING
        self._call_answered = False
        # (call_sid, platform)
        self._pending_screening: tuple[str, ScreeningPlatform] | None = None
        self._accumulated_text = ""
        self._screening_turns = 0
        self._started = False
        self._timer_tasks = BackgroundTaskScope(name="call-screening")
        self._agent_timeout_task: asyncio.Task[None] | None = None
        self._agent_timeout_fallback_started = False

    @property
    def state(self) -> ScreeningState:
        return self._state

    @property
    def screening_turns(self) -> int:
        return self._screening_turns

    @property
    def accumulated_text(self) -> str:
        """The most recent accumulated STT partial text from screening."""
        return self._accumulated_text

    @property
    def screening_response(self) -> str:
        """The configured static screening response text."""
        return self._screening_response

    def start(self) -> None:
        if not self._enabled or self._started:
            return
        self._event_bus.subscribe(CallInitiated, self._on_call_initiated)
        self._event_bus.subscribe(CallAnswered, self._on_call_answered)
        self._event_bus.subscribe(STTPartial, self._on_stt_partial)
        self._event_bus.subscribe(STTFinal, self._on_stt_final)
        self._event_bus.subscribe(VoicemailDetected, self._on_voicemail)
        self._event_bus.subscribe(CallEnded, self._on_call_ended)
        self._started = True

    def stop(self) -> None:
        if self._started:
            self._event_bus.unsubscribe(CallInitiated, self._on_call_initiated)
            self._event_bus.unsubscribe(CallAnswered, self._on_call_answered)
            self._event_bus.unsubscribe(STTPartial, self._on_stt_partial)
            self._event_bus.unsubscribe(STTFinal, self._on_stt_final)
            self._event_bus.unsubscribe(VoicemailDetected, self._on_voicemail)
            self._event_bus.unsubscribe(CallEnded, self._on_call_ended)
        self._cancel_agent_timeout()
        self._started = False
        self._reset_internal()

    async def _on_call_initiated(self, event: CallInitiated) -> None:
        """Reset screening state for a new outbound call."""
        if not event.call_sid or event.call_sid == self._call_sid:
            return
        if self._call_boundary_acceptor is not None and not self._call_boundary_acceptor(
            event.call_sid, self._call_sid
        ):
            return
        self._cancel_agent_timeout()
        self._reset_internal()
        self._call_sid = event.call_sid

    def reset(self) -> None:
        self._cancel_agent_timeout()
        self._reset_internal()

    def _reset_internal(self) -> None:
        self._state = ScreeningState.WAITING
        self._call_answered = False
        self._pending_screening = None
        self._accumulated_text = ""
        self._screening_turns = 0
        self._agent_timeout_fallback_started = False

    def _cancel_agent_timeout(self) -> None:
        task = self._agent_timeout_task
        if task is None:
            return
        self._timer_tasks.cancel(_AGENT_RESPONSE_TIMER_MEMBER)
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        if task is not current and not task.done():
            task.cancel()
        if task is not current:
            self._agent_timeout_task = None

    def notify_agent_responded(self) -> bool:
        """Signal that the agent has delivered its screening reply.

        Cancels the static-response fallback timer so the caller does not
        receive a duplicate reply.

        Returns:
            ``True`` if the fallback timer was cancelled before it fired,
            ``False`` if the fallback already executed (caller should skip
            synthesis to avoid a duplicate response).
        """
        if self._agent_timeout_fallback_started:
            return False

        cancelled_in_time = (
            self._agent_timeout_task is not None and not self._agent_timeout_task.done()
        )
        self._cancel_agent_timeout()
        return cancelled_in_time

    def record_screening_turn(self, callee_text: str) -> None:
        """Record a screening turn from the callee side.

        Increments the turn counter so the detector can enforce the
        ``max_screening_turns`` limit.
        """
        self._screening_turns += 1

    async def _on_call_answered(self, event: CallAnswered) -> None:
        if self._call_sid and event.call_sid != self._call_sid:
            return
        self._call_answered = True
        if event.call_sid:
            self._call_sid = event.call_sid
        # If screening was detected before the call was answered (early media),
        # emit the deferred CallScreening event now.
        if self._pending_screening is not None:
            _, platform = self._pending_screening
            self._pending_screening = None
            # Use self._call_sid (just updated from event.call_sid) instead
            # of the stale value stored when screening was first detected
            # during early media — before CallAnswered provided the real SID.
            await self._emit_screening(self._call_sid, platform)

    async def _on_stt_partial(self, event: STTPartial) -> None:
        if self._state != ScreeningState.WAITING:
            return

        # Track filtering: only analyze inbound (callee) audio.
        # Skip events that carry a *different* explicit track — this prevents
        # bot-side transcripts from triggering false screening matches when
        # transcription_track="both".  A track-less event (track is None) is
        # accepted, mirroring call_state._on_stt_final, since most STT
        # providers do not stamp a track and the Twilio media transport
        # already drops outbound audio.
        if self._track_filter and event.track is not None and event.track != self._track_filter:
            return

        # Always use the latest partial — STT providers may revise/correct earlier text.
        text = event.text
        self._accumulated_text = text

        # Pattern matching uses exact substring checks, so short
        # transcripts (including CJK) cannot false-positive.
        platform = match_screening_platform(self._accumulated_text, self._patterns)
        if platform is None:
            return

        self._state = ScreeningState.SCREENING_DETECTED

        if self._call_answered:
            await self._emit_screening(self._call_sid, platform)
        else:
            # Defer emission until CallAnswered arrives so the state machine
            # is in a state that can process the screening event.
            self._pending_screening = (self._call_sid, platform)

    async def _emit_screening(self, call_sid: str, platform: ScreeningPlatform) -> None:
        """Emit CallScreening and optional response events."""
        await self._event_bus.emit(CallScreening(call_sid=call_sid, platform=platform))

        # Emit screening response if configured.
        if self._screening_use_agent:
            self._state = ScreeningState.RESPONDING
            # Start agent timeout BEFORE emitting so the fallback can fire
            # while EventBus.emit() awaits the (potentially slow) agent handler.
            self._agent_timeout_fallback_started = False
            self._agent_timeout_task = self._timer_tasks.create_task(
                _AGENT_RESPONSE_TIMER_MEMBER,
                self._agent_timeout_fallback(),
                replace=True,
            )
            await self._event_bus.emit(ScreeningResponse(text="", mode="agent"))
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
                self._agent_timeout_fallback_started = True
                await self._event_bus.emit(
                    ScreeningResponse(text=self._screening_response, mode="static")
                )
        finally:
            if self._agent_timeout_task is asyncio.current_task():
                self._agent_timeout_task = None

    async def _on_stt_final(self, event: STTFinal) -> None:
        """Handle final transcript after screening detected."""
        if self._state == ScreeningState.WAITING:
            return
        if self._state in _TERMINAL_SCREENING_STATES:
            return

        text = event.text.strip()
        if not text:
            return

        # Track filtering for multi-turn (track-less events accepted; see
        # _on_stt_partial).
        if self._track_filter and event.track is not None and event.track != self._track_filter:
            return

        # Check if this looks like a human answering (conversational speech)
        # *before* enforcing the turn limit, so a human picking up on the
        # last allowed exchange is classified as HUMAN_ANSWERED, not timeout.
        if is_conversational(text, self._patterns):
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
        if event.call_sid and self._call_sid and event.call_sid != self._call_sid:
            return
        if self._state == ScreeningState.WAITING:
            return
        if self._state in _TERMINAL_SCREENING_STATES:
            return
        if event.result == "machine":
            self._state = ScreeningState.VOICEMAIL
            self._cancel_agent_timeout()

    async def _on_call_ended(self, event: CallEnded) -> None:
        """Handle call ended during screening — callee declined."""
        if self._call_sid and event.call_sid != self._call_sid:
            return
        if self._state == ScreeningState.WAITING:
            return
        if self._state in _TERMINAL_SCREENING_STATES:
            return
        self._state = ScreeningState.DECLINED
        self._cancel_agent_timeout()


def _validate_positive_number(name: str, value: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a positive number")


def _validate_positive_int(name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
