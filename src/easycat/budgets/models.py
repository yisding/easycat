"""Budget value objects shared across runtime, validation, eval, and debugger.

``LatencyBudget`` already lives in :mod:`easycat.validation.latency` and is
re-exported here for ergonomics (the legacy import path keeps working).
``CostBudget`` is net-new: a per-session USD ceiling with a warn threshold and
a configurable action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Re-export the EXISTING latency budget so callers can write
# ``from easycat.budgets import LatencyBudget`` while the legacy
# ``from easycat.validation.latency import LatencyBudget`` keeps working.
from easycat.validation.latency import LatencyBudget

__all__ = ["CostBudget", "LatencyBudget"]


@dataclass(frozen=True)
class CostBudget:
    """A per-session USD cost ceiling.

    ``max_session_usd`` is the hard ceiling. ``warn_at`` is the fraction of the
    ceiling at which the budget is considered "warning" (default 80%).
    ``action`` selects what the runtime should do once the ceiling is exceeded:
    ``"stop"`` tears the session down, ``"warn"`` only records the breach.

    The session-config field is spelled ``max_session_cost_usd``; that name is
    preserved as a constructor/serialization alias here so config and budget
    code share one value object.
    """

    max_session_usd: float
    warn_at: float = 0.8
    action: Literal["warn", "stop"] = "stop"

    def __post_init__(self) -> None:
        if self.max_session_usd <= 0:
            raise ValueError(
                f"CostBudget max_session_usd must be positive; got {self.max_session_usd!r}"
            )
        if not 0.0 < self.warn_at <= 1.0:
            raise ValueError(f"CostBudget warn_at must be in (0, 1]; got {self.warn_at!r}")
        if self.action not in ("warn", "stop"):
            raise ValueError(f"CostBudget action must be 'warn' or 'stop'; got {self.action!r}")

    @property
    def max_session_cost_usd(self) -> float:
        """Alias for :attr:`max_session_usd` matching the config field name."""
        return self.max_session_usd

    @property
    def warn_at_usd(self) -> float:
        """The absolute USD value at which the budget enters its warning band."""
        return self.max_session_usd * self.warn_at

    def to_dict(self) -> dict[str, float | str]:
        return {
            "max_session_usd": self.max_session_usd,
            "warn_at": self.warn_at,
            "action": self.action,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> CostBudget:
        """Build a ``CostBudget`` accepting either ceiling spelling.

        Both ``max_session_usd`` and the config-style ``max_session_cost_usd``
        alias are accepted; the canonical key wins when both are present.
        """
        if "max_session_usd" in payload:
            ceiling = payload["max_session_usd"]
        elif "max_session_cost_usd" in payload:
            ceiling = payload["max_session_cost_usd"]
        else:
            raise KeyError(
                "CostBudget payload must include 'max_session_usd' or 'max_session_cost_usd'"
            )
        action = payload.get("action", "stop")
        return cls(
            max_session_usd=float(ceiling),  # type: ignore[arg-type]
            warn_at=float(payload.get("warn_at", 0.8)),  # type: ignore[arg-type]
            action=action,  # type: ignore[arg-type]
        )
