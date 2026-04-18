"""EasyCat pipeline stages.

Each stage wraps an existing provider with a uniform ``execute`` /
``snapshot_state`` / ``handle_upstream`` surface and optional journal
recording.

**Import order matters.** The ``ReplaySpec`` re-export below must come
*before* the stage-module imports, because those modules import
``ReplayCassette``/``ReplayFidelity``/``ReplaySpec`` from
``easycat.runtime.replay``, and ``replay.py`` imports
``NONDETERMINISTIC_FIELDS`` from ``stages.base``.  Loading replay
first breaks the cycle cleanly — replay defines everything it needs
before any stage module tries to pull it.
"""

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
from easycat.stages.telephony import TelephonyStage
from easycat.stages.transport import TransportStage
from easycat.stages.tts import TTSStage
from easycat.stages.turn import TurnStage
from easycat.stages.vad import VADStage

__all__ = [
    "AgentStage",
    "AudioStage",
    "BackpressureSignal",
    "CancelSignal",
    "ControlSignal",
    "InterruptSignal",
    "NONDETERMINISTIC_FIELDS",
    "PauseSignal",
    "ReplaySpec",
    "ResumeSignal",
    "STTStage",
    "Stage",
    "StageStateSnapshot",
    "TelephonyStage",
    "TransportStage",
    "TTSStage",
    "TurnStage",
    "VADStage",
]


# ``ReplaySpec`` is re-exported at import time (see module docstring).
# No PEP 562 ``__getattr__`` hook is needed.
