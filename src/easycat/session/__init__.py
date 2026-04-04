"""Session: the core runtime for a single voice conversation."""

from easycat.session._session import Session
from easycat.session._streaming import AgentStreamResult, consume_agent_stream
from easycat.session._turn_context import TurnContext
from easycat.session._types import Agent, SessionConfig, SessionHelper, TurnState
from easycat.session.action_executors import CoreSessionActionExecutor
from easycat.session.actions import (
    CustomAction,
    EndCallAction,
    SendDTMFAction,
    SendSMSAction,
    SessionAction,
    SessionActionExecutor,
    SessionActionResult,
    SessionActions,
    SessionActionType,
    TransferCallAction,
    TransferPlan,
)

__all__ = [
    "Agent",
    "AgentStreamResult",
    "CoreSessionActionExecutor",
    "CustomAction",
    "EndCallAction",
    "SendDTMFAction",
    "SendSMSAction",
    "Session",
    "SessionAction",
    "SessionActionExecutor",
    "SessionActionResult",
    "SessionActionType",
    "SessionActions",
    "SessionConfig",
    "SessionHelper",
    "TransferCallAction",
    "TransferPlan",
    "TurnContext",
    "TurnState",
    "consume_agent_stream",
]
