"""Call screening detection: pattern matching against STT transcripts."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from easycat.events import CallScreening, EventBus, STTPartial

logger = logging.getLogger(__name__)

# Minimum transcript length before screening patterns are checked,
# to prevent false-positive triggers on short human utterances.
MIN_TRANSCRIPT_LENGTH = 30

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


@dataclass(frozen=True)
class ScreeningResponse:
    """Emitted when the detector decides to respond to screening."""

    text: str
    mode: str  # "static" | "agent"


class CallScreeningDetector:
    """Detects call screening by subscribing to STT partial transcripts.

    Emits :class:`CallScreening` when a screening prompt is detected.
    Optionally emits :class:`ScreeningResponse` with the identification text.
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        call_sid: str = "",
        enabled: bool = True,
        screening_response: str = "",
        screening_use_agent: bool = False,
        max_screening_turns: int = 3,
        patterns: ScreeningPatternSet | None = None,
        track_filter: str | None = "inbound",
    ) -> None:
        self._event_bus = event_bus
        self._call_sid = call_sid
        self._enabled = enabled
        self._screening_response = screening_response
        self._screening_use_agent = screening_use_agent
        self._max_screening_turns = max_screening_turns
        self._patterns = patterns or ScreeningPatternSet()
        self._track_filter = track_filter

        self._state = ScreeningState.WAITING
        self._detected = False
        self._accumulated_text = ""
        self._screening_turns = 0
        self._started = False

    @property
    def state(self) -> ScreeningState:
        return self._state

    def start(self) -> None:
        if not self._enabled:
            return
        self._event_bus.subscribe(STTPartial, self._on_stt_partial)
        self._started = True

    def stop(self) -> None:
        if self._started:
            self._event_bus.unsubscribe(STTPartial, self._on_stt_partial)
        self._started = False
        self._reset_internal()

    def reset(self) -> None:
        self._reset_internal()

    def _reset_internal(self) -> None:
        self._state = ScreeningState.WAITING
        self._detected = False
        self._accumulated_text = ""
        self._screening_turns = 0

    async def _on_stt_partial(self, event: STTPartial) -> None:
        if self._detected:
            return

        # Track filtering: only analyze inbound (callee) audio.
        if self._track_filter and hasattr(event, "track"):
            if getattr(event, "track", None) != self._track_filter:
                return

        # Sliding window: use the latest partial (STTPartial replaces prior text).
        text = event.text
        if len(text) > len(self._accumulated_text):
            self._accumulated_text = text

        if len(self._accumulated_text) < MIN_TRANSCRIPT_LENGTH:
            return

        platform = match_screening_platform(self._accumulated_text, self._patterns)
        if platform is None:
            return

        self._detected = True
        self._state = ScreeningState.SCREENING_DETECTED

        await self._event_bus.emit(CallScreening(call_sid=self._call_sid, platform=platform))

        # Emit screening response if configured.
        if self._screening_use_agent:
            self._state = ScreeningState.RESPONDING
            await self._event_bus.emit(ScreeningResponse(text="", mode="agent"))
        elif self._screening_response:
            self._state = ScreeningState.RESPONDING
            await self._event_bus.emit(
                ScreeningResponse(text=self._screening_response, mode="static")
            )
