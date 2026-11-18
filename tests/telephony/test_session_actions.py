"""Tests for telephony-backed session action executors."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
from typing import Any
from unittest.mock import patch

import pytest

from easycat._dtmf_tones import (
    DTMFToneConfig,
    DTMFTonePlayback,
    count_dtmf_keys,
    dtmf_sequence_duration_ms,
)
from easycat.session.actions import (
    MAX_DTMF_INTER_DIGIT_DELAY_MS,
    EndCallAction,
    SendDTMFAction,
    SendSMSAction,
    TransferCallAction,
    TransferPlan,
)
from easycat.telephony.session_actions import (
    TWILIO_DTMF_MODES,
    TwilioDTMFMode,
    TwilioSessionActionConfig,
    TwilioSessionActionExecutor,
    _apply_inter_digit_delay,
)
from easycat.telephony.twilio_app import _TWILIO_DTMF_MODES, twilio_app_settings_from_env
from easycat.telephony.twiml import (
    compute_twilio_webhook_signature,
    validate_twilio_webhook_signature,
)
from easycat.transports.twilio_media import TwilioConnectionTransport


class _FakeCallUpdater:
    def __init__(self) -> None:
        self.updates: list[dict[str, str]] = []

    def update(self, **kwargs: str) -> None:
        self.updates.append(kwargs)


class _FakeCalls:
    def __init__(self) -> None:
        self.last_sid: str | None = None
        self.updater = _FakeCallUpdater()

    def __call__(self, sid: str) -> _FakeCallUpdater:
        self.last_sid = sid
        return self.updater


class _FakeMessages:
    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []

    def create(self, *, to: str, from_: str, body: str):
        self.requests.append({"to": to, "from_": from_, "body": body})
        return type("Message", (), {"sid": "SM123"})()


class _FakeTwilioClient:
    def __init__(self) -> None:
        self.calls = _FakeCalls()
        self.messages = _FakeMessages()


class _FakeTransport:
    call_sid = "CA123"


class _FakeSession:
    transport = _FakeTransport()


class _FakeWebSocket:
    async def send(self, _message: str) -> None:
        return None

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_twilio_session_action_missing_sdk_install_hint() -> None:
    executor = TwilioSessionActionExecutor(
        TwilioSessionActionConfig(account_sid="AC123", auth_token="token")
    )

    with patch.dict("sys.modules", {"twilio": None, "twilio.rest": None}):  # noqa: SIM117 nested scopes clarify setup and cleanup
        with pytest.raises(RuntimeError) as exc_info:
            await executor.execute(_FakeSession(), EndCallAction())

    message = str(exc_info.value)
    assert "uv add 'easycat[telephony]'" in message
    assert "From the EasyCat repo, use: uv sync --extra telephony --group dev" in message


def test_twilio_webhook_signature_validation_groups_duplicate_form_values() -> None:
    url = "https://voice.example.com/twiml?tenant=acme"
    params = [("Digits", "2"), ("CallSid", "CA123"), ("Digits", "1")]
    signed = url + "CallSidCA123Digits1Digits2"
    expected = base64.b64encode(
        hmac.new(b"token", signed.encode("utf-8"), hashlib.sha1).digest()
    ).decode("ascii")

    assert (
        compute_twilio_webhook_signature(
            auth_token="token",
            url=url,
            params=params,
        )
        == expected
    )
    assert validate_twilio_webhook_signature(
        auth_token="token",
        url=url,
        params=params,
        signature=expected,
    )
    assert not validate_twilio_webhook_signature(
        auth_token="token",
        url=url,
        params=params,
        signature="bad",
    )


@pytest.mark.asyncio
async def test_twilio_transfer_action_updates_call_with_twiml() -> None:
    client = _FakeTwilioClient()
    executor = TwilioSessionActionExecutor(TwilioSessionActionConfig(client=client))
    action = TransferCallAction(
        target="+15551234567",
        plan=TransferPlan(
            client_message="Connecting you now.",
            post_dial_digits="ww1234",
        ),
    )

    result = await executor.execute(_FakeSession(), action)

    assert result.stop_session is True
    assert client.calls.last_sid == "CA123"
    twiml = client.calls.updater.updates[0]["twiml"]
    assert "Connecting you now." in twiml
    assert "+15551234567" in twiml
    assert "ww1234" in twiml


@pytest.mark.asyncio
async def test_twilio_dtmf_action_inserts_delays() -> None:
    client = _FakeTwilioClient()
    executor = TwilioSessionActionExecutor(TwilioSessionActionConfig(client=client))
    action = SendDTMFAction(digits="123", inter_digit_delay_ms=1000)

    result = await executor.execute(_FakeSession(), action)

    assert result.metadata["digits"] == "1W2W3"
    assert 'digits="1W2W3"' in client.calls.updater.updates[0]["twiml"]


@pytest.mark.asyncio
async def test_twilio_dtmf_action_requests_stop_because_update_ends_the_stream() -> None:
    """A Twilio call update replaces ``<Connect><Stream>``: the session must stop.

    Twilio cannot send outbound DTMF over a bidirectional Media Stream, so the
    executor plays the tones by replacing the call's TwiML. That ends the media
    stream (and the call, once the tones finish), so reporting the session as
    still active would leave it running on a dead transport.
    """
    client = _FakeTwilioClient()
    executor = TwilioSessionActionExecutor(TwilioSessionActionConfig(client=client))

    result = await executor.execute(_FakeSession(), SendDTMFAction(digits="1"))

    assert result.stop_session is True
    assert result.metadata["media_stream_replaced"] is True
    twiml = client.calls.updater.updates[0]["twiml"]
    assert "<Stream" not in twiml


def test_apply_inter_digit_delay_clamps_large_delay() -> None:
    digits = _apply_inter_digit_delay("12", MAX_DTMF_INTER_DIGIT_DELAY_MS * 1000)

    assert digits == "1" + ("W" * 10) + "2"


@pytest.mark.asyncio
async def test_twilio_connection_transport_session_action_uses_start_call_sid() -> None:
    client = _FakeTwilioClient()
    transport = TwilioConnectionTransport(_FakeWebSocket())
    await transport._handle_start(
        {
            "streamSid": "MZ123",
            "start": {
                "streamSid": "MZ123",
                "callSid": "CA_FROM_START",
                "customParameters": {"From": "+15551234567"},
            },
        }
    )
    session = type("Session", (), {"transport": transport})()
    executor = TwilioSessionActionExecutor(TwilioSessionActionConfig(client=client))

    result = await executor.execute(session, SendDTMFAction(digits="9"))

    assert transport.stream_sid == "MZ123"
    assert transport.call_sid == "CA_FROM_START"
    assert client.calls.last_sid == "CA_FROM_START"
    assert result.metadata["call_sid"] == "CA_FROM_START"
    assert 'digits="9"' in client.calls.updater.updates[0]["twiml"]


@pytest.mark.asyncio
async def test_twilio_sms_action_uses_configured_from_number() -> None:
    client = _FakeTwilioClient()
    executor = TwilioSessionActionExecutor(
        TwilioSessionActionConfig(client=client, sms_from_number="+15550001111")
    )

    result = await executor.execute(
        _FakeSession(),
        SendSMSAction(to="+15551112222", body="Here is your link."),
    )

    assert result.metadata["message_sid"] == "SM123"
    assert client.messages.requests == [
        {"to": "+15551112222", "from_": "+15550001111", "body": "Here is your link."}
    ]


# ── DTMF delivery modes (rest default, in_band / auto opt-in) ─────────────


class _FakeToneSession:
    """Session double exposing ``play_dtmf_tones`` like :class:`Session`."""

    transport = _FakeTransport()

    def __init__(self, outcome: str | None = "played", *, duration_ms: int = 10) -> None:
        self._outcome = outcome
        self._duration_ms = duration_ms
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.playbacks: list[DTMFTonePlayback] = []

    async def play_dtmf_tones(self, digits: str, **kwargs: Any) -> DTMFTonePlayback:
        self.calls.append((digits, kwargs))
        done: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        if self._outcome is not None:
            done.set_result(self._outcome)
        playback = DTMFTonePlayback(
            key_count=count_dtmf_keys(digits),
            duration_ms=self._duration_ms,
            mark_name="easycat_dtmf_1",
            _done=done,
        )
        self.playbacks.append(playback)
        return playback


class _ExplodingClient:
    """A Twilio client that fails the test if the in-band path touches it."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"in-band DTMF must not use the Twilio REST client ({name})")


def test_twilio_dtmf_mode_defaults_to_rest() -> None:
    config = TwilioSessionActionConfig()

    assert config.dtmf_mode == "rest"
    assert config.dtmf_tones is None
    assert config.dtmf_wait_for_playback is True
    assert config.dtmf_playback_timeout_margin_s == 10.0
    assert config.dtmf_fence_timeout_s == 10.0
    assert TWILIO_DTMF_MODES == ("rest", "in_band", "auto")


@pytest.mark.asyncio
async def test_default_rest_mode_ignores_a_tone_capable_session() -> None:
    """The REST default is unchanged even when the session could play tones."""
    client = _FakeTwilioClient()
    executor = TwilioSessionActionExecutor(TwilioSessionActionConfig(client=client))
    session = _FakeToneSession()

    result = await executor.execute(session, SendDTMFAction(digits="12", inter_digit_delay_ms=0))

    assert session.calls == []
    assert result.stop_session is True
    assert result.metadata == {
        "call_sid": "CA123",
        "digits": "12",
        "delivery": "rest",
        "media_stream_replaced": True,
    }
    assert 'digits="12"' in client.calls.updater.updates[0]["twiml"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["in_band", "auto"])
async def test_in_band_dtmf_keeps_the_session_and_skips_rest(mode: TwilioDTMFMode) -> None:
    tones = DTMFToneConfig(tone_ms=80)
    executor = TwilioSessionActionExecutor(
        TwilioSessionActionConfig(dtmf_mode=mode, dtmf_tones=tones, dtmf_fence_timeout_s=3.0)
    )
    session = _FakeToneSession()

    result = await executor.execute(
        session, SendDTMFAction(digits="12#", inter_digit_delay_ms=250)
    )

    # No REST client is built (no credentials, no SDK needed) and no update.
    assert executor._client is None
    assert session.calls == [
        (
            "12#",
            {"inter_digit_delay_ms": 250, "tone_config": tones, "fence_timeout_s": 3.0},
        )
    ]
    assert result.stop_session is False
    assert result.metadata == {
        "call_sid": "CA123",
        "digits": "12#",
        "delivery": "in_band",
        "duration_ms": 10,
        "playback_mark": "easycat_dtmf_1",
        "playback": "played",
        "inter_digit_delay_ms": 250,
    }


@pytest.mark.asyncio
async def test_in_band_dtmf_never_touches_a_configured_rest_client() -> None:
    executor = TwilioSessionActionExecutor(
        TwilioSessionActionConfig(client=_ExplodingClient(), dtmf_mode="in_band")
    )

    result = await executor.execute(_FakeToneSession(), SendDTMFAction(digits="9"))

    assert result.metadata["delivery"] == "in_band"


@pytest.mark.asyncio
async def test_in_band_dtmf_without_waiting_reports_sent() -> None:
    executor = TwilioSessionActionExecutor(
        TwilioSessionActionConfig(dtmf_mode="in_band", dtmf_wait_for_playback=False)
    )
    session = _FakeToneSession(outcome=None)

    result = await executor.execute(session, SendDTMFAction(digits="5"))

    assert result.metadata["playback"] == "sent"
    assert result.stop_session is False
    assert not session.playbacks[0].done()


@pytest.mark.asyncio
async def test_in_band_dtmf_unconfirmed_playback_counts_as_completed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    executor = TwilioSessionActionExecutor(
        TwilioSessionActionConfig(dtmf_mode="in_band", dtmf_playback_timeout_margin_s=0.01)
    )
    session = _FakeToneSession(outcome=None, duration_ms=1)

    with caplog.at_level(logging.WARNING, logger="easycat.telephony.session_actions"):
        result = await executor.execute(session, SendDTMFAction(digits="5"))

    assert result.metadata["playback"] == "timeout"
    assert result.stop_session is False
    assert any("did not confirm" in message for message in caplog.messages)
    assert not any("5" in message for message in caplog.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["estimated", "played"])
async def test_in_band_dtmf_reports_the_playback_outcome(outcome: str) -> None:
    executor = TwilioSessionActionExecutor(TwilioSessionActionConfig(dtmf_mode="auto"))

    result = await executor.execute(_FakeToneSession(outcome), SendDTMFAction(digits="0"))

    assert result.metadata["playback"] == outcome


@pytest.mark.asyncio
async def test_in_band_dtmf_disconnect_raises() -> None:
    executor = TwilioSessionActionExecutor(TwilioSessionActionConfig(dtmf_mode="in_band"))

    with pytest.raises(RuntimeError, match="media stream ended"):
        await executor.execute(_FakeToneSession("disconnected"), SendDTMFAction(digits="1"))


@pytest.mark.asyncio
async def test_in_band_mode_fails_closed_without_play_dtmf_tones() -> None:
    client = _FakeTwilioClient()
    executor = TwilioSessionActionExecutor(
        TwilioSessionActionConfig(client=client, dtmf_mode="in_band")
    )

    with pytest.raises(RuntimeError, match="dtmf_mode='rest'"):
        await executor.execute(_FakeSession(), SendDTMFAction(digits="1"))

    assert client.calls.updater.updates == []


@pytest.mark.asyncio
async def test_auto_mode_falls_back_to_rest_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _FakeTwilioClient()
    executor = TwilioSessionActionExecutor(
        TwilioSessionActionConfig(client=client, dtmf_mode="auto")
    )

    with caplog.at_level(logging.WARNING, logger="easycat.telephony.session_actions"):
        result = await executor.execute(
            _FakeSession(), SendDTMFAction(digits="123", inter_digit_delay_ms=1000)
        )

    assert "falling back to REST" in caplog.text
    assert "ends the media stream" in caplog.text
    # The PR #1347 REST contract holds on the fallback path.
    assert result.stop_session is True
    assert result.metadata["media_stream_replaced"] is True
    assert result.metadata["delivery"] == "rest"
    assert result.metadata["digits"] == "1W2W3"
    assert 'digits="1W2W3"' in client.calls.updater.updates[0]["twiml"]


@pytest.mark.asyncio
async def test_auto_mode_sends_an_over_long_sequence_over_rest(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sequence the tone renderer would reject never reaches in-band playback."""
    client = _FakeTwilioClient()
    executor = TwilioSessionActionExecutor(
        TwilioSessionActionConfig(client=client, dtmf_mode="auto")
    )
    session = _FakeToneSession()
    digits = "1" * 56
    duration_ms = dtmf_sequence_duration_ms(digits, inter_digit_delay_ms=1000)
    assert duration_ms > DTMFToneConfig().max_total_ms

    with caplog.at_level(logging.WARNING, logger="easycat.telephony.session_actions"):
        result = await executor.execute(
            session, SendDTMFAction(digits=digits, inter_digit_delay_ms=1000)
        )

    assert session.calls == []
    assert result.stop_session is True
    assert result.metadata["delivery"] == "rest"
    assert len(client.calls.updater.updates) == 1
    assert "falling back to REST" in caplog.text
    assert digits not in caplog.text


@pytest.mark.asyncio
async def test_auto_mode_uses_the_configured_tone_limit() -> None:
    tones = DTMFToneConfig(max_total_ms=600_000)
    client = _FakeTwilioClient()
    executor = TwilioSessionActionExecutor(
        TwilioSessionActionConfig(client=client, dtmf_mode="auto", dtmf_tones=tones)
    )
    session = _FakeToneSession()

    result = await executor.execute(
        session, SendDTMFAction(digits="1" * 56, inter_digit_delay_ms=1000)
    )

    assert result.metadata["delivery"] == "in_band"
    assert len(session.calls) == 1
    assert client.calls.updater.updates == []


@pytest.mark.asyncio
async def test_auto_mode_does_not_retry_over_rest_after_a_play_error() -> None:
    """Playback errors may follow sent frames, so auto never re-sends over REST."""

    class _RejectingSession(_FakeToneSession):
        async def play_dtmf_tones(self, digits: str, **kwargs: Any) -> DTMFTonePlayback:
            raise RuntimeError("transport rejected DTMF audio")

    client = _FakeTwilioClient()
    executor = TwilioSessionActionExecutor(
        TwilioSessionActionConfig(client=client, dtmf_mode="auto")
    )

    with pytest.raises(RuntimeError, match="transport rejected"):
        await executor.execute(_RejectingSession(), SendDTMFAction(digits="1"))

    assert client.calls.updater.updates == []


@pytest.mark.asyncio
async def test_in_band_mode_still_fails_closed_on_an_over_long_sequence() -> None:
    class _RenderingSession(_FakeToneSession):
        async def play_dtmf_tones(self, digits: str, **kwargs: Any) -> DTMFTonePlayback:
            raise ValueError("DTMF sequence would last too long")

    client = _FakeTwilioClient()
    executor = TwilioSessionActionExecutor(
        TwilioSessionActionConfig(client=client, dtmf_mode="in_band")
    )

    with pytest.raises(ValueError, match="too long"):
        await executor.execute(
            _RenderingSession(), SendDTMFAction(digits="1" * 56, inter_digit_delay_ms=1000)
        )

    assert client.calls.updater.updates == []


@pytest.mark.asyncio
async def test_in_band_dtmf_propagates_play_errors() -> None:
    class _RejectingSession(_FakeToneSession):
        async def play_dtmf_tones(self, digits: str, **kwargs: Any) -> DTMFTonePlayback:
            raise RuntimeError("transport rejected DTMF audio")

    executor = TwilioSessionActionExecutor(TwilioSessionActionConfig(dtmf_mode="in_band"))

    with pytest.raises(RuntimeError, match="transport rejected"):
        await executor.execute(_RejectingSession(), SendDTMFAction(digits="1"))


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"dtmf_mode": "tones"}, ValueError),
        ({"dtmf_mode": None}, ValueError),
        ({"dtmf_tones": {"tone_ms": 80}}, TypeError),
        ({"dtmf_wait_for_playback": 1}, TypeError),
        ({"dtmf_playback_timeout_margin_s": 0}, ValueError),
        ({"dtmf_playback_timeout_margin_s": float("nan")}, ValueError),
        ({"dtmf_playback_timeout_margin_s": True}, ValueError),
        ({"dtmf_fence_timeout_s": -1.0}, ValueError),
        ({"dtmf_fence_timeout_s": float("inf")}, ValueError),
    ],
)
def test_twilio_dtmf_config_validation(overrides: dict[str, Any], error: type[Exception]) -> None:
    with pytest.raises(error):
        TwilioSessionActionConfig(**overrides)


@pytest.mark.asyncio
async def test_executor_revalidates_a_mutated_dtmf_config() -> None:
    config = TwilioSessionActionConfig(client=_FakeTwilioClient())
    executor = TwilioSessionActionExecutor(config)
    config.dtmf_mode = "bogus"  # type: ignore[assignment]

    with pytest.raises(ValueError, match="dtmf_mode"):
        await executor.execute(_FakeSession(), SendDTMFAction(digits="1"))
    with pytest.raises(ValueError, match="dtmf_mode"):
        TwilioSessionActionExecutor(config)


def test_twilio_app_settings_dtmf_mode_env() -> None:
    env = {
        "TWILIO_STREAM_URL": "wss://voice.example.test/media",
        "TWILIO_ACCOUNT_SID": "AC123",
        "TWILIO_AUTH_TOKEN": "token",
    }

    default = twilio_app_settings_from_env(environ=env)
    in_band = twilio_app_settings_from_env(environ={**env, "TWILIO_DTMF_MODE": " In_Band "})

    assert default.dtmf_mode == "rest"
    actions = default.twilio_session_actions()
    assert actions is not None and actions.dtmf_mode == "rest"
    assert in_band.dtmf_mode == "in_band"
    actions = in_band.twilio_session_actions()
    assert actions is not None and actions.dtmf_mode == "in_band"
    with pytest.raises(RuntimeError, match="TWILIO_DTMF_MODE"):
        twilio_app_settings_from_env(environ={**env, "TWILIO_DTMF_MODE": "rfc2833"})


@pytest.mark.parametrize(("mode", "configured"), [("in_band", True), ("auto", False)])
def test_twilio_app_settings_in_band_mode_needs_no_rest_credentials(
    mode: str, configured: bool
) -> None:
    env = {"TWILIO_STREAM_URL": "wss://voice.example.test/media", "TWILIO_DTMF_MODE": mode}

    settings = twilio_app_settings_from_env(environ=env)
    actions = settings.twilio_session_actions()

    assert settings.twilio_actions_enabled is False
    if not configured:
        assert actions is None
        return
    assert actions is not None
    assert actions.dtmf_mode == "in_band"
    assert actions.account_sid == "" and actions.auth_token == ""


def test_twilio_app_settings_dtmf_modes_match_the_executor() -> None:
    assert set(_TWILIO_DTMF_MODES) == set(TWILIO_DTMF_MODES)
