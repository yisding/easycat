"""Shared VAD base class, backend type, and small helpers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal, TypeAlias, cast

from easycat.events import Event, VADStartSpeaking, VADStopSpeaking

VADBackend: TypeAlias = Literal["auto", "silero", "funasr", "ten", "krisp"]
_VALID_VAD_BACKENDS: tuple[VADBackend, ...] = ("auto", "silero", "funasr", "ten", "krisp")

_DEFAULT_VAD_SENSITIVITY = 0.5


def _validate_vad_backend(backend: str) -> VADBackend:
    if backend not in _VALID_VAD_BACKENDS:
        allowed = ", ".join(_VALID_VAD_BACKENDS)
        raise ValueError(f"Unknown VAD backend '{backend}'. Expected one of: {allowed}.")
    return cast(VADBackend, backend)


# ── VAD base class ────────────────────────────────────────────────


class _VADBase:
    """Internal base class holding the shared VAD state machine.

    Provides the threshold + timing state, ``configure()``, the synchronous
    ``_evaluate_speech()`` generator, and ``reset()`` for the common variables.
    """

    def __init__(self) -> None:
        self._threshold: float = 0.5
        self._min_speech_duration_ms: int = 250
        self._min_silence_duration_ms: int = 150
        self._pre_roll_ms: int = 100
        self._post_roll_ms: int = 100

        # Internal state
        self._is_speaking: bool = False
        self._speech_start_time: float | None = None
        self._silence_start_time: float | None = None
        self._speech_confirmed: bool = False

    def configure(
        self,
        *,
        min_speech_duration_ms: int = 250,
        min_silence_duration_ms: int = 150,
        sensitivity: float = 0.5,
        pre_roll_ms: int = 100,
        post_roll_ms: int = 100,
    ) -> None:
        """Configure VAD thresholds and buffering parameters."""
        self._min_speech_duration_ms = min_speech_duration_ms
        self._min_silence_duration_ms = min_silence_duration_ms
        # Sensitivity maps inversely to threshold: higher sensitivity = lower threshold
        self._threshold = 1.0 - sensitivity
        self._pre_roll_ms = pre_roll_ms
        self._post_roll_ms = post_roll_ms

    def _evaluate_speech(self, speech_prob: float, now: float) -> Iterator[Event]:
        """Evaluate a single speech probability against the state machine.

        Yields VADStartSpeaking / VADStopSpeaking events as appropriate.
        """
        if speech_prob >= self._threshold:
            # Speech detected
            self._silence_start_time = None
            if not self._is_speaking:
                if self._speech_start_time is None:
                    self._speech_start_time = now
                if (
                    self._speech_start_time is not None
                    and not self._speech_confirmed
                    and (now - self._speech_start_time) * 1000 >= self._min_speech_duration_ms
                ):
                    self._is_speaking = True
                    self._speech_confirmed = True
                    yield VADStartSpeaking()
        else:
            # Silence detected
            self._speech_start_time = None
            self._speech_confirmed = False
            if self._is_speaking:
                if self._silence_start_time is None:
                    self._silence_start_time = now
                elif (now - self._silence_start_time) * 1000 >= self._min_silence_duration_ms:
                    self._is_speaking = False
                    self._silence_start_time = None
                    yield VADStopSpeaking()

    def reset(self) -> None:
        """Reset the common VAD state variables."""
        self._is_speaking = False
        self._speech_start_time = None
        self._silence_start_time = None
        self._speech_confirmed = False

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "unknown",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": "unknown",
        }
