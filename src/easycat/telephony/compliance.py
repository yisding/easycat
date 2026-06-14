"""Compliance utilities for outbound calling (TCPA, FCC, DNC).

Warning: The area-code-to-timezone mapping in this module covers only a small
subset of US area codes, and area-code extraction only accepts plausibly-NANP
numbers (10 digits, or 11 digits with a leading ``1``).  Everything else fails
closed (``lookup_timezone`` returns ``None``, so the call is blocked).  For
production use, replace ``_AREA_CODE_TZ`` with a complete database or
third-party API (e.g. libphonenumber, Twilio Lookup), or always pass
``timezone_override`` / ``current_hour`` to :func:`check_calling_hours`.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

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


def _strip_to_digits(phone: str) -> str:
    """Return only the digit characters from *phone*."""
    return "".join(c for c in phone if c.isdigit())


def _extract_area_code(phone: str) -> str | None:
    """Extract the 3-digit area code from a *plausibly NANP* phone number.

    Only numbers that look like North American Numbering Plan (NANP) numbers
    are accepted:

    * an 11-digit string with a leading ``1`` country code (``+1NXXNXXXXXX``), or
    * a bare 10-digit national number (``NXXNXXXXXX``).

    Anything else (too few/too many digits, or a non-``1`` country code) returns
    ``None`` rather than guessing ``digits[:3]``.  Guessing would let a
    malformed or non-US number be misrouted to a US timezone and incorrectly
    *allowed* through :func:`check_calling_hours`; failing closed is the safer
    compliance posture.  Production callers should supply ``timezone_override``
    or a real lookup (e.g. libphonenumber, Twilio Lookup).
    """
    digits = _strip_to_digits(phone)
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:4]
    if len(digits) == 10:
        return digits[:3]
    return None


def lookup_timezone(phone: str) -> str | None:
    """Look up approximate timezone for a US phone number by area code.

    Returns ``None`` (and logs a warning) when the area code is not in the
    built-in mapping.  Callers should treat ``None`` conservatively.
    """
    area_code = _extract_area_code(phone)
    if area_code:
        tz = _AREA_CODE_TZ.get(area_code)
        if tz is None:
            logger.warning(
                "Area code %s not in timezone mapping for %s — consider using a complete database",
                area_code,
                phone,
            )
        return tz
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
        # Conservative: deny the call when we can't determine timezone.
        # TCPA requires knowledge of the recipient's local time.
        logger.warning("Cannot determine timezone for %s, blocking call", phone)
        return False

    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        return start_hour <= now.hour < end_hour
    except (KeyError, ValueError):
        logger.warning("Invalid or unknown timezone %r for %s, blocking call", tz_name, phone)
        return False


@dataclass(frozen=True)
class CallBlocked:
    """Emitted when a call is blocked by compliance checks."""

    number: str
    reason: str


@runtime_checkable
class DNCStore(Protocol):
    """Structural interface for a Do-Not-Call list.

    The outbound pre-dial check and the ``add_to_dnc`` / ``remove_from_dnc``
    agent actions depend only on these three methods, so any object that
    implements them can be passed as ``EasyConfig(dnc_list=...)``.  Both the
    in-memory :class:`DNCList` and the durable :class:`SQLiteDNCList` satisfy
    it; apps needing a shared/clustered backend (Redis, Postgres, a DNC
    vendor API) can implement their own.
    """

    def add(self, phone: str) -> None: ...

    def remove(self, phone: str) -> None: ...

    def is_on_dnc(self, phone: str) -> bool: ...


class DNCList:
    """In-memory Do Not Call list.

    Maintains a set of phone numbers that should not be called.  This is the
    default :class:`DNCStore` and lives only in process memory — it is **not**
    persisted anywhere, so do-not-call state is lost on restart and is not
    shared across worker processes.  For durable, cross-restart DNC state use
    :class:`SQLiteDNCList` (or another :class:`DNCStore` implementation).
    """

    def __init__(self) -> None:
        self._numbers: set[str] = set()

    @staticmethod
    def _normalize(phone: str) -> str:
        return _strip_to_digits(phone)

    def add(self, phone: str) -> None:
        """Add a number to the DNC list."""
        self._numbers.add(self._normalize(phone))

    def remove(self, phone: str) -> None:
        """Remove a number from the DNC list."""
        self._numbers.discard(self._normalize(phone))

    def is_on_dnc(self, phone: str) -> bool:
        """Check if a number is on the DNC list."""
        return self._normalize(phone) in self._numbers

    def __len__(self) -> int:
        return len(self._numbers)


class SQLiteDNCList:
    """Durable, SQLite-backed Do-Not-Call list.

    A drop-in replacement for :class:`DNCList` (same :class:`DNCStore`
    surface) that persists numbers to a SQLite file, so do-not-call state
    survives process restarts and is shared by every session/worker that
    points at the same ``path``.  Numbers are normalized to digits (matching
    :class:`DNCList`) before storage.

    Pass the same ``path`` to every session to share one list; pass
    ``":memory:"`` for an ephemeral instance (e.g. in tests).  The single
    WAL-mode connection is guarded by a lock so the async action executor and
    the outbound pre-dial check may touch it from different threads.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("CREATE TABLE IF NOT EXISTS dnc_numbers (number TEXT PRIMARY KEY)")
        self._conn.commit()

    @staticmethod
    def _normalize(phone: str) -> str:
        return _strip_to_digits(phone)

    def add(self, phone: str) -> None:
        """Add a number to the DNC list (idempotent)."""
        number = self._normalize(phone)
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO dnc_numbers (number) VALUES (?)", (number,))
            self._conn.commit()

    def remove(self, phone: str) -> None:
        """Remove a number from the DNC list (no-op if absent)."""
        number = self._normalize(phone)
        with self._lock:
            self._conn.execute("DELETE FROM dnc_numbers WHERE number = ?", (number,))
            self._conn.commit()

    def is_on_dnc(self, phone: str) -> bool:
        """Check if a number is on the DNC list."""
        number = self._normalize(phone)
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM dnc_numbers WHERE number = ? LIMIT 1", (number,)
            ).fetchone()
        return row is not None

    def __len__(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM dnc_numbers").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        with self._lock:
            self._conn.close()


@dataclass
class AIDisclosureConfig:
    """Configuration for AI disclosure at the start of calls."""

    enabled: bool = True
    text: str = "This call uses AI assistance."
