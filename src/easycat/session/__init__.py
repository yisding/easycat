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
    # Per-turn state
    "TurnContext": ("easycat.session._turn_context", "TurnContext"),
    # Session types
    "Agent": ("easycat.session._types", "Agent"),
    "CallDirection": ("easycat.session._types", "CallDirection"),
    "CallIdentity": ("easycat.session._types", "CallIdentity"),
    "CallerIdExposure": ("easycat.session._types", "CallerIdExposure"),
    "SessionConfig": ("easycat.session._types", "SessionConfig"),
    "TurnState": ("easycat.session._types", "TurnState"),
    # Interruption helpers (public for bridge authors inspecting sessions)
    "estimate_and_notify_interruption": (
        "easycat.session.interruption",
        "estimate_and_notify_interruption",
    ),
    # Action executor + actions
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
    from easycat.session._turn_context import TurnContext
    from easycat.session._types import (
        Agent,
        CallDirection,
        CallerIdExposure,
        CallIdentity,
        SessionConfig,
        TurnState,
    )
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
    from easycat.session.interruption import estimate_and_notify_interruption


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
