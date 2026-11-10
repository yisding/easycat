from __future__ import annotations

import pytest

from easycat.validation.redaction import redact_text


@pytest.mark.parametrize(
    "text",
    [
        "started at 2026-11-05 12:30:00Z",
        "started at 2026-11-05 12:30:00.123456",
        "started at 2026-11-05 12:30:00.123456789",
        "2026-11-05T12:30:00.123456789Z",
        "2026-11-05 12:30:00 ERROR provider timed out",
    ],
)
def test_pii_redaction_keeps_space_separated_timestamps(text: str) -> None:
    # Log-style timestamps are not phone numbers; redacting them corrupts
    # validation reports (e.g. ``[REDACTED_PHONE]:30:00Z``).
    assert redact_text(text, policy="pii") == text


def test_pii_redaction_keeps_t_separated_timestamp() -> None:
    # Control: the ISO-8601 ``T`` form is already left intact.
    text = "2026-11-05T12:30:00"
    assert redact_text(text, policy="pii") == text


@pytest.mark.parametrize(
    "text",
    [
        "at 2026-11-05 12:30:00Z call +1 (415) 555-2671",
        "12:30 call 415-555-2671",
        "dial 415.555.2671 now",
    ],
)
def test_pii_redaction_still_redacts_phone_numbers_near_timestamps(text: str) -> None:
    redacted = redact_text(text, policy="pii")

    assert "2671" not in redacted
    assert "[REDACTED_PHONE]" in redacted
