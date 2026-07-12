from __future__ import annotations

from collections.abc import Mapping
from typing import cast

import pytest

from easycat.events import IVRActionType
from easycat.telephony._ivr_decision import IVRAgentDecision, parse_ivr_agent_decision


@pytest.mark.parametrize(
    ("raw", "expected_type", "expected_payload"),
    [
        ({"action": "dtmf", "digits": "12W#"}, IVRActionType.DTMF, "12W#"),
        ({"action": "speak", "text": "billing"}, IVRActionType.SPEAK, "billing"),
        ({"action": "hangup"}, IVRActionType.HANGUP, ""),
        ({"action": "wait"}, IVRActionType.WAIT, ""),
    ],
)
def test_parse_ivr_agent_decision(
    raw: Mapping[str, object],
    expected_type: IVRActionType,
    expected_payload: str,
) -> None:
    decision = parse_ivr_agent_decision(raw)

    assert decision.type is expected_type
    assert decision.payload == expected_payload


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        {},
        {"action": "unknown"},
        {"action": "dtmf"},
        {"action": "dtmf", "digits": ""},
        {"action": "dtmf", "digits": 1},
        {"action": "dtmf", "digits": "1;hangup"},
        {"action": "speak"},
        {"action": "speak", "text": 1},
    ],
)
def test_malformed_agent_decisions_degrade_to_wait(raw: object) -> None:
    assert parse_ivr_agent_decision(raw) == IVRAgentDecision(IVRActionType.WAIT)


def test_navigation_decision_projects_history_and_event() -> None:
    decision = IVRAgentDecision(IVRActionType.DTMF, "1W2")

    assert decision.advances_menu is True
    assert decision.history_entry() == {"action": "dtmf", "digits": "1W2"}
    assert decision.to_event(menu_depth=3).digits == "1W2"
    assert decision.to_event(menu_depth=3).menu_depth == 3


@pytest.mark.parametrize(
    ("action_type", "payload"),
    [
        (IVRActionType.DTMF, ""),
        (IVRActionType.DTMF, "1 2"),
        (IVRActionType.SPEAK, ""),
        (IVRActionType.WAIT, "payload"),
        (IVRActionType.HUMAN_DETECTED, ""),
    ],
)
def test_decision_rejects_invalid_invariants(action_type: IVRActionType, payload: str) -> None:
    with pytest.raises(ValueError):
        IVRAgentDecision(action_type, payload)


@pytest.mark.parametrize(
    ("action_type", "payload", "message"),
    [
        (cast(IVRActionType, "dtmf"), "1", "type must be"),
        (IVRActionType.SPEAK, cast(str, 1), "payload must be"),
    ],
)
def test_decision_rejects_runtime_type_mismatches(
    action_type: IVRActionType, payload: str, message: str
) -> None:
    with pytest.raises(TypeError, match=message):
        IVRAgentDecision(action_type, payload)
