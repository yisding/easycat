"""Privacy-preserving labels for telephony diagnostics."""

from __future__ import annotations


def phone_number_log_label(phone: str | None) -> str:
    """Return a useful log label without exposing a full phone number."""
    digits = "".join(character for character in phone or "" if character.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return f"area code {digits[1:4]}"
    if len(digits) == 10:
        return f"area code {digits[:3]}"
    return "non-NANP number"
