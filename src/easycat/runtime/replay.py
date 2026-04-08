"""ReplaySpec and fidelity types for WS4 replay support.

Replaces the stub ``ReplaySpec`` in ``stages.base`` with a fully
specified replay configuration including fidelity levels, tool
policies, and timing modes.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Literal

from easycat.stages.base import NONDETERMINISTIC_FIELDS


class ReplayFidelity(enum.Enum):
    ARTIFACT = "artifact"
    SIMULATED = "simulated"
    LIVE = "live"


class ToolReplayPolicy(enum.Enum):
    DENY = "deny"
    STUB = "stub"
    ALLOW = "allow"


class ReplaySideEffectBlocked(RuntimeError):
    """Raised when a tool/MCP call is blocked by DENY policy."""


class ProviderVersionMismatchError(RuntimeError):
    """Raised when provider versions don't match the bundle."""

    def __init__(self, message: str, *, error_code: str = "PROVIDER_VERSION_MISMATCH") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class ReplaySpec:
    """Full replay specification for WS4.

    ``fidelity`` is required (no default) to prevent accidental use of
    a wrong fidelity level.
    """

    fidelity: ReplayFidelity  # REQUIRED, no default
    from_sequence: int | None = None
    to_sequence: int | None = None
    stage_filter: list[str] | None = None
    overrides: dict[str, Any] = field(default_factory=dict)
    timing: Literal["fast", "wall"] = "fast"
    force: bool = False
    tool_policy: ToolReplayPolicy = ToolReplayPolicy.DENY


# Re-export NONDETERMINISTIC_FIELDS from stages.base and extend
REPLAY_IGNORE_FIELDS: frozenset[str] = NONDETERMINISTIC_FIELDS | frozenset(
    {
        "timing.wall_deadline_ns",
        "artifact_written_at",
        "artifact_hashed_at",
    }
)
