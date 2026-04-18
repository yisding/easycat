"""Session: the core runtime for a single voice conversation.

Re-exports load lazily via PEP 562 ``__getattr__``.  This matters
because ``stages.agent`` imports :class:`TurnContext` from
``session._turn_context``; if this ``__init__.py`` eagerly loaded
``_session`` (which in turn imports every stage class), we would hit a
``stages.agent ↔ session._session`` circular import whenever the
stages package was loaded cold.

Consumers who need ``Session`` itself continue to use
``from easycat.session import Session`` — it still works, just
lazily.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

_LAZY: dict[str, tuple[str, str]] = {
    # Core Session class
    "Session": ("easycat.session._session", "Session"),
    # Streaming helpers
    "AgentStreamResult": ("easycat.session._streaming", "AgentStreamResult"),
    "consume_agent_stream": ("easycat.session._streaming", "consume_agent_stream"),
    # Per-turn state
    "TurnContext": ("easycat.session._turn_context", "TurnContext"),
    # Session types
    "Agent": ("easycat.session._types", "Agent"),
    "SessionConfig": ("easycat.session._types", "SessionConfig"),
    "SessionHelper": ("easycat.session._types", "SessionHelper"),
    "TurnState": ("easycat.session._types", "TurnState"),
    # Action executor + actions
    "CoreSessionActionExecutor": (
        "easycat.session.action_executors",
        "CoreSessionActionExecutor",
    ),
    "CustomAction": ("easycat.session.actions", "CustomAction"),
    "EndCallAction": ("easycat.session.actions", "EndCallAction"),
    "SendDTMFAction": ("easycat.session.actions", "SendDTMFAction"),
    "SendSMSAction": ("easycat.session.actions", "SendSMSAction"),
    "SessionAction": ("easycat.session.actions", "SessionAction"),
    "SessionActionExecutor": ("easycat.session.actions", "SessionActionExecutor"),
    "SessionActionResult": ("easycat.session.actions", "SessionActionResult"),
    "SessionActions": ("easycat.session.actions", "SessionActions"),
    "SessionActionType": ("easycat.session.actions", "SessionActionType"),
    "TransferCallAction": ("easycat.session.actions", "TransferCallAction"),
    "TransferPlan": ("easycat.session.actions", "TransferPlan"),
}


if TYPE_CHECKING:
    # Static-analysis view — imports never execute at runtime.
    from easycat.session._session import Session
    from easycat.session._streaming import (
        AgentStreamResult,
        consume_agent_stream,
    )
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


def __getattr__(name: str):  # PEP 562
    try:
        module_path, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(f"module 'easycat.session' has no attribute {name!r}") from None
    module = importlib.import_module(module_path)
    value = getattr(module, attr)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(list(globals()) + list(_LAZY)))


__all__ = sorted(_LAZY)
