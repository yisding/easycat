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

        from easycat.telephony.screening import COHERENCE_STOPWORDS

        total_overlap = 0
        comparisons = 0

        sw = COHERENCE_STOPWORDS
        for i, callee_text in enumerate(self._callee_turns):
            callee_words = set(callee_text.lower().split()) - sw
            context_words: set[str] = set()
            if i < len(self._bot_turns):
                context_words |= set(self._bot_turns[i].lower().split()) - sw
            if i > 0:
                context_words |= set(self._callee_turns[i - 1].lower().split()) - sw

            if not callee_words or not context_words:
                continue

            overlap = len(callee_words & context_words)
            max_possible = min(len(callee_words), len(context_words))
            total_overlap += overlap / max_possible if max_possible > 0 else 0
            comparisons += 1

        return total_overlap / comparisons if comparisons > 0 else 1.0

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
        self._in_early_media = True
        self._call_answered = False
        self._early_media_texts: list[str] = []

    @property
    def in_early_media(self) -> bool:
        return self._in_early_media and not self._call_answered

    def on_call_answered(self) -> None:
        """Mark that the call has been answered — end early media phase."""
        self._call_answered = True
        self._in_early_media = False

    def record_early_text(self, text: str) -> None:
        """Record text received during early media phase."""
        if self.in_early_media:
            self._early_media_texts.append(text)

    def is_early_media_text(self, text: str) -> bool:
        """Check if text matches common early media announcements."""
        lower = text.lower()
        return any(p in lower for p in EARLY_MEDIA_PHRASES)

    def reset(self) -> None:
        self._in_early_media = True
        self._call_answered = False
        self._early_media_texts.clear()
