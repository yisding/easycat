"""Shared VAD base class, backend type, and small helpers."""

from __future__ import annotations

import math
from collections.abc import Iterator
from numbers import Real
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


def _validate_vad_sensitivity(sensitivity: float) -> None:
    if (
        not isinstance(sensitivity, Real)
        or isinstance(sensitivity, bool)
        or not math.isfinite(float(sensitivity))
    ):
        raise ValueError("sensitivity must be a number between 0 and 1")
    if not 0 <= sensitivity <= 1:
        raise ValueError("sensitivity must be between 0 and 1")


def _validate_non_negative_ms(name: str, value: int) -> None:
    if not isinstance(value, Real) or isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError(f"{name} must be a non-negative number")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _validate_positive_int(name: str, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


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
    ) -> None:
        """Configure VAD thresholds."""
        _validate_non_negative_ms("min_speech_duration_ms", min_speech_duration_ms)
        _validate_non_negative_ms("min_silence_duration_ms", min_silence_duration_ms)
        _validate_vad_sensitivity(sensitivity)

        self._min_speech_duration_ms = min_speech_duration_ms
        self._min_silence_duration_ms = min_silence_duration_ms
        # Sensitivity maps inversely to threshold: higher sensitivity = lower threshold
        self._threshold = 1.0 - sensitivity

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
                # Mirror the speech-side structure: fall through to the elapsed
                # check so that with min_silence_duration_ms=0 the stop event can
                # fire on the first silent frame, matching the speech path.
                if (
                    self._silence_start_time is not None
                    and (now - self._silence_start_time) * 1000 >= self._min_silence_duration_ms
                ):
                    self._is_speaking = False
                    self._silence_start_time = None
                    yield VADStopSpeaking()

    def reset(self) -> None:
        """Reset the common VAD state variables."""
        self._is_speaking = False
        self._speech_start_time = None
        self._silence_start_time = None
        self._speech_confirmed = False

    def close(self) -> None:
        """Release any native model/session handle held by the backend.

        Default behaviour drops the ``_model``/``_session`` reference (if the
        subclass holds one) so the underlying onnxruntime session or native
        handle is reclaimed promptly rather than waiting for GC.  Backends with
        a richer wrapper (e.g. an inner model exposing its own ``close``) or a
        differently-named handle override this; see ``SileroVAD``/``TenVAD``.
        This makes the cleanup contract uniform across the subpackage so the
        ``close`` hook invoked by ``Session.stop()`` is honoured by every
        backend, not just the commercial ones.
        """
        inner_close = getattr(getattr(self, "_model", None), "close", None)
        if callable(inner_close):
            try:
                inner_close()
            except Exception:
                pass
        if hasattr(self, "_model"):
            self._model = None
        if hasattr(self, "_session"):
            self._session = None

    def version_info(self) -> dict[str, str]:
        return {
            "provider": "unknown",
            "model": "unknown",
            "api_version": "unknown",
            "sdk_version": "unknown",
        }
