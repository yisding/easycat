"""Health/readiness models for :class:`VoiceServer`.

``/health/ready`` is split across milestones. M4 owns the
serving/draining/capacity/route-ready half. This MODULE never imports the
planner (``easycat.planning``); :class:`VoiceServerHealth` is a pure value
object built FROM live state by :meth:`VoiceServer.health`, which imports the
planner LAZILY (so ``import easycat.server`` still pulls no planner — the M4
boundary).

The M6b manifest-loaded + plan-has-no-blocking-errors checks are GATED behind
the planner-vs-``create_session`` parity test passing. They are wired in as
OPTIONAL fields here: a factory-only server (no manifest) leaves them ``None``,
so the ``manifest`` / ``providers`` sub-checks keep the M4 ``"skipped"``
placeholder; a ``from_manifest`` server fills them and they gate readiness.

This module also defines no metric and no metric label: ``state`` maps to the
future ``easycat.server_state`` label, but registration/emission is M8 (same
PR as the ``_observability.py`` allow-list change). M4 ships zero metric
references.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ServerState = Literal["serving", "draining"]

# Sub-check status placeholder for checks the planner owns (M6b). For a
# factory-only server (no manifest) the ``manifest`` and ``providers`` sub-checks
# are not evaluated, so they report ``"skipped"`` rather than a fabricated
# ``"ok"`` — only ``sessions`` is live there.
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
    # M6b readiness sub-checks (parity-gated). ``None`` means "not evaluated"
    # (a factory-only server with no manifest), which keeps the M4 ``"skipped"``
    # placeholder and does NOT gate readiness. ``manifest_loaded=False`` means a
    # configured manifest failed to load; ``plan_blocking_errors`` carries the
    # plan's blocking-error reasons (empty tuple when clean).
    manifest_loaded: bool | None = None
    plan_blocking_errors: tuple[str, ...] | None = None

    @property
    def status(self) -> str:
        """Human-facing top-level status string for the ``/health`` payload."""
        return "ok" if self.is_ready() else "degraded"

    def readiness_failures(self) -> tuple[str, ...]:
        """Return the readiness checks that are currently failing.

        M4 checks (draining / capacity / route-ready) plus the M6b
        manifest-loaded + plan-no-blocking-errors checks WHEN evaluated (a
        ``from_manifest`` server). The reasons are deliberately content-free (no
        session IDs, IPs, or tokens) so they are safe to echo in the
        ``/health/ready`` body.
        """
        failures: list[str] = []
        if self.draining:
            failures.append("draining")
        if self.active_sessions >= self.max_sessions:
            failures.append("at_capacity")
        if not self.route_stack_ready:
            failures.append("route_stack_not_ready")
        # M6b checks, only when evaluated (manifest-backed server).
        if self.manifest_loaded is False:
            failures.append("manifest_not_loaded")
        if self.plan_blocking_errors:
            failures.append("plan_has_blocking_errors")
        return tuple(failures)

    def is_ready(self) -> bool:
        """Return ``True`` when the readiness contract is satisfied.

        M4 checks: (1) not draining, (2) ``active_sessions`` below
        ``max_sessions``, and (3) the route stack (aiohttp runner/site + raw-ws
        listener) is up. M6b adds, for a ``from_manifest`` server only, (4) the
        manifest loaded and (5) the provider plan has no blocking errors.
        """
        return not self.readiness_failures()

    def checks(self) -> dict[str, dict[str, str]]:
        """Return the ``checks`` sub-object of the ``/health`` payload.

        ``manifest`` / ``providers`` report ``"skipped"`` for a factory-only
        server (M4 placeholder) and a live status for a ``from_manifest`` server
        (M6b). ``sessions`` is always live.
        """
        sessions_status = "ok"
        if self.draining or self.active_sessions >= self.max_sessions:
            sessions_status = "degraded"

        if self.manifest_loaded is None:
            manifest_status = _DEFERRED_CHECK_STATUS
        else:
            manifest_status = "ok" if self.manifest_loaded else "degraded"

        if self.plan_blocking_errors is None:
            providers_status = _DEFERRED_CHECK_STATUS
        else:
            providers_status = "degraded" if self.plan_blocking_errors else "ok"

        return {
            "manifest": {"status": manifest_status},
            "providers": {"status": providers_status},
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
