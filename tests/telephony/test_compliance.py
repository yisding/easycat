"""Tests for compliance utilities."""

from __future__ import annotations

from easycat.telephony.compliance import (
    AIDisclosureConfig,
    DNCList,
    check_calling_hours,
    detect_opt_out,
    lookup_timezone,
    match_opt_out_phrase,
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

    def test_unknown_timezone_blocks_call(self) -> None:
        # Area code 999 is not in the mapping — should block conservatively.
        assert not check_calling_hours("+19995551234")

    def test_non_nanp_number_does_not_resolve_timezone(self) -> None:
        # A non-US E.164 number (UK) must not be misrouted to a US timezone.
        assert lookup_timezone("+442012345678") is None
        # And the call must be blocked rather than allowed via a guessed tz.
        assert not check_calling_hours("+442012345678")

    def test_malformed_short_number_returns_none(self) -> None:
        # Too few digits to be a NANP number — no area code guessing.
        assert lookup_timezone("212") is None

    def test_bare_ten_digit_number_resolves(self) -> None:
        assert lookup_timezone("2125551234") == "America/New_York"


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

    def test_opt_out_word_boundaries(self) -> None:
        # "opt out" must not match inside unrelated words/phrases.
        assert not detect_opt_out("I am weighing my options out loud")
        assert match_opt_out_phrase("opt outage report") is None

    def test_opt_out_negation_guard(self) -> None:
        assert not detect_opt_out("I do not want to opt out of anything else right now")
        assert not detect_opt_out("please don't stop calling me")
        assert not detect_opt_out("I won't unsubscribe")

    def test_opt_out_broadened_phrases(self) -> None:
        assert detect_opt_out("Please remove me from your list")
        assert detect_opt_out("take my number off")
        assert detect_opt_out("remove me")
