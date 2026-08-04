"""Canonical Session owner for turn identity publication and clearing."""

from __future__ import annotations

from easycat._epoch import Epoch, Lease
from easycat._turn_context import TurnContext

__all__ = ["TurnLifecycle"]


def _stamp_turn_generation(turn: TurnContext | None, generation: int) -> None:
    """Mirror the published generation onto the turn inside the epoch lock."""
    if turn is not None:
        turn.generation = generation


class TurnLifecycle:
    """Own one Session's turn-identity epoch and legacy generation dual-write.

    Session is the single logical writer. Readers may capture a lease from any
    thread, but liveness-sensitive effects still guard it on the event loop as
    required by :mod:`easycat._epoch`.
    """

    __slots__ = ("_identity",)

    def __init__(self) -> None:
        self._identity: Epoch[TurnContext | None] = Epoch(None)

    @property
    def current(self) -> TurnContext | None:
        """Return the identity payload current at this point-in-time read."""
        return self._identity.capture().value

    @property
    def generation(self) -> int:
        """Return the current identity generation during legacy migration."""
        return self._identity.generation

    def capture_identity(self) -> Lease[TurnContext | None]:
        """Capture the current identity generation and TurnContext atomically."""
        return self._identity.capture()

    def publish_identity(self, turn: TurnContext) -> Lease[TurnContext | None]:
        """Install ``turn``, invalidate prior leases, and dual-write its generation.

        Stamping and publication share one critical section, so a competing
        publish or clear cannot land between them and leave the installed turn
        carrying a generation that disagrees with its lease.
        """
        lease = self._identity.publish(turn, stamp=_stamp_turn_generation)
        if __debug__:
            assert lease.generation == turn.generation
            assert lease.value is turn
        return lease

    def clear_identity(self) -> Lease[TurnContext | None]:
        """Clear the installed turn and invalidate every outstanding identity lease."""
        lease = self._identity.publish(None)
        if __debug__:
            assert lease.value is None
        return lease

    def assert_legacy_generation(self, generation: int) -> None:
        """Debug-check a legacy mirror write without creating a second owner."""
        if __debug__:
            assert generation == self.generation
            current = self.current
            assert current is None or current.generation == generation
