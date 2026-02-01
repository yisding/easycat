"""Tests for TwiML generation and Gather webhook parsing (Tasks 6.2, 6.4)."""

from __future__ import annotations

from easycat.events import DTMF, EventBus
from easycat.telephony.twiml import (
    parse_gather_webhook,
    twiml_dial_send_digits,
    twiml_gather,
    twiml_hangup,
    twiml_play_digits,
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

    def test_escapes_special_chars(self) -> None:
        result = twiml_play_digits("1&2")
        assert "&amp;" in result


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
