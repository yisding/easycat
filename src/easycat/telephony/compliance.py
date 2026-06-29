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

try:  # Optional; provided by the ``telephony`` extra (phonenumberslite).
    import phonenumbers as _phonenumbers
except ModuleNotFoundError:  # pragma: no cover - exercised via the fallback path
    _phonenumbers = None

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


def _normalize_dnc_number(phone: str, *, region: str | None = None) -> str:
    """Normalize a phone number to a canonical DNC-matching key.

    When the optional ``phonenumbers`` library (Google libphonenumber, pulled in
    by the ``telephony`` extra via ``phonenumberslite``) is installed, the
    number is parsed and rendered as **E.164**, so the same physical number
    matches regardless of formatting or country.  ``region`` (an ISO 3166-1
    alpha-2 code such as ``"US"``) is used to interpret numbers supplied without
    a ``+`` country code; E.164 inputs need no region.

    Falls back to a digit-only heuristic (see :func:`_normalize_digits_fallback`)
    when ``phonenumbers`` is unavailable or the number cannot be parsed, so DNC
    matching still works — just without global canonicalization.  Inputs that
    cannot produce even one digit are rejected; otherwise unrelated non-number
    strings would all collapse to the same empty DNC key.

    Keep a single deployment consistent: a list written while ``phonenumbers``
    is installed (E.164 keys) and later read without it (digit keys) will not
    match.  Likewise, mixing E.164 and bare-national inputs requires setting a
    ``region`` so both canonicalize the same way.
    """
    if _phonenumbers is None:
        normalized = _normalize_digits_fallback(phone)
    else:
        try:
            parsed = _phonenumbers.parse(phone, region)
        except _phonenumbers.NumberParseException:
            normalized = _normalize_digits_fallback(phone)
        else:
            normalized = _phonenumbers.format_number(
                parsed, _phonenumbers.PhoneNumberFormat.E164
            )
    if not _strip_to_digits(normalized):
        raise ValueError("DNC phone number must contain at least one digit")
    return normalized


def _normalize_dnc_lookup_number(phone: str, *, region: str | None = None) -> str | None:
    """Normalize a DNC lookup/removal key, returning ``None`` for no-number values."""
    try:
        return _normalize_dnc_number(phone, region=region)
    except ValueError:
        return None


def _normalize_digits_fallback(phone: str) -> str:
    """Digit-only DNC key used when ``phonenumbers`` is unavailable.

    Strips to digits, then collapses a plausibly-NANP number to its 10-digit
    national form so ``+1XXXXXXXXXX``, ``1XXXXXXXXXX`` and ``XXXXXXXXXX`` all
    key to the same entry (mirroring :func:`_extract_area_code`).  Without this
    a number added from an E.164 caller-id (``+1…``) would not match a bare
    10-digit ``place_call`` target, silently letting a do-not-call through.
    Non-NANP numbers keep their stripped-digit form (best effort).
    """
    digits = _strip_to_digits(phone)
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


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

    ``default_region`` (an ISO 3166-1 alpha-2 code such as ``"US"``) is used to
    canonicalize numbers supplied without a ``+`` country code; see
    :func:`_normalize_dnc_number`.
    """

    def __init__(self, *, default_region: str | None = None) -> None:
        self._numbers: set[str] = set()
        self._default_region = default_region

    def _normalize(self, phone: str) -> str:
        return _normalize_dnc_number(phone, region=self._default_region)

    def _normalize_lookup(self, phone: str) -> str | None:
        return _normalize_dnc_lookup_number(phone, region=self._default_region)

    def add(self, phone: str) -> None:
        """Add a number to the DNC list."""
        self._numbers.add(self._normalize(phone))

    def remove(self, phone: str) -> None:
        """Remove a number from the DNC list."""
        number = self._normalize_lookup(phone)
        if number is not None:
            self._numbers.discard(number)

    def is_on_dnc(self, phone: str) -> bool:
        """Check if a number is on the DNC list."""
        number = self._normalize_lookup(phone)
        return number is not None and number in self._numbers

    def __len__(self) -> int:
        return len(self._numbers)


class SQLiteDNCList:
    """Durable, SQLite-backed Do-Not-Call list.

    A drop-in replacement for :class:`DNCList` (same :class:`DNCStore`
    surface) that persists numbers to a SQLite file, so do-not-call state
    survives process restarts and is shared by every session/worker that
    points at the same ``path``.  Numbers are normalized (matching
    :class:`DNCList`, see :func:`_normalize_dnc_number`) before storage.

    Pass the same ``path`` to every session to share one list; pass
    ``":memory:"`` for an ephemeral instance (e.g. in tests).  The single
    WAL-mode connection is guarded by a lock so it is safe to share across
    threads.  The methods are synchronous and do blocking disk I/O, so async
    callers (the action executor, the outbound pre-dial check) must offload
    them with :func:`asyncio.to_thread` rather than call them on the event
    loop.

    Phone numbers tied to do-not-call status are PII, so an on-disk database
    (and its WAL/SHM sidecars) is created owner-only (``0o600``), matching the
    SQLite journal's private-file handling.  A missing parent directory is
    created.

    ``default_region`` (an ISO 3166-1 alpha-2 code such as ``"US"``) is used to
    canonicalize numbers supplied without a ``+`` country code; see
    :func:`_normalize_dnc_number`.
    """

    def __init__(self, path: str | Path, *, default_region: str | None = None) -> None:
        self._path = str(path)
        self._default_region = default_region
        self._lock = threading.Lock()
        self._persistent = self._path not in (":memory:", "")
        db_path = Path(self._path)
        if self._persistent:
            # Create a missing parent (so a fresh first run does not crash) and
            # the DB file itself owner-only before SQLite opens it.
            db_path.parent.mkdir(parents=True, exist_ok=True)
            from easycat.runtime._private_files import touch_private_file

            touch_private_file(db_path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("CREATE TABLE IF NOT EXISTS dnc_numbers (number TEXT PRIMARY KEY)")
            self._conn.commit()
            self._harden()
        except Exception:
            # Don't leak the connection if PRAGMA/CREATE fails (read-only DB, etc.).
            self._conn.close()
            raise

    def _normalize(self, phone: str) -> str:
        return _normalize_dnc_number(phone, region=self._default_region)

    def _normalize_lookup(self, phone: str) -> str | None:
        return _normalize_dnc_lookup_number(phone, region=self._default_region)

    def _harden(self) -> None:
        """Force owner-only perms on the DB and any WAL/SHM sidecars."""
        if not self._persistent:
            return
        from easycat.runtime._private_files import harden_sqlite_files

        harden_sqlite_files(Path(self._path))

    def add(self, phone: str) -> None:
        """Add a number to the DNC list (idempotent)."""
        number = self._normalize(phone)
        with self._lock:
            self._conn.execute("INSERT OR IGNORE INTO dnc_numbers (number) VALUES (?)", (number,))
            self._conn.commit()
            self._harden()

    def remove(self, phone: str) -> None:
        """Remove a number from the DNC list (no-op if absent)."""
        number = self._normalize_lookup(phone)
        if number is None:
            return
        with self._lock:
            self._conn.execute("DELETE FROM dnc_numbers WHERE number = ?", (number,))
            self._conn.commit()
            self._harden()

    def is_on_dnc(self, phone: str) -> bool:
        """Check if a number is on the DNC list."""
        number = self._normalize_lookup(phone)
        if number is None:
            return False
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
        """Checkpoint the WAL into the main DB and close the connection."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                logger.debug("DNC store WAL checkpoint on close failed", exc_info=True)
            self._conn.close()


@dataclass
class AIDisclosureConfig:
    """Configuration for AI disclosure at the start of calls."""

    enabled: bool = True
    text: str = "This call uses AI assistance."
