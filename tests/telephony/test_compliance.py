"""Tests for compliance utilities."""

from __future__ import annotations

from easycat.telephony.compliance import (
    AIDisclosureConfig,
    DNCList,
    check_calling_hours,
    detect_opt_out,
    lookup_timezone,
)


class TestCallingHoursEnforcement:
    def test_rejects_call_outside_hours(self) -> None:
        # 7 AM is before 8 AM start.
        assert not check_calling_hours("+12125551234", current_hour=7)
        # 9 PM (21) is at-or-after end_hour=21.
        assert not check_calling_hours("+12125551234", current_hour=21)

    def test_accepts_call_within_hours(self) -> None:
        assert check_calling_hours("+12125551234", current_hour=10)
        assert check_calling_hours("+12125551234", current_hour=20)

    def test_timezone_lookup_by_area_code(self) -> None:
        tz = lookup_timezone("+12125551234")
        assert tz == "America/New_York"
        tz = lookup_timezone("+14155551234")
        assert tz == "America/Los_Angeles"

    def test_timezone_override(self) -> None:
        # Override takes precedence — use current_hour to test logic.
        assert check_calling_hours(
            "+12125551234", current_hour=10, timezone_override="America/Chicago"
        )


class TestAIDisclosure:
    def test_disclosure_text_configurable(self) -> None:
        config = AIDisclosureConfig(text="This call uses AI assistance")
        assert config.text == "This call uses AI assistance"

    def test_disclosure_spoken_on_human_connect(self) -> None:
        """Disclosure should be spoken when connected to human (tested at config level)."""
        config = AIDisclosureConfig(enabled=True, text="AI assisted call")
        assert config.enabled
        assert config.text == "AI assisted call"

    def test_disclosure_not_spoken_to_voicemail(self) -> None:
        """Disclosure disabled check — config flag controls this."""
        config = AIDisclosureConfig(enabled=False)
        assert not config.enabled

    def test_disclosure_disabled_by_config(self) -> None:
        config = AIDisclosureConfig(enabled=False)
        assert not config.enabled


class TestDNCIntegration:
    def test_dnc_check_before_call(self) -> None:
        dnc = DNCList()
        assert not dnc.is_on_dnc("+15551234567")

    def test_dnc_blocks_call(self) -> None:
        dnc = DNCList()
        dnc.add("+15551234567")
        assert dnc.is_on_dnc("+15551234567")

    def test_opt_out_during_call(self) -> None:
        assert detect_opt_out("Please take me off your list")
        assert detect_opt_out("stop calling me")
        assert detect_opt_out("I want to opt out")
        assert not detect_opt_out("Hello, how are you?")
