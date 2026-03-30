"""Conversation coherence and early media detection utilities."""

from __future__ import annotations

from easycat.telephony.screening import EARLY_MEDIA_PHRASES


class ConversationCoherenceDetector:
    """Detects incoherent callee responses that suggest an answer bot.

    Uses lightweight heuristics (keyword overlap) as a first pass.
    Can be upgraded to use sentence embeddings for more accuracy.
    """

    def __init__(self, *, min_turns: int = 2, coherence_threshold: float = 0.3) -> None:
        self._min_turns = min_turns
        self._coherence_threshold = coherence_threshold
        self._callee_turns: list[str] = []
        self._bot_turns: list[str] = []

    def add_callee_turn(self, text: str) -> None:
        self._callee_turns.append(text)

    def add_bot_turn(self, text: str) -> None:
        self._bot_turns.append(text)

    def is_coherent(self) -> bool:
        """Check if the conversation appears coherent (likely human)."""
        if len(self._callee_turns) < self._min_turns:
            return True

        from easycat.telephony.screening import check_coherence

        return check_coherence(self._callee_turns, self._bot_turns)

    def coherence_score(self) -> float:
        """Return a coherence score (0.0 = incoherent, 1.0 = coherent)."""
        if len(self._callee_turns) < self._min_turns:
            return 1.0

        from easycat.telephony.screening import coherence_score

        return coherence_score(self._callee_turns, self._bot_turns)

    def reset(self) -> None:
        self._callee_turns.clear()
        self._bot_turns.clear()


class EarlyMediaDetector:
    """Detects and handles early media (audio before call answer).

    Early media arrives on the media stream before the `in-progress`
    callback from Twilio. During this phase, classification should be
    delayed to avoid misclassifying carrier announcements as IVR or
    screening prompts.
    """

    def __init__(self) -> None:
        self._call_answered = False
        self._early_media_texts: list[str] = []

    @property
    def in_early_media(self) -> bool:
        return not self._call_answered

    def on_call_answered(self) -> None:
        """Mark that the call has been answered — end early media phase."""
        self._call_answered = True

    def record_early_text(self, text: str) -> None:
        """Record text received during early media phase."""
        if self.in_early_media:
            self._early_media_texts.append(text)

    def is_early_media_text(self, text: str) -> bool:
        """Check if text matches common early media announcements."""
        lower = text.lower()
        return any(p in lower for p in EARLY_MEDIA_PHRASES)

    def reset(self) -> None:
        self._call_answered = False
        self._early_media_texts.clear()
