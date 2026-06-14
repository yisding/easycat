"""Typed session actions and the queue used by agent tools.

Agent tools running inside OpenAI Agents SDK or PydanticAI cannot directly
access the live :class:`~easycat.session._session.Session`. Instead, tools
enqueue typed actions on :class:`SessionActions`. The session drains the queue
after the current turn completes and executes the actions through configured
executors.
"""

from __future__ import annotations

import enum
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, ClassVar, Protocol
from uuid import uuid4

logger = logging.getLogger(__name__)

MAX_DTMF_DIGITS = 128
MAX_DTMF_INTER_DIGIT_DELAY_MS = 10_000


def _validate_dtmf_digits(digits: str) -> None:
    if len(digits) > MAX_DTMF_DIGITS:
        raise ValueError(f"DTMF digits must be {MAX_DTMF_DIGITS} characters or fewer")


class SessionActionType(enum.StrEnum):
    """Types of session-level actions that tools can request."""

    END_CALL = "end_call"
    TRANSFER_CALL = "transfer_call"
    SEND_DTMF = "send_dtmf"
    SEND_SMS = "send_sms"
    ADD_TO_DNC = "add_to_dnc"
    REMOVE_FROM_DNC = "remove_from_dnc"
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

    def __post_init__(self) -> None:
        _validate_dtmf_digits(self.post_dial_digits)


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

    def __post_init__(self) -> None:
        _validate_dtmf_digits(self.digits)
        if not 0 <= self.inter_digit_delay_ms <= MAX_DTMF_INTER_DIGIT_DELAY_MS:
            raise ValueError(
                f"DTMF inter_digit_delay_ms must be between 0 and {MAX_DTMF_INTER_DIGIT_DELAY_MS}"
            )


@dataclass(frozen=True, slots=True)
class SendSMSAction(SessionAction):
    """Request that the system send an SMS message."""

    action_type: ClassVar[SessionActionType] = SessionActionType.SEND_SMS

    to: str = ""
    body: str = ""


@dataclass(frozen=True, slots=True)
class AddToDNCAction(SessionAction):
    """Request that a phone number be added to the session Do-Not-Call list."""

    action_type: ClassVar[SessionActionType] = SessionActionType.ADD_TO_DNC

    number: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RemoveFromDNCAction(SessionAction):
    """Request that a phone number be removed from the session Do-Not-Call list."""

    action_type: ClassVar[SessionActionType] = SessionActionType.REMOVE_FROM_DNC

    number: str = ""
    reason: str = ""


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

    def add_to_dnc(
        self,
        number: str,
        *,
        reason: str = "",
        no_interrupt: bool = False,
    ) -> None:
        """Queue adding ``number`` to the session Do-Not-Call list.

        Call this from an agent function tool when the caller asks not to be
        contacted again. The session applies it after the current turn via the
        :class:`CoreSessionActionExecutor`, which mutates ``session.dnc_list``
        and emits ``SessionActionStarted`` / ``SessionActionCompleted`` events
        so the request is journaled for audit. If the session has no
        ``dnc_list`` configured the action is a logged no-op.

        Example (OpenAI Agents SDK)::

            from agents import RunContextWrapper, function_tool
            from easycat import SessionActions

            @function_tool
            def stop_calling(ctx: RunContextWrapper[SessionActions], number: str) -> str:
                \"\"\"Add the caller to the do-not-call list on request.\"\"\"
                ctx.context.add_to_dnc(number, reason="caller requested")
                return "Understood — you won't be called again."
        """
        self.enqueue(AddToDNCAction(number=number, reason=reason, no_interrupt=no_interrupt))

    def remove_from_dnc(
        self,
        number: str,
        *,
        reason: str = "",
        no_interrupt: bool = False,
    ) -> None:
        """Queue removing ``number`` from the session Do-Not-Call list.

        The complement of :meth:`add_to_dnc`, applied the same way (after the
        turn, via :class:`CoreSessionActionExecutor`, journaled for audit).
        """
        self.enqueue(RemoveFromDNCAction(number=number, reason=reason, no_interrupt=no_interrupt))

    def request(
        self,
        name: str,
        *,
        payload: dict[str, Any] | None = None,
        no_interrupt: bool = False,
    ) -> None:
        self.enqueue(CustomAction(name=name, payload=payload or {}, no_interrupt=no_interrupt))

    def drain(self, *, preserve_no_interrupt: bool = False) -> list[SessionAction]:
        """Remove and return all queued actions.

        Parameters
        ----------
        preserve_no_interrupt:
            Keep the interrupt guard active after removing the actions.
            Session turn finalization uses this while it executes deferred
            end-call/transfer actions and waits for already-queued outbound
            audio to drain.
        """
        with self._lock:
            actions = list(self._queue)
            self._queue.clear()
            if not preserve_no_interrupt:
                self._no_interrupt = False
            return actions

    def clear_no_interrupt(self) -> None:
        """Clear any drained-action interrupt guard.

        If another thread queued a new no-interrupt action while the drained
        actions were being handled, keep the guard active for the still-pending
        action.
        """
        with self._lock:
            self._no_interrupt = any(action.no_interrupt for action in self._queue)

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


@dataclass(slots=True)
class CoreSessionActionExecutor(SessionActionExecutor):
    """Executor for provider-neutral core session actions.

    Always registered on every session, so :meth:`SessionActions.end_call`,
    :meth:`SessionActions.add_to_dnc`, and :meth:`SessionActions.remove_from_dnc`
    work without any extra wiring.
    """

    def supports(self, action: SessionAction) -> bool:
        return isinstance(action, EndCallAction | AddToDNCAction | RemoveFromDNCAction)

    async def execute(self, session: Any, action: SessionAction) -> SessionActionResult:
        if isinstance(action, EndCallAction):
            logger.info("Agent requested end_call: reason=%s", action.reason)
            return SessionActionResult(stop_session=True, metadata={"reason": action.reason})
        if isinstance(action, AddToDNCAction | RemoveFromDNCAction):
            return self._apply_dnc(session, action)
        raise TypeError(f"CoreSessionActionExecutor cannot handle {type(action).__name__}")

    def _apply_dnc(
        self,
        session: Any,
        action: AddToDNCAction | RemoveFromDNCAction,
    ) -> SessionActionResult:
        """Add/remove ``action.number`` on ``session.dnc_list``.

        Returns a result whose ``metadata`` records whether the change was
        applied; a missing ``dnc_list`` or empty number is a logged no-op
        rather than a failure, so a misconfigured app never crashes a turn.
        """
        verb = "add" if isinstance(action, AddToDNCAction) else "remove"
        number = action.number
        meta: dict[str, Any] = {"dnc": verb, "number": number, "reason": action.reason}
        if not number:
            logger.warning("DNC %s requested with an empty number; ignoring", verb)
            return SessionActionResult(
                metadata={**meta, "applied": False, "skipped": "empty_number"}
            )
        dnc_list = getattr(session, "dnc_list", None)
        if dnc_list is None:
            logger.warning(
                "DNC %s requested for %s but no dnc_list is configured; ignoring", verb, number
            )
            return SessionActionResult(
                metadata={**meta, "applied": False, "skipped": "no_dnc_list"}
            )
        try:
            if isinstance(action, AddToDNCAction):
                dnc_list.add(number)
            else:
                dnc_list.remove(number)
        except Exception as exc:
            logger.exception("DNC %s failed for %s", verb, number)
            return SessionActionResult(metadata={**meta, "applied": False, "error": str(exc)})
        logger.info("Agent updated DNC list (%s %s): reason=%s", verb, number, action.reason)
        return SessionActionResult(metadata={**meta, "applied": True})
