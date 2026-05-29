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
import re
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
    except KeyError:
        logger.warning("Unknown timezone %r for %s, blocking call", tz_name, phone)
        return False


@dataclass(frozen=True)
class CallBlocked:
    """Emitted when a call is blocked by compliance checks."""

    number: str
    reason: str


class DNCList:
    """Internal Do Not Call list.

    Maintains a set of phone numbers that should not be called.
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


@dataclass
class AIDisclosureConfig:
    """Configuration for AI disclosure at the start of calls."""

    enabled: bool = True
    text: str = "This call uses AI assistance."


# Common opt-out phrases that trigger DNC addition.
#
# Under-detection (missing a real opt-out) is the more serious compliance risk
# than over-detection, so the list errs toward broad coverage of common
# phrasings.  Matching is anchored on word boundaries (see
# :func:`match_opt_out_phrase`) so embedded substrings do not produce spurious
# hits.
OPT_OUT_PHRASES: list[str] = [
    "take me off your list",
    "take my number off",
    "stop calling",
    "do not call",
    "don't call",
    "remove my number",
    "remove me from your list",
    "remove me",
    "unsubscribe",
    "opt out",
]

# Phrases that, when they immediately precede an opt-out phrase, negate it
# (e.g. "I do not want to opt out").  Kept intentionally small and literal.
_NEGATION_PREFIXES: tuple[str, ...] = (
    "do not want to",
    "don't want to",
    "do not need to",
    "don't need to",
    "no need to",
    "not going to",
    "won't",
    "will not",
    "please don't",
    "please do not",
)


def detect_opt_out(text: str) -> bool:
    """Detect if the callee is requesting to be removed from the calling list."""
    return match_opt_out_phrase(text) is not None


def match_opt_out_phrase(text: str, phrases: list[str] | None = None) -> str | None:
    """Return the first matching opt-out phrase, or ``None`` when none match.

    Matching is anchored on word boundaries so that an opt-out phrase only
    matches as a whole word/phrase rather than as an arbitrary substring
    (``"opt out"`` no longer matches inside ``"options"``, and ``"stop
    calling"`` no longer fires on unrelated text).  A small set of negation
    prefixes (e.g. ``"please don't stop calling me"``) is also guarded so an
    explicit *non*-opt-out does not silently add the number to a DNC list.

    Adding a number to a DNC list is hard to reverse from the callee's side, so
    this guards against the most common false positives; however it is *not* a
    substitute for application-level confirmation of ambiguous requests.

    Useful when a caller wants to know *which* phrase the callee used
    (logging, journal records, compliance audit trails).  ``phrases``
    defaults to :data:`OPT_OUT_PHRASES` but can be overridden with a
    localised list.
    """
    lower = text.lower()
    for phrase in phrases or OPT_OUT_PHRASES:
        # Word-boundary anchored search; the phrase may contain spaces/internal
        # punctuation so we escape it and rely on \b at the edges.
        pattern = rf"\b{re.escape(phrase)}\b"
        match = re.search(pattern, lower)
        if match is None:
            continue
        if _is_negated(lower, match.start()):
            continue
        return phrase
    return None


def _is_negated(lower: str, phrase_start: int) -> bool:
    """Return ``True`` if a negation prefix immediately precedes *phrase_start*.

    Only the text in the short window directly before the matched phrase is
    considered, so that distant negations elsewhere in the utterance do not
    suppress a genuine opt-out.
    """
    preceding = lower[:phrase_start]
    # Trim trailing connective words/whitespace between the negation and the
    # phrase (e.g. "do not want to *opt out*").
    trimmed = re.sub(r"[\s,]+$", "", preceding)
    return any(trimmed.endswith(prefix) for prefix in _NEGATION_PREFIXES)
