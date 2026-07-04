"""Shared truthiness parsing for boolean opt-in environment flags.

This is the single source of truth for interpreting an env-var string as a
boolean flag. All flag readers (``EASYCAT_DEV``, ``EASYCAT_EMERGENCY_EXPORT``,
``EASYCAT_DEBUGGER_AUTOLAUNCH``, ``EASYCAT_CAPTURE_AEC_REFERENCE``, …) route
their opt-in decision through :func:`is_truthy` so ``"0"``/``"false"``/``"no"``/
``"off"`` are consistently falsy and any other non-empty value is truthy.
"""

from __future__ import annotations


def is_truthy(value: str | None) -> bool:
    """Interpret an env-var string as a boolean opt-in flag."""
    if value is None:
        return False
    return value.strip().lower() not in ("", "0", "false", "no", "off")
