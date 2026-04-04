"""Typed session actions and the queue used by agent tools.

Agent tools running inside OpenAI Agents SDK or PydanticAI cannot directly
access the live :class:`~easycat.session._session.Session`. Instead, tools
enqueue typed actions on :class:`SessionActions`. The session drains the queue
after the current turn completes and executes the actions through configured
executors.
"""

from __future__ import annotations

import enum
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol
from uuid import uuid4


class SessionActionType(enum.StrEnum):
    """Types of session-level actions that tools can request."""

    END_CALL = "end_call"
    TRANSFER_CALL = "transfer_call"
    SEND_DTMF = "send_dtmf"
    SEND_SMS = "send_sms"
    CUSTOM = "custom"


@dataclass(frozen=True, slots=True)
class SessionAction:
    """Base class for queued session actions."""

    action_type: ClassVar[SessionActionType]

    id: str = field(default_factory=lambda: uuid4().hex, kw_only=True)
    no_interrupt: bool = field(default=False, kw_only=True)

    @property
    def type(self) -> SessionActionType:
        return type(self).action_type


@dataclass(frozen=True, slots=True)
class EndCallAction(SessionAction):
    """Request that the session end after the current turn."""

    action_type: ClassVar[SessionActionType] = SessionActionType.END_CALL

    reason: str = ""


@dataclass(frozen=True, slots=True)
class TransferPlan:
    """Provider-neutral transfer options."""

    client_message: str = ""
    post_dial_digits: str = ""
    caller_id: str | None = None


@dataclass(frozen=True, slots=True)
class TransferCallAction(SessionAction):
    """Request that the call transfer to another destination."""

    action_type: ClassVar[SessionActionType] = SessionActionType.TRANSFER_CALL

    target: str = ""
    reason: str = ""
    plan: TransferPlan = field(default_factory=TransferPlan)


@dataclass(frozen=True, slots=True)
class SendDTMFAction(SessionAction):
    """Request that DTMF digits be sent on the call."""

    action_type: ClassVar[SessionActionType] = SessionActionType.SEND_DTMF

    digits: str = ""
    inter_digit_delay_ms: int = 1000


@dataclass(frozen=True, slots=True)
class SendSMSAction(SessionAction):
    """Request that the system send an SMS message."""

    action_type: ClassVar[SessionActionType] = SessionActionType.SEND_SMS

    to: str = ""
    body: str = ""


@dataclass(frozen=True, slots=True)
class CustomAction(SessionAction):
    """User-defined session action with arbitrary payload."""

    action_type: ClassVar[SessionActionType] = SessionActionType.CUSTOM

    name: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionActionResult:
    """Result returned by an action executor."""

    stop_session: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class SessionActionExecutor(Protocol):
    """Provider-neutral execution protocol for session actions."""

    def supports(self, action: SessionAction) -> bool: ...

    async def execute(self, session: Any, action: SessionAction) -> SessionActionResult: ...


class SessionActions:
    """Thread-safe queue used by agent tools to request session actions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: deque[SessionAction] = deque()
        self._no_interrupt = False

    def enqueue(self, action: SessionAction) -> None:
        """Append a pre-built action object to the queue."""
        with self._lock:
            if action.no_interrupt:
                self._no_interrupt = True
            self._queue.append(action)

    def end_call(
        self,
        *,
        reason: str = "",
        no_interrupt: bool = True,
    ) -> None:
        self.enqueue(
            EndCallAction(
                reason=reason,
                no_interrupt=no_interrupt,
            )
        )

    def transfer_call(
        self,
        target: str,
        *,
        reason: str = "",
        plan: TransferPlan | None = None,
        no_interrupt: bool = True,
    ) -> None:
        self.enqueue(
            TransferCallAction(
                target=target,
                reason=reason,
                plan=plan or TransferPlan(),
                no_interrupt=no_interrupt,
            )
        )

    def send_dtmf(
        self,
        digits: str,
        *,
        inter_digit_delay_ms: int = 1000,
        no_interrupt: bool = False,
    ) -> None:
        self.enqueue(
            SendDTMFAction(
                digits=digits,
                inter_digit_delay_ms=inter_digit_delay_ms,
                no_interrupt=no_interrupt,
            )
        )

    def send_sms(
        self,
        to: str,
        body: str,
        *,
        no_interrupt: bool = False,
    ) -> None:
        self.enqueue(SendSMSAction(to=to, body=body, no_interrupt=no_interrupt))

    def request(
        self,
        name: str,
        *,
        payload: dict[str, Any] | None = None,
        no_interrupt: bool = False,
    ) -> None:
        self.enqueue(CustomAction(name=name, payload=payload or {}, no_interrupt=no_interrupt))

    def drain(self) -> list[SessionAction]:
        """Remove and return all queued actions."""
        with self._lock:
            actions = list(self._queue)
            self._queue.clear()
            self._no_interrupt = False
            return actions

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._queue)

    @property
    def no_interrupt(self) -> bool:
        with self._lock:
            return self._no_interrupt

    def clear(self) -> None:
        with self._lock:
            self._queue.clear()
            self._no_interrupt = False
