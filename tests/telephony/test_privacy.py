"""Regression tests for privacy-preserving telephony log labels."""

from easycat.telephony._privacy import phone_number_log_label


def test_phone_number_log_label_keeps_only_nanp_area_code() -> None:
    assert phone_number_log_label("+1 (415) 555-1234") == "area code 415"
    assert phone_number_log_label("2125551234") == "area code 212"


def test_phone_number_log_label_hides_non_nanp_and_malformed_values() -> None:
    assert phone_number_log_label("+442012345678") == "non-NANP number"
    assert phone_number_log_label("not a number") == "non-NANP number"
    assert phone_number_log_label(None) == "non-NANP number"
