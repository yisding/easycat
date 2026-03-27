"""Compliance utilities for outbound calling (TCPA, FCC, DNC)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# US area code to timezone mapping (simplified — covers major codes).
# In production, use a proper database or API.
_AREA_CODE_TZ: dict[str, str] = {
    "201": "America/New_York",
    "212": "America/New_York",
    "213": "America/Los_Angeles",
    "214": "America/Chicago",
    "312": "America/Chicago",
    "415": "America/Los_Angeles",
    "503": "America/Los_Angeles",
    "602": "America/Phoenix",
    "617": "America/New_York",
    "713": "America/Chicago",
    "808": "Pacific/Honolulu",
    "907": "America/Anchorage",
}


def _extract_area_code(phone: str) -> str | None:
    """Extract 3-digit area code from a US phone number."""
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("1") and len(digits) >= 4:
        return digits[1:4]
    if len(digits) >= 3:
        return digits[:3]
    return None


def lookup_timezone(phone: str) -> str | None:
    """Look up approximate timezone for a US phone number by area code."""
    area_code = _extract_area_code(phone)
    if area_code:
        return _AREA_CODE_TZ.get(area_code)
    return None


def check_calling_hours(
    phone: str,
    *,
    current_hour: int | None = None,
    timezone_override: str | None = None,
    start_hour: int = 8,
    end_hour: int = 21,
) -> bool:
    """Check if it's within allowed calling hours for the recipient.

    Args:
        phone: Recipient phone number (E.164 format).
        current_hour: Current hour in recipient's timezone (0-23). If None,
            derived from timezone lookup.
        timezone_override: Explicit timezone for the recipient.
        start_hour: Earliest allowed calling hour (default 8 = 8 AM).
        end_hour: Latest allowed calling hour (default 21 = 9 PM).

    Returns:
        True if calling is allowed, False otherwise.
    """
    if current_hour is not None:
        return start_hour <= current_hour < end_hour

    tz_name = timezone_override or lookup_timezone(phone)
    if tz_name is None:
        # Conservative: if we can't determine timezone, allow the call
        # but log a warning.
        logger.warning("Cannot determine timezone for %s, allowing call", phone)
        return True

    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        return start_hour <= now.hour < end_hour
    except Exception:
        logger.warning("Timezone lookup failed for %s (%s)", phone, tz_name)
        return True


@dataclass(frozen=True)
class CallBlocked:
    """Emitted when a call is blocked by compliance checks."""

    number: str
    reason: str


class DNCSList:
    """Internal Do Not Call list.

    Maintains a set of phone numbers that should not be called.
    """

    def __init__(self) -> None:
        self._numbers: set[str] = set()

    def add(self, phone: str) -> None:
        """Add a number to the DNC list."""
        normalized = "".join(c for c in phone if c.isdigit())
        self._numbers.add(normalized)

    def remove(self, phone: str) -> None:
        """Remove a number from the DNC list."""
        normalized = "".join(c for c in phone if c.isdigit())
        self._numbers.discard(normalized)

    def is_on_dnc(self, phone: str) -> bool:
        """Check if a number is on the DNC list."""
        normalized = "".join(c for c in phone if c.isdigit())
        return normalized in self._numbers

    def __len__(self) -> int:
        return len(self._numbers)


@dataclass
class AIDisclosureConfig:
    """Configuration for AI disclosure at the start of calls."""

    enabled: bool = True
    text: str = "This call uses AI assistance."


# Common opt-out phrases that trigger DNC addition.
OPT_OUT_PHRASES: list[str] = [
    "take me off your list",
    "stop calling",
    "do not call",
    "remove my number",
    "unsubscribe",
    "opt out",
]


def detect_opt_out(text: str) -> bool:
    """Detect if the callee is requesting to be removed from the calling list."""
    lower = text.lower()
    return any(phrase in lower for phrase in OPT_OUT_PHRASES)
