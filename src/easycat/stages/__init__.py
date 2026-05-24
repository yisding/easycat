"""EasyCat pipeline stages.

Each stage wraps an existing provider with a uniform ``execute`` /
``snapshot_state`` / ``handle_upstream`` surface and optional journal
recording.

Exports load lazily via PEP 562 ``__getattr__`` so importing the
package doesn't pull every stage module (and the cycle with
``runtime.replay``) — only the symbols a caller actually touches.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

_LAZY: dict[str, tuple[str, str]] = {
    "AgentStage": ("easycat.stages.agent", "AgentStage"),
    "AudioStage": ("easycat.stages.audio", "AudioStage"),
    "BackpressureSignal": ("easycat.stages.base", "BackpressureSignal"),
    "CancelSignal": ("easycat.stages.base", "CancelSignal"),
    "ControlSignal": ("easycat.stages.base", "ControlSignal"),
    "InterruptSignal": ("easycat.stages.base", "InterruptSignal"),
    "NONDETERMINISTIC_FIELDS": ("easycat.stages.base", "NONDETERMINISTIC_FIELDS"),
    "PauseSignal": ("easycat.stages.base", "PauseSignal"),
    "ReplaySpec": ("easycat.runtime.replay", "ReplaySpec"),
    "ResumeSignal": ("easycat.stages.base", "ResumeSignal"),
    "STTStage": ("easycat.stages.stt", "STTStage"),
    "Stage": ("easycat.stages.base", "Stage"),
    "StageStateSnapshot": ("easycat.stages.base", "StageStateSnapshot"),
    "TTSStage": ("easycat.stages.tts", "TTSStage"),
    "TransportStage": ("easycat.stages.transport", "TransportStage"),
    "TurnStage": ("easycat.stages.turn", "TurnStage"),
    "VADStage": ("easycat.stages.vad", "VADStage"),
}


if TYPE_CHECKING:
    from easycat.runtime.replay import ReplaySpec
    from easycat.stages.agent import AgentStage
    from easycat.stages.audio import AudioStage
    from easycat.stages.base import (
        NONDETERMINISTIC_FIELDS,
        BackpressureSignal,
        CancelSignal,
        ControlSignal,
        InterruptSignal,
        PauseSignal,
        ResumeSignal,
        Stage,
        StageStateSnapshot,
    )
    from easycat.stages.stt import STTStage
    from easycat.stages.transport import TransportStage
    from easycat.stages.tts import TTSStage
    from easycat.stages.turn import TurnStage
    from easycat.stages.vad import VADStage


def __getattr__(name: str):  # PEP 562
    try:
        module_path, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module 'easycat.stages' has no attribute {name!r}") from None
    module = importlib.import_module(module_path)
    value = getattr(module, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(list(globals()) + list(_LAZY)))


__all__ = sorted(_LAZY)
