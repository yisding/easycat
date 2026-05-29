"""Tests for TwiML generation and Gather webhook parsing (Tasks 6.2, 6.4)."""

from __future__ import annotations

from easycat.events import DTMF, EventBus
from easycat.telephony import (
    compute_twilio_webhook_signature,
    validate_twilio_webhook_signature,
)
from easycat.telephony.twiml import (
    parse_gather_webhook,
    reconstruct_public_url,
    sanitize_dtmf_digits,
    twiml_dial_number,
    twiml_dial_send_digits,
    twiml_gather,
    twiml_hangup,
    twiml_play_digits,
)


def test_twilio_webhook_helpers_are_public_telephony_exports() -> None:
    signature = compute_twilio_webhook_signature(
        auth_token="token",
        url="https://voice.example.com/twiml",
        params={"CallSid": "CA123", "From": "+15551234567"},
    )
    assert validate_twilio_webhook_signature(
        auth_token="token",
        url="https://voice.example.com/twiml",
        params={"CallSid": "CA123", "From": "+15551234567"},
        signature=signature,
    )


# ── Task 6.2: TwiML Gather webhook parsing ──────────────────────


class TestParseGatherWebhook:
    """Tests for parse_gather_webhook."""

    def test_single_digit(self) -> None:
        events = parse_gather_webhook({"Digits": "5"})
        assert len(events) == 1
        assert events[0].digit == "5"

    def test_multiple_digits(self) -> None:
        events = parse_gather_webhook({"Digits": "12345#"})
        assert len(events) == 6
        assert [e.digit for e in events] == ["1", "2", "3", "4", "5", "#"]

    def test_star_and_hash(self) -> None:
        events = parse_gather_webhook({"Digits": "*#"})
        assert len(events) == 2
        assert events[0].digit == "*"
        assert events[1].digit == "#"

    def test_empty_digits(self) -> None:
        events = parse_gather_webhook({"Digits": ""})
        assert len(events) == 0

    def test_missing_digits_field(self) -> None:
        events = parse_gather_webhook({"CallSid": "CA123"})
        assert len(events) == 0

    def test_non_string_digits(self) -> None:
        events = parse_gather_webhook({"Digits": 12345})
        assert len(events) == 0

    def test_lowercase_letters_normalized(self) -> None:
        events = parse_gather_webhook({"Digits": "abcd"})
        assert len(events) == 4
        assert [e.digit for e in events] == ["A", "B", "C", "D"]

    def test_invalid_characters_skipped(self) -> None:
        events = parse_gather_webhook({"Digits": "1x2y3"})
        assert len(events) == 3
        assert [e.digit for e in events] == ["1", "2", "3"]

    def test_typical_twilio_payload(self) -> None:
        """Simulate a realistic Twilio Gather callback."""
        payload = {
            "AccountSid": "AC123",
            "ApiVersion": "2010-04-01",
            "CallSid": "CA456",
            "CallStatus": "in-progress",
            "Called": "+15551234567",
            "Caller": "+15559876543",
            "Digits": "1928#",
            "From": "+15559876543",
            "To": "+15551234567",
        }
        events = parse_gather_webhook(payload)
        assert len(events) == 5
        assert [e.digit for e in events] == ["1", "9", "2", "8", "#"]


class TestEmitGatherDigits:
    """Tests for emit_gather_digits convenience function."""

    async def test_emits_all_digits(self) -> None:
        from easycat.telephony.twiml import emit_gather_digits

        bus = EventBus()
        received: list[DTMF] = []
        bus.subscribe(DTMF, lambda e: received.append(e))

        events = await emit_gather_digits({"Digits": "123"}, bus)
        assert len(events) == 3
        assert len(received) == 3
        assert [r.digit for r in received] == ["1", "2", "3"]


# ── Task 6.4: DTMF output TwiML ─────────────────────────────────


class TestTwimlPlayDigits:
    """Tests for twiml_play_digits."""

    def test_basic_digits(self) -> None:
        result = twiml_play_digits("1234")
        assert '<Play digits="1234"/>' in result
        assert "<Response>" in result
        assert "</Response>" in result

    def test_star_and_hash(self) -> None:
        result = twiml_play_digits("*#")
        assert '<Play digits="*#"/>' in result

    def test_xml_declaration(self) -> None:
        result = twiml_play_digits("5")
        assert result.startswith('<?xml version="1.0" encoding="UTF-8"?>')

    def test_strips_non_dtmf_special_chars(self) -> None:
        # Non-DTMF characters (including XML-significant ones) are stripped
        # before rendering, so the payload cannot carry markup at all.
        result = twiml_play_digits("1&2")
        assert '<Play digits="12"/>' in result
        assert "&" not in result


class TestTwimlDialSendDigits:
    """Tests for twiml_dial_send_digits."""

    def test_basic_dial(self) -> None:
        result = twiml_dial_send_digits("+15551234567", "1234#")
        assert '<Number sendDigits="1234#">+15551234567</Number>' in result
        assert "<Dial>" in result
        assert "</Dial>" in result

    def test_with_wait_pauses(self) -> None:
        result = twiml_dial_send_digits("+15551234567", "wwww1928#")
        assert 'sendDigits="wwww1928#"' in result

    def test_with_caller_id(self) -> None:
        result = twiml_dial_send_digits("+15551234567", "123", caller_id="+15559876543")
        assert 'callerId="+15559876543"' in result

    def test_without_caller_id(self) -> None:
        result = twiml_dial_send_digits("+15551234567", "123")
        assert "callerId" not in result


class TestTwimlGather:
    """Tests for twiml_gather."""

    def test_basic_gather(self) -> None:
        result = twiml_gather(action_url="/handle-digits")
        assert "<Gather" in result
        assert 'action="/handle-digits"' in result
        assert 'timeout="5"' in result
        assert 'finishOnKey="#"' in result
        assert 'input="dtmf"' in result

    def test_with_num_digits(self) -> None:
        result = twiml_gather(action_url="/pin", num_digits=4)
        assert 'numDigits="4"' in result

    def test_with_say_prompt(self) -> None:
        result = twiml_gather(
            action_url="/digits",
            say_text="Enter your account number",
        )
        assert "<Say>Enter your account number</Say>" in result

    def test_custom_timeout(self) -> None:
        result = twiml_gather(action_url="/x", timeout=10)
        assert 'timeout="10"' in result

    def test_custom_finish_key(self) -> None:
        result = twiml_gather(action_url="/x", finish_on_key="*")
        assert 'finishOnKey="*"' in result


class TestTwimlHangup:
    """Tests for twiml_hangup."""

    def test_hangup(self) -> None:
        result = twiml_hangup()
        assert "<Hangup/>" in result
        assert "<Response>" in result
        assert result.startswith('<?xml version="1.0"')


# ── Finding 1: DTMF charset validation shared across both output paths ──


class TestSanitizeDtmfDigits:
    """Tests for the centralized DTMF whitelist."""

    def test_keeps_valid_digits_and_pauses(self) -> None:
        assert sanitize_dtmf_digits("1234*#ABCDwW") == "1234*#ABCDwW"

    def test_strips_non_dtmf_text(self) -> None:
        assert sanitize_dtmf_digits("12<Play>3") == "123"

    def test_strips_letters_outside_whitelist(self) -> None:
        assert sanitize_dtmf_digits("1x2y3") == "123"

    def test_empty(self) -> None:
        assert sanitize_dtmf_digits("") == ""


class TestDtmfOutputSanitization:
    """The two TwiML DTMF entry points share one whitelist (Finding 1)."""

    def test_play_digits_strips_injection(self) -> None:
        result = twiml_play_digits('1"/><Say>x</Say><Play digits="2')
        # Only DTMF digits survive; no injected element text leaks through.
        assert "<Say>" not in result
        assert '<Play digits="12"/>' in result

    def test_dial_send_digits_strips_injection(self) -> None:
        result = twiml_dial_send_digits("+15551234567", "12abc34")
        assert 'sendDigits="1234"' in result

    def test_dial_number_strips_injection(self) -> None:
        result = twiml_dial_number(
            "+15551234567",
            send_digits="9<Hangup/>9",
        )
        assert "<Hangup/>" not in result
        assert 'sendDigits="99"' in result


# ── Finding 2: proxied public-URL reconstruction for signature validation ──


class TestReconstructPublicUrl:
    """Tests for reconstruct_public_url."""

    def test_default_uses_host_and_https(self) -> None:
        url = reconstruct_public_url({"Host": "voice.example.com"}, "/twiml?x=1")
        assert url == "https://voice.example.com/twiml?x=1"

    def test_ignores_forwarded_headers_without_trust(self) -> None:
        headers = {
            "Host": "internal.lb",
            "X-Forwarded-Proto": "http",
            "X-Forwarded-Host": "voice.example.com",
        }
        url = reconstruct_public_url(headers, "/twiml")
        assert url == "https://internal.lb/twiml"

    def test_honors_forwarded_headers_when_trusted(self) -> None:
        headers = {
            "Host": "internal.lb",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "voice.example.com",
        }
        url = reconstruct_public_url(headers, "/twiml", trust_proxy=True)
        assert url == "https://voice.example.com/twiml"

    def test_forwarded_header_takes_first_entry(self) -> None:
        headers = {
            "Host": "internal.lb",
            "X-Forwarded-Host": "voice.example.com, internal.lb",
            "X-Forwarded-Proto": "https, http",
        }
        url = reconstruct_public_url(headers, "/twiml", trust_proxy=True)
        assert url == "https://voice.example.com/twiml"

    def test_case_insensitive_headers(self) -> None:
        url = reconstruct_public_url({"host": "voice.example.com"}, "/twiml")
        assert url == "https://voice.example.com/twiml"

    def test_prefixes_missing_leading_slash(self) -> None:
        url = reconstruct_public_url({"Host": "voice.example.com"}, "twiml")
        assert url == "https://voice.example.com/twiml"

    def test_no_host_returns_path(self) -> None:
        assert reconstruct_public_url({}, "/twiml") == "/twiml"

    def test_validates_signature_behind_proxy(self) -> None:
        public_url = "https://voice.example.com/twiml"
        params = {"CallSid": "CA123", "From": "+15551234567"}
        signature = compute_twilio_webhook_signature(
            auth_token="token", url=public_url, params=params
        )
        # The app behind a TLS-terminating LB sees http + internal host.
        headers = {
            "Host": "internal.lb",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "voice.example.com",
        }
        reconstructed = reconstruct_public_url(headers, "/twiml", trust_proxy=True)
        assert validate_twilio_webhook_signature(
            auth_token="token",
            url=reconstructed,
            params=params,
            signature=signature,
        )


class TestValidateWithCandidateUrls:
    """validate_twilio_webhook_signature accepts multiple candidate URLs."""

    def test_matches_one_of_several_candidates(self) -> None:
        public_url = "https://voice.example.com/twiml"
        params = {"CallSid": "CA123"}
        signature = compute_twilio_webhook_signature(
            auth_token="token", url=public_url, params=params
        )
        assert validate_twilio_webhook_signature(
            auth_token="token",
            url=["http://voice.example.com/twiml", public_url],
            params=params,
            signature=signature,
        )

    def test_rejects_when_no_candidate_matches(self) -> None:
        params = {"CallSid": "CA123"}
        signature = compute_twilio_webhook_signature(
            auth_token="token", url="https://voice.example.com/twiml", params=params
        )
        assert not validate_twilio_webhook_signature(
            auth_token="token",
            url=["http://voice.example.com/twiml", "https://other.example.com/twiml"],
            params=params,
            signature=signature,
        )

    def test_empty_candidate_list_rejected(self) -> None:
        assert not validate_twilio_webhook_signature(
            auth_token="token",
            url=[],
            params={},
            signature="x",
        )
