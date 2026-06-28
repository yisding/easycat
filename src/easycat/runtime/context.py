"""RunContext: immutable execution context threaded through every stage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class RunContext:
    """Immutable context bag threaded through every stage invocation.

    ``runtime_mode`` distinguishes the two session flavours:

    * ``"chained_pipeline"`` — full voice pipeline
      (transport -> NR -> VAD -> STT -> agent -> TTS -> transport).
    * ``"text_session"`` — text-only mode (no audio stages, agent only).

    ``journal`` and ``artifact_store`` are typed as ``Any`` to avoid
    circular imports with the runtime package; at runtime they are
    ``ExecutionJournal | None`` and ``ArtifactStore | None``.
    """

    run_id: str
    session_id: str
    runtime_mode: Literal["chained_pipeline", "text_session"]
    journal: Any = None  # ExecutionJournal | None
    artifact_store: Any = None  # ArtifactStore | None
    config_snapshot: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.runtime_mode not in ("chained_pipeline", "text_session"):
            raise ValueError(
                f"Unsupported runtime_mode: {self.runtime_mode!r}. "
                "Must be 'chained_pipeline' or 'text_session'."
            )
