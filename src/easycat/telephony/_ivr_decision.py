"""Runtime parsing for untrusted IVR agent callback results."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from easycat.events import IVRAction, IVRActionType
from easycat.telephony.dtmf import is_valid_dtmf_output

_NAVIGATION_TYPES = frozenset({IVRActionType.DTMF, IVRActionType.SPEAK})
_AGENT_TYPES = _NAVIGATION_TYPES | {IVRActionType.WAIT, IVRActionType.HANGUP}


@dataclass(frozen=True, slots=True)
class IVRAgentDecision:
    """Validated action selected by an IVR agent callback."""

    type: IVRActionType
    payload: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.type, IVRActionType):
            raise TypeError("type must be an IVRActionType")
        if not isinstance(self.payload, str):
            raise TypeError("payload must be a string")
        if self.type not in _AGENT_TYPES:
            raise ValueError(f"unsupported IVR agent action: {self.type.value}")
        if self.type is IVRActionType.DTMF and not is_valid_dtmf_output(self.payload):
            raise ValueError("DTMF decisions require valid, non-empty digits")
        if self.type is IVRActionType.SPEAK and not self.payload:
            raise ValueError("speak decisions require non-empty text")
        if self.type not in _NAVIGATION_TYPES and self.payload:
            raise ValueError(f"{self.type.value} decisions cannot carry a payload")

    @property
    def advances_menu(self) -> bool:
        """Return whether the decision consumes one menu level."""
        return self.type in _NAVIGATION_TYPES

    def history_entry(self) -> dict[str, str]:
        """Project a navigation decision into the public history shape."""
        if not self.advances_menu:
            raise ValueError(f"{self.type.value} decisions do not belong in menu history")
        payload_key = "digits" if self.type is IVRActionType.DTMF else "text"
        return {"action": self.type.value, payload_key: self.payload}

    def to_event(self, *, menu_depth: int) -> IVRAction:
        """Project the decision into an event-bus action."""
        return IVRAction(
            type=self.type,
            digits=self.payload if self.type is IVRActionType.DTMF else "",
            text=self.payload if self.type is IVRActionType.SPEAK else "",
            menu_depth=menu_depth,
        )


def parse_ivr_agent_decision(result: object) -> IVRAgentDecision:
    """Parse a callback result, degrading malformed or unknown input to wait."""
    if not isinstance(result, Mapping):
        return IVRAgentDecision(IVRActionType.WAIT)

    action = result.get("action")
    if action == IVRActionType.DTMF.value:
        digits = result.get("digits")
        if is_valid_dtmf_output(digits):
            return IVRAgentDecision(IVRActionType.DTMF, digits)
    elif action == IVRActionType.SPEAK.value:
        text = result.get("text")
        if isinstance(text, str) and text:
            return IVRAgentDecision(IVRActionType.SPEAK, text)
    elif action == IVRActionType.HANGUP.value:
        return IVRAgentDecision(IVRActionType.HANGUP)

    return IVRAgentDecision(IVRActionType.WAIT)
