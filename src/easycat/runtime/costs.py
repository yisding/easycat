"""Runtime cost-budget helpers shared by debugger and session code."""

from __future__ import annotations

import math
from typing import Any

COST_WARNING_FRACTION = 0.8
_MAX_SESSION_COST_STRING_LENGTH = 64


def finite_number(value: Any) -> float | None:
    """Return *value* as a finite float, rejecting booleans and non-numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def max_session_cost_usd_from_snapshot(config_snapshot: dict[str, Any] | None) -> float | None:
    """Extract a positive ``max_session_cost_usd`` from a safe config snapshot."""
    if not isinstance(config_snapshot, dict):
        return None
    raw = config_snapshot.get("max_session_cost_usd")
    if raw is None:
        return None
    if isinstance(raw, str):
        if len(raw) > _MAX_SESSION_COST_STRING_LENGTH:
            return None
        raw = raw.strip()
        if not raw:
            return None
        try:
            raw = float(raw)
        except ValueError:
            return None
    limit = finite_number(raw)
    if limit is None or limit <= 0:
        return None
    return limit


def cost_budget_status(total_usd: float, limit_usd: float | None) -> dict[str, Any]:
    """Return the canonical per-session cost-budget status payload."""
    if limit_usd is None:
        return {
            "configured": False,
            "max_session_cost_usd": None,
            "warning_threshold_usd": None,
            "usage_fraction": None,
            "remaining_usd": None,
            "overage_usd": None,
            "status": "unconfigured",
            "warning": False,
            "exceeded": False,
        }

    usage_fraction = total_usd / limit_usd
    exceeded = total_usd >= limit_usd
    warning = usage_fraction >= COST_WARNING_FRACTION
    status = "exceeded" if exceeded else "warning" if warning else "ok"
    return {
        "configured": True,
        "max_session_cost_usd": limit_usd,
        "warning_threshold_usd": limit_usd * COST_WARNING_FRACTION,
        "usage_fraction": usage_fraction,
        "remaining_usd": max(0.0, limit_usd - total_usd),
        "overage_usd": max(0.0, total_usd - limit_usd),
        "status": status,
        "warning": warning,
        "exceeded": exceeded,
    }
