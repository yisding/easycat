"""Telephony helper facade exposed as ``session.telephony``.

Telephony features (outbound call manager, call-state machine, number
health monitor, disposition tracker, DTMF/voicemail detectors) are
lifecycle-managed :class:`SessionHelper` objects.  The facade collects
the typed lookups behind one ``session.telephony`` attribute so Session
itself stays out of the helper-list business: it keeps a single
``telephony`` field, and lifecycle code iterates
``session.telephony.helpers``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar

from easycat.session._types import SessionHelper

if TYPE_CHECKING:
    from easycat.telephony.call_state import OutboundCallStateMachine
    from easycat.telephony.number_health import (
        CallDispositionTracker,
        NumberHealthMonitor,
    )
    from easycat.telephony.outbound import OutboundCallManager

_HelperT = TypeVar("_HelperT")


class TelephonyFacade:
    """Typed access to the telephony helpers attached to a Session."""

    def __init__(self, helpers: list[SessionHelper]) -> None:
        self._helpers = helpers

    @property
    def helpers(self) -> list[SessionHelper]:
        """The attached telephony helpers (lifecycle-managed by Session)."""
        return self._helpers

    def get(self, helper_type: type[_HelperT]) -> _HelperT | None:
        """Return the first attached helper that is an instance of *helper_type*."""
        for helper in self._helpers:
            if isinstance(helper, helper_type):
                return helper
        return None

    # ── Named typed accessors ────────────────────────────────────

    @property
    def outbound_call_manager(self) -> OutboundCallManager | None:
        """Outbound call manager attached to this session, when configured."""
        from easycat.telephony.outbound import OutboundCallManager

        return self.get(OutboundCallManager)

    @property
    def outbound_call_state_machine(self) -> OutboundCallStateMachine | None:
        """Outbound call state machine attached to this session, when configured."""
        from easycat.telephony.call_state import OutboundCallStateMachine

        return self.get(OutboundCallStateMachine)

    @property
    def number_health_monitor(self) -> NumberHealthMonitor | None:
        """Per-number health monitor attached to this session, when configured."""
        from easycat.telephony.number_health import NumberHealthMonitor

        return self.get(NumberHealthMonitor)

    @property
    def call_disposition_tracker(self) -> CallDispositionTracker | None:
        """Call disposition tracker attached to this session, when configured."""
        from easycat.telephony.number_health import CallDispositionTracker

        return self.get(CallDispositionTracker)


__all__ = ["TelephonyFacade"]
