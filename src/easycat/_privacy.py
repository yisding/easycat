"""Privacy-preserving labels for diagnostics."""

from __future__ import annotations


def redacted_phone_number_label() -> str:
    """Return a constant label that cannot retain phone-derived data."""
    return "redacted phone number"
