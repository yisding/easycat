"""EasyCat pipeline stages.

Each stage wraps an existing provider with a uniform ``execute`` /
``snapshot_state`` / ``handle_upstream`` surface and optional journal
recording.
"""

from easycat.stages.agent import AgentStage
from easycat.stages.audio import AudioStage
from easycat.stages.base import (
    NONDETERMINISTIC_FIELDS,
    BackpressureSignal,
    CancelSignal,
    ControlSignal,
    InterruptSignal,
    PauseSignal,
    ReplaySpec,
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
