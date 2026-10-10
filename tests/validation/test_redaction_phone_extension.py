from __future__ import annotations

import pytest

from easycat.validation.redaction import REDACTED_PHONE, redact_text


@pytest.mark.xfail(
    strict=True,
    reason="_PHONE_RE's trailing (?!\\w) lets a phone number with an attached extension leak",
)
@pytest.mark.parametrize(
    "text",
    [
        "call 415-555-2671x22",
        "call (415) 555-2671ext5",
    ],
)
def test_pii_redaction_covers_phone_number_with_attached_extension(text: str) -> None:
    redacted = redact_text(text, policy="pii")

    assert "555-2671" not in redacted
    assert REDACTED_PHONE in redacted


def test_pii_redaction_covers_phone_number_with_spaced_extension() -> None:
    # Control: the same number is redacted when the extension is space-separated.
    assert redact_text("call 415-555-2671 x22", policy="pii") == f"call {REDACTED_PHONE} x22"
