"""Regression tests for privacy-preserving log labels."""

from easycat._privacy import redacted_phone_number_label


def test_redacted_phone_number_label_is_constant() -> None:
    assert redacted_phone_number_label() == "redacted phone number"
