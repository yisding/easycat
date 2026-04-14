"""Stage protocol and shared types for the EasyCat pipeline.

Every pipeline stage (STT, TTS, VAD, Agent, etc.) implements the
``Stage`` protocol.  Stages are thin wrappers around existing providers
that add journal recording and a uniform ``execute`` / ``snapshot_state``
surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from easycat.runtime.context import RunContext
from easycat.session._turn_context import TurnContext

if TYPE_CHECKING:
    # Annotation-only import.  At runtime ``ReplaySpec`` resolves via
    # ``__getattr__`` below so we stay clear of the import cycle with
    # ``runtime.replay`` (which imports NONDETERMINISTIC_FIELDS from here).
    from easycat.runtime.replay import ReplaySpec

# ── Control signals ──────────────────────────────────────────────


@dataclass(frozen=True)
class ControlSignal:
    """Base for upstream control signals."""

    signal_id: str


@dataclass(frozen=True)
class InterruptSignal(ControlSignal):
    """User barged in — cancel downstream and truncate."""


@dataclass(frozen=True)
class CancelSignal(ControlSignal):
    """Cancel the current operation."""


@dataclass(frozen=True)
class PauseSignal(ControlSignal):
    """Pause the stage (e.g. during hold)."""


@dataclass(frozen=True)
class ResumeSignal(ControlSignal):
    """Resume a previously paused stage."""


@dataclass(frozen=True)
class BackpressureSignal(ControlSignal):
    """Downstream is overwhelmed — slow down."""


# ── Stage state ──────────────────────────────────────────────────


@dataclass(frozen=True)
class StageStateSnapshot:
    """Serialisable snapshot of a stage's internal state."""

    stage_name: str
    fields: dict[str, Any] = field(default_factory=dict)
    state_ref: str | None = None


# ── Stage protocol ───────────────────────────────────────────────


@runtime_checkable
class Stage(Protocol):
    """Uniform interface for every pipeline stage."""

    name: str

    async def execute(self, input: Any, ctx: RunContext, turn: TurnContext) -> Any:
        """Run the stage on *input* and return the result."""
        ...

    def snapshot_state(self) -> StageStateSnapshot:
        """Return a frozen snapshot of current internal state."""
        ...

    def replay(self, spec: ReplaySpec) -> Any:
        """Replay from journal/artifacts.  Stub until WS4."""
        ...

    async def handle_upstream(self, signal: ControlSignal) -> None:
        """React to an upstream control signal."""
        ...


# ── Non-deterministic fields ─────────────────────────────────────

NONDETERMINISTIC_FIELDS: frozenset[str] = frozenset(
    {
        "timing.wall_ns",
        "timing.cpu_ns",
        "timing.queue_ns",
        "timing.mono_ns",
        "recorded_at_monotonic_ns",
        "recorded_at_utc",
        "cursor.entered_at",
        "cursor.exited_at",
    }
)

# Extended in WS4 runtime/replay.py as REPLAY_IGNORE_FIELDS


# ── Lazy re-export: ReplaySpec lives in runtime.replay ──────────
# ``runtime.replay`` imports :data:`NONDETERMINISTIC_FIELDS` from this
# module, so a top-level ``from easycat.runtime.replay import
# ReplaySpec`` would deadlock during initial module load.  We defer
# the lookup to attribute access time instead.


def __getattr__(name: str) -> Any:  # pragma: no cover - trivial forwarder
    if name == "ReplaySpec":
        from easycat.runtime.replay import ReplaySpec

        return ReplaySpec
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
