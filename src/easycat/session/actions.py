"""Session action queue: allows agent tools to request session-level actions.

Agent tools running inside OpenAI Agents SDK or PydanticAI cannot directly
access the Session.  Instead, they call methods on a :class:`SessionActions`
instance (injected via context/deps) which enqueues actions for the Session
to execute after the current turn completes.

Usage (OpenAI Agents SDK)::

    from agents import RunContextWrapper, function_tool
    from easycat import SessionActions

    actions = SessionActions()

    @function_tool
    def end_call(ctx: RunContextWrapper[SessionActions], reason: str = "") -> str:
        ctx.context.end_call(reason=reason)
        return "Ending the call."

Usage (PydanticAI)::

    from pydantic_ai import RunContext
    from easycat import SessionActions

    @agent.tool
    def end_call(ctx: RunContext[MyDeps], reason: str = "") -> str:
        ctx.deps.actions.end_call(reason=reason)
        return "Ending the call."
"""

from __future__ import annotations

import enum
from collections import deque
from dataclasses import dataclass, field
from typing import Any


class SessionActionType(enum.Enum):
    """Types of session-level actions that tools can request."""

    END_CALL = "end_call"
    TRANSFER_CALL = "transfer_call"
    SEND_DTMF = "send_dtmf"
    CUSTOM = "custom"


@dataclass(frozen=True)
class SessionAction:
    """A queued session action requested by an agent tool."""

    type: SessionActionType
    data: dict[str, Any] = field(default_factory=dict)
    no_interrupt: bool = False


class SessionActions:
    """Action queue that agent tools use to request session-level operations.

    Inject into your agent's context (OpenAI Agents SDK) or deps
    (PydanticAI).  Tools call methods like :meth:`end_call` or
    :meth:`transfer_call` which enqueue actions.  The Session drains
    the queue after each agent turn completes.
    """

    def __init__(self) -> None:
        self._queue: deque[SessionAction] = deque()

    # ── Convenience methods for common actions ──────────────

    def end_call(self, *, reason: str = "", no_interrupt: bool = True) -> None:
        """Request that the session end after the current turn finishes.

        When *no_interrupt* is True (default), barge-in is suppressed for
        the remainder of this turn so the farewell message plays in full.
        """
        self._queue.append(
            SessionAction(
                type=SessionActionType.END_CALL,
                data={"reason": reason},
                no_interrupt=no_interrupt,
            )
        )

    def transfer_call(self, target: str, *, no_interrupt: bool = True, **kwargs: Any) -> None:
        """Request a call transfer after the current turn finishes.

        When *no_interrupt* is True (default), barge-in is suppressed for
        the remainder of this turn so the transfer announcement plays fully.
        """
        self._queue.append(
            SessionAction(
                type=SessionActionType.TRANSFER_CALL,
                data={"target": target, **kwargs},
                no_interrupt=no_interrupt,
            )
        )

    def send_dtmf(self, digits: str) -> None:
        """Request DTMF tones to be sent after the current turn finishes."""
        self._queue.append(
            SessionAction(
                type=SessionActionType.SEND_DTMF,
                data={"digits": digits},
            )
        )

    def request(self, action_type: str, **data: Any) -> None:
        """Enqueue a custom action (for user-defined action handlers)."""
        self._queue.append(
            SessionAction(
                type=SessionActionType.CUSTOM,
                data={"action_type": action_type, **data},
            )
        )

    # ── Queue access (for Session) ─────────────────────────

    def drain(self) -> list[SessionAction]:
        """Remove and return all queued actions.  Called by Session."""
        actions = list(self._queue)
        self._queue.clear()
        return actions

    @property
    def has_pending(self) -> bool:
        """Whether there are queued actions waiting to be drained."""
        return len(self._queue) > 0

    @property
    def no_interrupt(self) -> bool:
        """Whether any queued action requests barge-in suppression."""
        return any(a.no_interrupt for a in self._queue)

    def clear(self) -> None:
        """Discard all queued actions without executing them."""
        self._queue.clear()
