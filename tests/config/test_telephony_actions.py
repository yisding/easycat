from __future__ import annotations

import pytest

from easycat import (
    EasyConfig,
    create_session,
)
from easycat.config import TelephonyConfig
from easycat.events import DTMFAggregated
from easycat.telephony.dtmf import emit_twilio_dtmf
from easycat.telephony.session_actions import (
    TelnyxSessionActionConfig,
    TelnyxSessionActionExecutor,
    TwilioSessionActionConfig,
    TwilioSessionActionExecutor,
)
from tests.config._helpers import (
    _DummyAgent,
)


@pytest.mark.asyncio
async def test_telephony_helpers_are_managed_by_session_lifecycle():
    config = EasyConfig(
        openai_api_key="test-key",
        telephony=TelephonyConfig(enable_dtmf_aggregator=True),
        agent=_DummyAgent(),
    )
    config.smart_turn.enabled = False

    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise
    bus = session.event_bus
    aggregated: list[DTMFAggregated] = []
    bus.subscribe(DTMFAggregated, lambda e: aggregated.append(e))

    # Telephony helpers must be started (normally done by session.start())
    for helper in session.telephony.helpers:
        helper.start()

    await emit_twilio_dtmf({"event": "dtmf", "dtmf": {"digit": "1"}}, bus)
    await emit_twilio_dtmf({"event": "dtmf", "dtmf": {"digit": "#"}}, bus)
    assert aggregated

    for helper in session.telephony.helpers:
        helper.stop()

    aggregated.clear()
    await emit_twilio_dtmf({"event": "dtmf", "dtmf": {"digit": "1"}}, bus)
    await emit_twilio_dtmf({"event": "dtmf", "dtmf": {"digit": "#"}}, bus)
    assert not aggregated


def test_create_session_adds_twilio_action_executor_when_configured():
    config = EasyConfig(
        openai_api_key="test-key",
        agent=_DummyAgent(),
        telephony=TelephonyConfig(
            twilio_actions=TwilioSessionActionConfig(
                account_sid="AC123",
                auth_token="secret",
            )
        ),
    )

    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise

    assert any(
        isinstance(executor, TwilioSessionActionExecutor) for executor in session._action_executors
    )


def test_create_session_adds_telnyx_action_executor_when_configured():
    config = EasyConfig(
        openai_api_key="test-key",
        agent=_DummyAgent(),
        telephony=TelephonyConfig(
            telnyx_actions=TelnyxSessionActionConfig(
                api_key="key",
                sms_from_number="+15550001111",
            )
        ),
    )

    try:
        session = create_session(config)
    except RuntimeError as exc:
        if "No VAD backend available" in str(exc):
            pytest.skip("No VAD backend available")
        raise

    assert any(
        isinstance(executor, TelnyxSessionActionExecutor) for executor in session._action_executors
    )


def test_telnyx_actions_requires_config_instance():
    with pytest.raises(ValueError, match="telnyx_actions must be a TelnyxSessionActionConfig"):
        TelephonyConfig(telnyx_actions=object())  # type: ignore[arg-type]


def test_both_action_executors_wire_together():
    from easycat.config._telephony_wiring import create_action_executors

    executors = create_action_executors(
        TelephonyConfig(
            twilio_actions=TwilioSessionActionConfig(account_sid="AC123", auth_token="t"),
            telnyx_actions=TelnyxSessionActionConfig(api_key="key"),
        )
    )

    assert len(executors) == 2
    assert isinstance(executors[0], TwilioSessionActionExecutor)
    assert isinstance(executors[1], TelnyxSessionActionExecutor)
