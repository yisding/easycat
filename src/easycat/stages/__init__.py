"""EasyCat pipeline stages.

Each stage wraps an existing provider with a uniform ``execute`` /
``snapshot_state`` / ``handle_upstream`` surface and optional journal
recording.

Stage classes (``STTStage``, ``TTSStage``, ``AgentStage``, etc.),
control signals (``InterruptSignal``, ``CancelSignal``, …), and the
``Stage`` / ``StageStateSnapshot`` / ``ReplaySpec`` types are
extension-author surfaces — import them from their submodules
(``easycat.stages.base``, ``easycat.stages.stt``, …) directly.
"""
