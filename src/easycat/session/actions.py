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
        number: str | None = None,
        *,
        reason: str = "",
        no_interrupt: bool = False,
    ) -> None:
        """Queue adding a number to the session Do-Not-Call list.

        Call this from an agent function tool when the caller asks not to be
        contacted again. ``number`` is optional: when omitted (the common
        case — the caller just says "stop calling me"), the session resolves
        it from the live call's caller identity, so the agent does not need to
        know or repeat the number even under ``caller_id_exposure="off"``.

        The session applies it after the current turn via the
        :class:`CoreSessionActionExecutor`, which mutates ``session.dnc_list``
        and emits ``SessionActionStarted`` / ``SessionActionCompleted`` (or
        ``SessionActionFailed``) events that observers can subscribe to for an
        audit trail. If no number can be resolved or the session has no
        ``dnc_list`` configured, the action is a logged no-op. The durable
        record of who is on the list is the ``dnc_list`` itself (e.g.
        :class:`~easycat.telephony.compliance.SQLiteDNCList`).

        Example (OpenAI Agents SDK)::

            from agents import RunContextWrapper, function_tool
            from easycat import SessionActions

            @function_tool
            def stop_calling(ctx: RunContextWrapper[SessionActions]) -> str:
                \"\"\"Add the current caller to the do-not-call list on request.\"\"\"
                ctx.context.add_to_dnc(reason="caller requested")
                return "Understood — you won't be called again."
        """
        self.enqueue(AddToDNCAction(number=number or "", reason=reason, no_interrupt=no_interrupt))

    def remove_from_dnc(
        self,
        number: str | None = None,
        *,
        reason: str = "",
        no_interrupt: bool = False,
    ) -> None:
        """Queue removing a number from the session Do-Not-Call list.

        The complement of :meth:`add_to_dnc`, applied the same way (after the
        turn, via :class:`CoreSessionActionExecutor`, emitting the same
        auditable ``SessionAction*`` events), and with the same caller-identity
        fallback when ``number`` is omitted.
        """
        self.enqueue(
            RemoveFromDNCAction(number=number or "", reason=reason, no_interrupt=no_interrupt)
        )

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


def _current_caller_number(session: Any) -> str:
    """Resolve the live call's caller number, ignoring the exposure policy.

    Reads the *private* caller identity (the raw value that
    ``caller_id_exposure`` may hide from tools/LLM) so DNC actions can target
    the current caller even under ``"off"`` exposure — matching the behaviour
    of the removed opt-out policy.  Returns ``""`` when there is no identity.
    """
    caller_id = getattr(session, "_caller_id", None)
    identity = getattr(caller_id, "private_identity", None) if caller_id is not None else None
    return identity.caller_number if identity is not None else ""


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
            return await self._apply_dnc(session, action)
        raise TypeError(f"CoreSessionActionExecutor cannot handle {type(action).__name__}")

    async def _apply_dnc(
        self,
        session: Any,
        action: AddToDNCAction | RemoveFromDNCAction,
    ) -> SessionActionResult:
        """Add/remove a number on ``session.dnc_list``.

        The number falls back to the live call's caller identity when the
        action carries none, so an agent can DNC the current caller without
        being told the number.  A genuinely-absent number or missing
        ``dnc_list`` is a logged no-op rather than a failure, so a
        misconfigured app never crashes a turn.  Store write errors are *not*
        swallowed — they propagate so the drain loop reports
        ``SessionActionFailed`` instead of a misleading completed action.

        Native :class:`~easycat.telephony.compliance.AsyncDNCStore`
        implementations are awaited directly. Sync-only third-party stores
        are offloaded with :func:`asyncio.to_thread`, so neither path blocks
        the session event loop.
        """
        verb = "add" if isinstance(action, AddToDNCAction) else "remove"
        number = action.number or _current_caller_number(session)
        meta: dict[str, Any] = {"dnc": verb, "number": number, "reason": action.reason}
        if not number:
            logger.warning(
                "DNC %s requested but no number was given and the call has no caller "
                "identity; ignoring",
                verb,
            )
            return SessionActionResult(metadata={**meta, "applied": False, "skipped": "no_number"})
        dnc_list = getattr(session, "dnc_list", None)
        from easycat._privacy import redacted_phone_number_label

        if dnc_list is None:
            logger.warning(
                "DNC %s requested for %s but no dnc_list is configured; ignoring",
                verb,
                redacted_phone_number_label(),
            )
            return SessionActionResult(
                metadata={**meta, "applied": False, "skipped": "no_dnc_list"}
            )
        from easycat.telephony.compliance import dnc_add, dnc_remove

        if isinstance(action, AddToDNCAction):
            await dnc_add(dnc_list, number)
        else:
            await dnc_remove(dnc_list, number)
        logger.info(
            "Agent updated DNC list (%s %s): reason=%s",
            verb,
            redacted_phone_number_label(),
            action.reason,
        )
        return SessionActionResult(metadata={**meta, "applied": True})
