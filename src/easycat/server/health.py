"""Health/readiness models for :class:`VoiceServer`.

``/health/ready`` is split across milestones. M4 owns the
serving/draining/capacity/route-ready half ONLY and MUST NOT import the
planner (``easycat.planning`` / ``easycat.project``) or call
``create_session``. The manifest-loaded + plan-has-no-blocking-errors checks
are deferred to M6b and gated behind the planner-vs-``create_session`` parity
test; until then the ``manifest`` / ``providers`` sub-checks report a static
placeholder.

This module also defines no metric and no metric label: ``state`` maps to the
future ``easycat.server_state`` label, but registration/emission is M8 (same
PR as the ``_observability.py`` allow-list change). M4 ships zero metric
references.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ServerState = Literal["serving", "draining"]

# Sub-check status placeholder for checks the planner owns (M6b). In M4 the
# ``manifest`` and ``providers`` sub-checks are not evaluated, so they report
# ``"skipped"`` rather than a fabricated ``"ok"`` — only ``sessions`` is live.
_DEFERRED_CHECK_STATUS = "skipped"


@dataclass(frozen=True)
class VoiceServerHealth:
    """Immutable snapshot of server health for the ``/health`` family.

    Build it from live :class:`VoiceServer` state via
    :meth:`VoiceServer.health`. ``is_ready`` evaluates ONLY the M4 readiness
    checks; the manifest/plan checks are layered on in M6b behind the parity
    gate.
    """

    state: ServerState
    active_sessions: int
    max_sessions: int
    draining: bool
    route_stack_ready: bool

    @property
    def status(self) -> str:
        """Human-facing top-level status string for the ``/health`` payload."""
        return "ok" if self.is_ready() else "degraded"

    def readiness_failures(self) -> tuple[str, ...]:
        """Return the M4 readiness checks that are currently failing.

        The reasons are deliberately content-free (no session IDs, IPs, or
        tokens) so they are safe to echo in the ``/health/ready`` body.
        """
        failures: list[str] = []
        if self.draining:
            failures.append("draining")
        if self.active_sessions >= self.max_sessions:
            failures.append("at_capacity")
        if not self.route_stack_ready:
            failures.append("route_stack_not_ready")
        return tuple(failures)

    def is_ready(self) -> bool:
        """Return ``True`` when the M4 readiness contract is satisfied.

        M4 checks only: (1) not draining, (2) ``active_sessions`` below
        ``max_sessions`` (the minimal counter, not the M5 ``Semaphore``), and
        (3) the route stack (aiohttp runner/site + raw-ws listener) is up. It
        does not import the planner or call ``create_session``.
        """
        return not self.readiness_failures()

    def checks(self) -> dict[str, dict[str, str]]:
        """Return the ``checks`` sub-object of the ``/health`` payload.

        ``manifest`` / ``providers`` are M6b-owned and report a static
        ``"skipped"`` placeholder in M4. Only ``sessions`` is live.
        """
        sessions_status = "ok"
        if self.draining or self.active_sessions >= self.max_sessions:
            sessions_status = "degraded"
        return {
            "manifest": {"status": _DEFERRED_CHECK_STATUS},
            "providers": {"status": _DEFERRED_CHECK_STATUS},
            "sessions": {"status": sessions_status},
        }

    def to_payload(self) -> dict[str, object]:
        """Return the exact ``/health`` JSON dict (stable key set)."""
        return {
            "status": self.status,
            "state": self.state,
            "active_sessions": self.active_sessions,
            "max_sessions": self.max_sessions,
            "draining": self.draining,
            "checks": self.checks(),
        }
