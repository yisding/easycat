"""Process-local registry of live sessions for the dev debugger.

The dev debugger (``EASYCAT_DEV`` / ``VoiceApp(dev=True)``) launches ONE
loopback debugger UI per process and lets a developer switch between every live
session that process is running (browser/websocket/twilio fan out many
concurrent sessions). This registry is the bridge: each session registers itself
when it starts and unregisters when it stops, and the debugger app reads the
registry to populate its session selector and resolve the active source.

The registry is a deliberately small, thread-safe, module-level singleton —
there is exactly one debugger UI per process, so there is exactly one registry.
It holds only *weak-ish* bookkeeping (the session object plus a label and a
monotonically assigned id); it never starts, stops, or otherwise mutates the
sessions it tracks. Adapting a registered session into the
:class:`~easycat.debugger.server.DebuggerSource` the UI consumes is the
debugger app's job, not the registry's.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from itertools import count
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from easycat.session import Session

__all__ = [
    "LiveSessionSummary",
    "SessionRegistry",
    "get_registry",
    "list_sessions",
    "register_session",
    "unregister_session",
]


@dataclass(frozen=True)
class LiveSessionSummary:
    """The JSON-safe view of one registered session for the UI selector.

    Carries only allowlisted, non-sensitive bookkeeping — never the raw
    config or journal. The debugger app fetches records/budgets per session
    through the normal source API once the developer selects one.
    """

    registry_id: str
    session_id: str
    label: str
    is_running: bool
    turn_state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "registry_id": self.registry_id,
            "session_id": self.session_id,
            "label": self.label,
            "is_running": self.is_running,
            "turn_state": self.turn_state,
        }


class SessionRegistry:
    """Thread-safe process-local map of registry id -> live session.

    The dev debugger serves a single instance (``get_registry()``). Each entry
    pairs the live session object with a developer-facing label so the UI can
    list them; the registry assigns a stable, monotonically increasing id so a
    selector choice survives sessions starting and stopping around it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[Session, str]] = {}
        self._ids = count(1)

    def register(self, session: Session, *, label: str | None = None) -> str:
        """Register *session* and return its stable registry id.

        Re-registering the same session object returns the existing id rather
        than creating a duplicate entry (the dev opt-in fires per session start;
        a retried start must not list the same session twice).
        """
        with self._lock:
            for registry_id, (existing, _label) in self._sessions.items():
                if existing is session:
                    return registry_id
            registry_id = f"s{next(self._ids)}"
            resolved_label = label or _default_label(session, registry_id)
            self._sessions[registry_id] = (session, resolved_label)
            return registry_id

    def unregister(self, registry_id: str) -> None:
        """Drop *registry_id* if present; a no-op for unknown ids."""
        with self._lock:
            self._sessions.pop(registry_id, None)

    def get(self, registry_id: str) -> Session | None:
        """Return the live session for *registry_id*, or ``None`` if gone.

        A stopped session is pruned on access so the registry never hands the UI
        a dead session.
        """
        with self._lock:
            entry = self._sessions.get(registry_id)
            if entry is None:
                return None
            session = entry[0]
            if _session_is_closed(session):
                self._sessions.pop(registry_id, None)
                return None
            return session

    def list(self) -> list[LiveSessionSummary]:
        """Snapshot every registered session as a JSON-safe summary.

        Ordered by registry id (insertion order) so the UI selector is stable
        across polls. Stopped sessions are pruned here, so a per-connection
        server mode that registers a session per connection self-cleans without
        an explicit unregister at teardown (and the selector never lists a dead
        session). The UI polls ``list`` regularly, so eviction is prompt.
        """
        with self._lock:
            live: dict[str, tuple[Session, str]] = {}
            summaries: list[LiveSessionSummary] = []
            for registry_id, (session, label) in self._sessions.items():
                if _session_is_closed(session):
                    continue
                live[registry_id] = (session, label)
                summaries.append(
                    LiveSessionSummary(
                        registry_id=registry_id,
                        session_id=str(getattr(session, "session_id", "") or ""),
                        label=label,
                        is_running=bool(getattr(session, "is_running", False)),
                        turn_state=str(getattr(session, "turn_state", "") or ""),
                    )
                )
            self._sessions = live
            return summaries

    def clear(self) -> None:
        """Drop every entry. Used by tests to isolate the module singleton."""
        with self._lock:
            self._sessions.clear()


def _default_label(session: Session, registry_id: str) -> str:
    """Derive a readable selector label from the session id (fallback id)."""
    session_id = str(getattr(session, "session_id", "") or "")
    return session_id or registry_id


def _session_is_closed(session: Session) -> bool:
    """Whether *session* has been torn down.

    ``Session.stop()`` (the teardown verb the per-connection server modes call
    when a connection ends) flips ``_closed`` via ``_close()``. Pruning closed
    sessions lets the registry self-clean in fan-out modes without an explicit
    per-connection unregister, and keeps a dead session out of the UI selector.
    Read defensively (test fakes have no such attribute) and fall back to
    treating the session as live.
    """
    return bool(getattr(session, "_closed", False))


# ── Module-level singleton ───────────────────────────────────────────
#
# Exactly one debugger UI per process, so exactly one registry. Guard creation
# with a lock so the first ``register_session`` from a worker thread cannot race
# a second one into building two registries.

_REGISTRY: SessionRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_registry() -> SessionRegistry:
    """Return the process-local session registry, creating it on first use."""
    global _REGISTRY
    if _REGISTRY is None:
        with _REGISTRY_LOCK:
            if _REGISTRY is None:
                _REGISTRY = SessionRegistry()
    return _REGISTRY


def register_session(session: Session, *, label: str | None = None) -> str:
    """Register *session* in the process registry and return its id."""
    return get_registry().register(session, label=label)


def unregister_session(session_id: str) -> None:
    """Unregister the entry for *session_id* (a registry id) from the process."""
    get_registry().unregister(session_id)


def list_sessions() -> list[LiveSessionSummary]:
    """Return a JSON-safe summary of every registered live session."""
    return get_registry().list()
