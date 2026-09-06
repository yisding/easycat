"""Tests for TwiML generation and Gather webhook parsing (Tasks 6.2, 6.4)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest

from easycat.events import DTMF, CallAnswered, CallEnded, EventBus
from easycat.telephony import (
    TwilioCallSessionIndex,
    TwilioWebhookSignatureError,
    bearer_token_matches,
    compute_twilio_webhook_signature,
    reconstruct_public_url,
    twilio_app_settings_from_env,
    twilio_form_items_from_request,
    twilio_public_url_from_request,
    twilio_stream_parameters_from_form,
    validate_twilio_webhook_signature,
)
from easycat.telephony import (
    twiml_redirect as exported_twiml_redirect,
)
from easycat.telephony import (
    twiml_reject as exported_twiml_reject,
)
from easycat.telephony.twiml import (
    parse_gather_webhook,
    sanitize_dtmf_digits,
    twiml_dial_number,
    twiml_dial_send_digits,
    twiml_gather,
    twiml_hangup,
    twiml_play_digits,
    twiml_redirect,
    twiml_reject,
)


class _FakeUrl:
    def __init__(self, url: str, *, path: str, query: str = "") -> None:
        self._url = url
        self.path = path
        self.query = query

    def __str__(self) -> str:
        return self._url


class _FakeRequest:
    def __init__(self, *, url: _FakeUrl, headers: dict[str, str], body: str) -> None:
        self.url = url
        self.headers = headers
        self._body = body.encode("utf-8")

    async def body(self) -> bytes:
        return self._body


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
    assert not validate_twilio_webhook_signature(
        auth_token="token",
        url="https://voice.example.com/twiml",
        params={},
        signature="not-ascii-é",
    )
    assert twilio_stream_parameters_from_form({"From": "+15551234567"}) == {
        "Direction": "inbound",
        "From": "+15551234567",
    }
    assert reconstruct_public_url({"Host": "voice.example.com"}, "/twiml") == (
        "https://voice.example.com/twiml"
    )
    assert issubclass(TwilioWebhookSignatureError, ValueError)
    assert exported_twiml_redirect("/overflow").startswith("<?xml")
    assert exported_twiml_reject().startswith("<?xml")


def test_twilio_app_settings_from_env_reads_standard_vars() -> None:
    settings = twilio_app_settings_from_env(
        environ={
            "TWILIO_STREAM_URL": "wss://voice.example.com/stream",
            "TWILIO_ACCOUNT_SID": "AC123",
            "TWILIO_AUTH_TOKEN": "token",
            "TWILIO_VOICE_FROM": "+15551234567",
            "TWILIO_TWIML_URL": "https://voice.example.com/twiml",
            "TWILIO_STATUS_CALLBACK_URL": "https://voice.example.com/status",
            "TWILIO_CALL_API_TOKEN": "call-token",
            "TWILIO_SMS_FROM": "+15557654321",
            "TWILIO_STREAM_TOKEN_SECRET": "stream-secret",
            "TWILIO_DRAIN_TIMEOUT_S": "45",
            "TWILIO_FORCE_SHUTDOWN_TIMEOUT_S": "7.5",
            "TWILIO_PUBLIC_TWIML_URL": "https://voice.example.com/prefix/twiml",
            "TWILIO_MAX_SESSIONS": "12",
        }
    )

    assert settings.stream_url == "wss://voice.example.com/stream"
    assert settings.account_sid == "AC123"
    assert settings.stream_token_secret_or_auth_token == "stream-secret"
    assert settings.outbound_calling_enabled is True
    assert settings.twilio_actions_enabled is True
    assert settings.call_api_token == "call-token"
    assert settings.drain_timeout_s == 45.0
    assert settings.force_shutdown_timeout_s == 7.5
    assert settings.public_twiml_url == "https://voice.example.com/prefix/twiml"
    assert settings.max_sessions == 12
    actions = settings.twilio_session_actions()
    assert actions is not None
    assert actions.account_sid == "AC123"
    assert actions.auth_token == "token"
    assert actions.sms_from_number == "+15557654321"


def test_twilio_app_settings_stream_url_override_and_missing_error() -> None:
    settings = twilio_app_settings_from_env(
        stream_url="  wss://override.example.com/stream  ",
        environ={"TWILIO_STREAM_URL": "wss://ignored.example.com/stream"},
    )

    assert settings.stream_url == "wss://override.example.com/stream"

    with pytest.raises(RuntimeError, match="TWILIO_STREAM_URL is required"):
        twilio_app_settings_from_env(environ={})

    with pytest.raises(RuntimeError, match="TWILIO_STREAM_URL is required"):
        twilio_app_settings_from_env(environ={"TWILIO_STREAM_URL": "   "})


def test_twilio_app_settings_can_require_auth_and_validate_session_limit() -> None:
    with pytest.raises(RuntimeError, match="TWILIO_AUTH_TOKEN is required"):
        twilio_app_settings_from_env(
            stream_url="wss://voice.example.com/stream",
            require_auth_token=True,
            environ={},
        )

    for value in ("0", "-1", "many"):
        with pytest.raises(RuntimeError, match="TWILIO_MAX_SESSIONS"):
            twilio_app_settings_from_env(
                stream_url="wss://voice.example.com/stream",
                environ={"TWILIO_MAX_SESSIONS": value},
            )

    for name in ("TWILIO_DRAIN_TIMEOUT_S", "TWILIO_FORCE_SHUTDOWN_TIMEOUT_S"):
        for value in ("-0.1", "later", "nan", "inf"):
            with pytest.raises(RuntimeError, match=name):
                twilio_app_settings_from_env(
                    stream_url="wss://voice.example.com/stream",
                    environ={name: value},
                )

    for value in ("-0.1", "later", "nan", "inf"):
        with pytest.raises(RuntimeError, match="TWILIO_START_TIMEOUT_S"):
            twilio_app_settings_from_env(
                stream_url="wss://voice.example.com/stream",
                environ={"TWILIO_START_TIMEOUT_S": value},
            )


@pytest.mark.asyncio
async def test_twilio_call_session_index_tracks_and_unsubscribes() -> None:
    bus = EventBus()
    session = SimpleNamespace(event_bus=bus)
    index = TwilioCallSessionIndex()
    cleanup = index.track(session)

    await bus.emit(CallAnswered(call_sid="CA123"))
    assert index.get("CA123") is session
    await bus.emit(CallEnded(call_sid="CA123"))
    assert index.get("CA123") is None

    cleanup()
    await bus.emit(CallAnswered(call_sid="CA-LATE"))
    assert index.get("CA-LATE") is None


def test_bearer_token_matches_is_constant_time_safe_for_non_ascii() -> None:
    assert bearer_token_matches("Bearer secret", "secret")
    assert bearer_token_matches("bearer secret", "secret")
    assert not bearer_token_matches("Bearer wrong", "secret")
    assert not bearer_token_matches("Basic secret", "secret")
    assert not bearer_token_matches("Bearer secrét", "secret")
    assert not bearer_token_matches("Bearer secret", "secrét")


def test_bearer_token_matches_fails_closed_on_an_unconfigured_token() -> None:
    """An empty expected token must reject everything (gh 1105).

    ``TwilioAppSettings.call_api_token`` is ``""`` when
    ``TWILIO_CALL_API_TOKEN`` is unset, and a bare ``Authorization: Bearer``
    header compared equal to it — so an unconfigured deployment authenticated
    any such request. The sibling ``validate_twilio_webhook_signature`` already
    fails closed on an empty auth token.
    """
    assert not bearer_token_matches("Bearer ", "")
    assert not bearer_token_matches("Bearer anything", "")
    assert not bearer_token_matches("", "")
    assert not bearer_token_matches(None, "")


def test_twilio_app_settings_treats_whitespace_env_values_as_missing() -> None:
    settings = twilio_app_settings_from_env(
        environ={
            "TWILIO_STREAM_URL": "wss://voice.example.com/stream",
            "TWILIO_ACCOUNT_SID": "   ",
            "TWILIO_AUTH_TOKEN": "   ",
            "TWILIO_VOICE_FROM": "   ",
            "TWILIO_TWIML_URL": "   ",
            "TWILIO_CALL_API_TOKEN": "   ",
            "TWILIO_SMS_FROM": "   ",
            "TWILIO_STREAM_TOKEN_SECRET": "   ",
        }
    )

    assert settings.account_sid == ""
    assert settings.auth_token == ""
    assert settings.stream_token_secret_or_auth_token is None
    assert settings.call_api_token == ""
    assert settings.sms_from == ""
    assert settings.outbound_calling_enabled is False
    assert settings.twilio_actions_enabled is False


def test_twilio_app_settings_optional_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = twilio_app_settings_from_env(
        environ={
            "TWILIO_STREAM_URL": "wss://voice.example.com/stream",
            "TWILIO_ACCOUNT_SID": "AC123",
            "TWILIO_AUTH_TOKEN": "token",
            "TWILIO_VOICE_FROM": "+15551234567",
            "TWILIO_TWIML_URL": "https://voice.example.com/twiml",
            "TWILIO_STATUS_CALLBACK_URL": "https://voice.example.com/status",
        }
    )
    calls: list[tuple[EventBus, dict[str, str]]] = []

    class _Manager:
        def __init__(self, event_bus: EventBus, **kwargs: str) -> None:
            calls.append((event_bus, kwargs))
            self.started = False

        def start(self) -> None:
            self.started = True

    monkeypatch.setattr("easycat.telephony.outbound.OutboundCallManager", _Manager)

    bus = EventBus()
    manager = settings.start_outbound_manager(bus)

    assert manager is not None
    assert manager.started is True
    assert calls == [
        (
            bus,
            {
                "from_number": "+15551234567",
                "twilio_account_sid": "AC123",
                "twilio_auth_token": "token",
                "twiml_url": "https://voice.example.com/twiml",
                "status_callback_url": "https://voice.example.com/status",
            },
        )
    ]
    disabled = twilio_app_settings_from_env(environ={"TWILIO_STREAM_URL": "wss://x"})
    assert disabled.start_outbound_manager(EventBus()) is None
    assert disabled.twilio_session_actions() is None


def test_twilio_public_url_from_request_uses_framework_url_without_proxy() -> None:
    request = _FakeRequest(
        url=_FakeUrl("http://internal.example/twiml?x=1", path="/twiml", query="x=1"),
        headers={"Host": "internal.example"},
        body="",
    )

    assert twilio_public_url_from_request(request) == "http://internal.example/twiml?x=1"


def test_twilio_public_url_from_request_honors_forwarded_proxy_headers() -> None:
    request = _FakeRequest(
        url=_FakeUrl("http://internal.example/twiml?x=1", path="/twiml", query="x=1"),
        headers={
            "Host": "internal.example",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "voice.example.com",
        },
        body="",
    )

    assert twilio_public_url_from_request(request) == "https://voice.example.com/twiml?x=1"


async def test_twilio_form_items_from_request_preserves_blank_values() -> None:
    request = _FakeRequest(
        url=_FakeUrl("https://voice.example.com/twiml", path="/twiml"),
        headers={},
        body="CallSid=CA123&Empty=&From=%2B15551234567",
    )

    assert await twilio_form_items_from_request(request) == [
        ("CallSid", "CA123"),
        ("Empty", ""),
        ("From", "+15551234567"),
    ]


async def test_twilio_form_items_from_request_validates_forwarded_signature() -> None:
    params = [("CallSid", "CA123"), ("From", "+15551234567")]
    signature = compute_twilio_webhook_signature(
        auth_token="token",
        url="https://voice.example.com/twiml?x=1",
        params=params,
    )
    request = _FakeRequest(
        url=_FakeUrl("http://internal.example/twiml?x=1", path="/twiml", query="x=1"),
        headers={
            "Host": "internal.example",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "voice.example.com",
            "X-Twilio-Signature": signature,
        },
        body="CallSid=CA123&From=%2B15551234567",
    )

    assert await twilio_form_items_from_request(request, auth_token="token") == params


async def test_twilio_form_items_from_request_rejects_bad_signature() -> None:
    request = _FakeRequest(
        url=_FakeUrl("https://voice.example.com/twiml", path="/twiml"),
        headers={"X-Twilio-Signature": "bad"},
        body="CallSid=CA123",
    )

    with pytest.raises(TwilioWebhookSignatureError):
        await twilio_form_items_from_request(request, auth_token="token")


def test_twilio_stream_parameters_from_form_defaults_and_copies_caller_fields() -> None:
    assert twilio_stream_parameters_from_form({}) == {"Direction": "inbound"}
    assert twilio_stream_parameters_from_form(
        [
            ("Direction", "outbound-api"),
            ("From", "+15551234567"),
            ("To", "+15557654321"),
            ("CallerId", "+15550001111"),
            ("ForwardedFrom", "+15559990000"),
            ("CallerName", "Ada Lovelace"),
            ("FromCity", "SEATTLE"),
            ("FromState", "WA"),
            ("FromZip", "98101"),
            ("FromCountry", "US"),
            ("Ignored", "value"),
            ("X-Carrier", "pstn"),
        ],
        extra_fields=("X-Carrier",),
    ) == {
        "Direction": "outbound-api",
        "From": "+15551234567",
        "To": "+15557654321",
        "CallerId": "+15550001111",
        "ForwardedFrom": "+15559990000",
        "CallerName": "Ada Lovelace",
        "FromCity": "SEATTLE",
        "FromState": "WA",
        "FromZip": "98101",
        "FromCountry": "US",
        "X-Carrier": "pstn",
    }


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

    def test_numeric_attrs_escape_quotes(self) -> None:
        result = twiml_gather(
            action_url="/x",
            timeout='5" method="GET',  # type: ignore[arg-type]
            num_digits='4" actionOnEmptyResult="true',  # type: ignore[arg-type]
        )
        gather = ET.fromstring(result).find("Gather")
        assert gather is not None
        assert gather.attrib == {
            "action": "/x",
            "timeout": '5" method="GET',
            "finishOnKey": "#",
            "input": "dtmf",
            "numDigits": '4" actionOnEmptyResult="true',
        }


class TestTwimlHangup:
    """Tests for twiml_hangup."""

    def test_hangup(self) -> None:
        result = twiml_hangup()
        assert "<Hangup/>" in result
        assert "<Response>" in result
        assert result.startswith('<?xml version="1.0"')


class TestTwimlReject:
    """Tests for twiml_reject."""

    def test_default_reject_reason(self) -> None:
        result = twiml_reject()
        reject = ET.fromstring(result).find("Reject")
        assert reject is not None
        assert reject.attrib == {"reason": "rejected"}

    def test_busy_reject_reason(self) -> None:
        result = twiml_reject("busy")
        reject = ET.fromstring(result).find("Reject")
        assert reject is not None
        assert reject.attrib == {"reason": "busy"}

    @pytest.mark.parametrize("reason", ["", "temporary", "Busy", 'busy" x="1'])
    def test_rejects_unsupported_reason(self, reason: str) -> None:
        with pytest.raises(ValueError, match="reason must be 'rejected' or 'busy'"):
            twiml_reject(reason)  # type: ignore[arg-type]


class TestTwimlRedirect:
    """Tests for twiml_redirect."""

    def test_redirect_url(self) -> None:
        result = twiml_redirect("https://voice.example.com/overflow?x=1&y=2")
        redirect = ET.fromstring(result).find("Redirect")
        assert redirect is not None
        assert redirect.text == "https://voice.example.com/overflow?x=1&y=2"
        assert redirect.attrib == {}

    @pytest.mark.parametrize("method", ["GET", "POST"])
    def test_redirect_method(self, method: str) -> None:
        result = twiml_redirect("/overflow", method=method)  # type: ignore[arg-type]
        redirect = ET.fromstring(result).find("Redirect")
        assert redirect is not None
        assert redirect.attrib == {"method": method}

    @pytest.mark.parametrize("url", ["", " ", "\n\t"])
    def test_rejects_blank_url(self, url: str) -> None:
        with pytest.raises(ValueError, match="url must be non-empty"):
            twiml_redirect(url)

    @pytest.mark.parametrize("method", ["", "get", "PUT", 'GET" bad="1'])
    def test_rejects_unsupported_method(self, method: str) -> None:
        with pytest.raises(ValueError, match="method must be 'GET' or 'POST'"):
            twiml_redirect("/overflow", method=method)  # type: ignore[arg-type]


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
