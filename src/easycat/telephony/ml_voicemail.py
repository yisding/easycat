"""ML-based voicemail detection interface.

Provides a pluggable interface for ML-powered voicemail classification
(e.g., Bland AI's Wave2Vec model). Falls back gracefully to heuristic
detection when the model is not available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MLClassificationResult:
    """Result from ML voicemail classifier."""

    result: str  # "human" | "machine"
    confidence: float  # 0.0-1.0
    latency_ms: float  # Time taken for classification


class MLVoicemailDetector:
    """ML-powered voicemail detector interface.

    Wraps an optional ML model (e.g., Wave2Vec) for high-accuracy
    voicemail detection from audio. Falls back gracefully when the
    model is not installed.

    To use a real model, subclass and override :meth:`classify_audio`.
    """

    def __init__(
        self,
        *,
        model_path: str = "",
        confidence_threshold: float = 0.7,
    ) -> None:
        self._model_path = model_path
        self._confidence_threshold = confidence_threshold
        self._model_loaded = False
        self._model: object | None = None

    @classmethod
    def is_available(cls) -> bool:
        """Check if the ML model dependencies are installed."""
        try:
            import onnxruntime  # noqa: F401

            return True
        except ImportError:
            return False

    def load_model(self) -> bool:
        """Attempt to load the ML model. Returns True on success."""
        if not self._model_path:
            return False
        try:
            import onnxruntime as ort

            self._model = ort.InferenceSession(self._model_path)
            self._model_loaded = True
            return True
        except Exception:
            logger.warning("Failed to load ML voicemail model from %s", self._model_path)
            self._model_loaded = False
            return False

    async def classify_audio(
        self, pcm16_data: bytes, sample_rate: int = 16000
    ) -> MLClassificationResult | None:
        """Classify a 2-second audio window.

        Args:
            pcm16_data: Raw PCM16 audio bytes (ideally ~2 seconds).
            sample_rate: Sample rate of the audio.

        Returns:
            Classification result, or None if the model is not available.
        """
        if not self._model_loaded:
            return None

        # Placeholder for actual model inference.
        # A real implementation would:
        # 1. Resample audio to model's expected sample rate
        # 2. Extract features (e.g., log-mel spectrograms)
        # 3. Run ONNX inference
        # 4. Map output to "human" or "machine"
        #
        # For now, return None to indicate graceful fallback.
        return None

    def classify_audio_sync(
        self, pcm16_data: bytes, sample_rate: int = 16000
    ) -> MLClassificationResult | None:
        """Synchronous version for use in thread executor."""
        if not self._model_loaded:
            return None
        return None


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
        early_patterns = [
            "this call may be monitored",
            "call may be recorded",
            "please hold while we connect",
            "your call is important",
        ]
        return any(p in lower for p in early_patterns)

    def reset(self) -> None:
        self._in_early_media = True
        self._call_answered = False
        self._early_media_texts.clear()
