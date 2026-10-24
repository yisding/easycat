"""Startup validation for the telephony servers' lifecycle timeouts."""

from __future__ import annotations

import math

from easycat._numeric import is_finite_number


def validate_server_timeouts(
    config_name: str,
    *,
    start_timeout_s: float,
    drain_timeout_s: float,
    force_shutdown_timeout_s: float,
) -> None:
    """Reject invalid timeouts before any listener is bound.

    The shutdown budgets use the rule ``runtime.drain`` applies (finite and
    ``>= 0``; ``0`` drains by force at once). Checking them only at shutdown
    would let the server take calls with a config that ``drain`` then rejects
    before stopping any live session (gh 1259).
    """
    if not math.isfinite(start_timeout_s) or start_timeout_s <= 0:
        raise ValueError(f"{config_name}.start_timeout_s must be positive")
    for name, value in (
        ("drain_timeout_s", drain_timeout_s),
        ("force_shutdown_timeout_s", force_shutdown_timeout_s),
    ):
        if not is_finite_number(value) or value < 0:
            raise ValueError(f"{config_name}.{name} must be a finite number >= 0")
