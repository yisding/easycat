"""Process-local registry of live sessions for the dev debugger.

The dev debugger (``EASYCAT_DEV`` / ``VoiceApp(dev=True)``) launches ONE
loopback debugger UI per process and lets a developer switch between every live
session that process is running (browser/websocket/twilio fan out many
concurrent sessions). This registry is the bridge: each session registers itself
when it starts and unregisters when it stops, and the debugger app reads the
registry to populate its session selector and resolve the active source.

The registry is a deliberately small, thread-safe, module-level singleton —
there is exactly one debugger UI per process, so there is exactly one registry.
It holds only *weak* references to the sessions it tracks (plus a label and a
monotonically assigned id) so a stopped session is never pinned alive for the
life of the process: when the last real owner drops a session it becomes
collectable and the registry prunes it (deterministically via
:func:`weakref.finalize`, with a prune-on-read backstop). It never starts,
stops, or otherwise mutates the sessions it tracks. Adapting a registered
session into the :class:`~easycat.debugger.server.DebuggerSource` the UI
consumes is the debugger app's job, not the registry's.

A monotonically increasing :meth:`SessionRegistry.version` counter bumps on
every structural change (register / unregister / prune / clear) so the debugger
can push selector updates over the live WebSocket only when something actually
changed, instead of polling.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable
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
    "unregister_session_obj",
]


@dataclass(frozen=True)
class LiveSessionSummary:
    """The JSON-safe view of one registered session for the UI selector.

    Carries only allowlisted, non-sensitive bookkeeping — never the raw
    config or journal. The debugger app fetches records per session through the
    normal source API once the developer selects one.

    ``last_sequence`` (the session journal's high-water sequence) and
    ``activity`` (``active`` while a turn is in flight, else ``idle``) are cheap,
    ``getattr``-guarded signals the UI uses to order and badge the selector — a
    best-of-class live dashboard surfaces which of N concurrent sessions is hot.
    """

    registry_id: str
    session_id: str
    label: str
    is_running: bool
    turn_state: str
    last_sequence: int = 0
    activity: str = "idle"

    def to_dict(self) -> dict[str, Any]:
        return {
            "registry_id": self.registry_id,
            "session_id": self.session_id,
            "label": self.label,
            "is_running": self.is_running,
            "turn_state": self.turn_state,
            "last_sequence": self.last_sequence,
            "activity": self.activity,
        }


# Turn states that mean "a turn is in flight" rather than waiting for input.
# Anything outside ``{"", "idle", "listening"}`` is treated as active.
_IDLE_TURN_STATES = frozenset({"", "idle", "listening"})


def _summarise(registry_id: str, session: Session, label: str) -> LiveSessionSummary:
    """Build the JSON-safe summary for one live session (all accessors guarded)."""
    turn_state = str(getattr(session, "turn_state", "") or "")
    journal = getattr(session, "journal", None)
    try:
        last_sequence = int(getattr(journal, "latest_sequence", 0) or 0)
    except (TypeError, ValueError):
        last_sequence = 0
    return LiveSessionSummary(
        registry_id=registry_id,
        session_id=str(getattr(session, "session_id", "") or ""),
        label=label,
        is_running=bool(getattr(session, "is_running", False)),
        turn_state=turn_state,
        last_sequence=last_sequence,
        activity="active" if turn_state.lower() not in _IDLE_TURN_STATES else "idle",
    )


class SessionRegistry:
    """Thread-safe process-local map of registry id -> live session (weakly held).

    The dev debugger serves a single instance (``get_registry()``). Each entry
    pairs a weak reference to the live session with a developer-facing label so
    the UI can list them; the registry assigns a stable, monotonically
    increasing id so a selector choice survives sessions starting and stopping
    around it. Dead sessions are pruned deterministically (``weakref.finalize``)
    and lazily on read, so the registry never lists a collected session nor pins
    one alive.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # registry_id -> (deref, label); ``deref()`` returns the session or None
        # once it has been collected. A weakref for the common case; a strong
        # closure as a fallback for the rare non-weakreffable object.
        self._sessions: dict[str, tuple[Callable[[], Session | None], str]] = {}
        self._ids = count(1)
        self._version = 0

    def register(self, session: Session, *, label: str | None = None) -> str:
        """Register *session* and return its stable registry id.

        Re-registering the same session object returns the existing id rather
        than creating a duplicate entry (the dev opt-in fires per session start;
        a retried start must not list the same session twice).
        """
        finalize_id: str | None = None
        with self._lock:
            for registry_id, (deref, _label) in self._sessions.items():
                if deref() is session:
                    return registry_id
            registry_id = f"s{next(self._ids)}"
            resolved_label = label or _default_label(session, registry_id)
            deref, is_weak = _make_deref(session)
            self._sessions[registry_id] = (deref, resolved_label)
            self._version += 1
            if is_weak:
                finalize_id = registry_id
        # Attach the finalizer outside the lock: its callback (``_on_finalize``)
        # itself takes the lock, and it can fire from arbitrary GC contexts.
        if finalize_id is not None:
            weakref.finalize(session, self._on_finalize, finalize_id)
        return registry_id

    def unregister(self, registry_id: str) -> None:
        """Drop *registry_id* if present; a no-op for unknown ids."""
        with self._lock:
            if self._sessions.pop(registry_id, None) is not None:
                self._version += 1

    def unregister_obj(self, session: Session) -> None:
        """Drop whichever entry holds *session* (by identity); a no-op if absent.

        Used by the stop-watcher so a session is removed the moment it closes,
        without the caller needing to remember its registry id.
        """
        with self._lock:
            for registry_id, (deref, _label) in list(self._sessions.items()):
                if deref() is session:
                    del self._sessions[registry_id]
                    self._version += 1
                    return

    def get(self, registry_id: str) -> Session | None:
        """Return the live session for *registry_id*, or ``None`` if gone.

        Prunes the entry if the session has been collected since it registered.
        """
        with self._lock:
            entry = self._sessions.get(registry_id)
            if entry is None:
                return None
            session = entry[0]()
            if session is None:
                del self._sessions[registry_id]
                self._version += 1
            return session

    def list(self) -> list[LiveSessionSummary]:
        """Snapshot every still-live session as a JSON-safe summary.

        Ordered by registry id (insertion order) so the UI selector is stable
        across polls. Dead (collected) entries are pruned in the same pass.
        """
        summaries: list[LiveSessionSummary] = []
        with self._lock:
            dead: list[str] = []
            for registry_id, (deref, label) in self._sessions.items():
                session = deref()
                if session is None:
                    dead.append(registry_id)
                    continue
                summaries.append(_summarise(registry_id, session, label))
            for registry_id in dead:
                del self._sessions[registry_id]
            if dead:
                self._version += 1
        return summaries

    def version(self) -> int:
        """Return the change counter; bumps on any register/unregister/prune."""
        with self._lock:
            return self._version

    def clear(self) -> None:
        """Drop every entry. Used by tests to isolate the module singleton."""
        with self._lock:
            if self._sessions:
                self._version += 1
            self._sessions.clear()

    def _on_finalize(self, registry_id: str) -> None:
        """``weakref.finalize`` callback: prune *registry_id* when its session dies.

        Acquires the lock non-blocking so a GC pass that fires this while the
        registry lock is already held (same thread) can never deadlock — the
        prune-on-read path in :meth:`get` / :meth:`list` is the backstop.
        """
        if not self._lock.acquire(blocking=False):
            return
        try:
            if self._sessions.pop(registry_id, None) is not None:
                self._version += 1
        finally:
            self._lock.release()


def _make_deref(session: Session) -> tuple[Callable[[], Session | None], bool]:
    """Return ``(deref, is_weak)`` for *session*.

    ``deref()`` yields the session or ``None`` once it is collected. A weak
    reference for the common case so the registry never pins a stopped session
    alive; a strong-holding closure as a fallback for the rare object that
    cannot be weak-referenced (some C-extension / ``__slots__`` types) — which
    therefore also cannot be auto-pruned via :func:`weakref.finalize`, hence the
    ``is_weak`` flag the caller uses to decide whether to attach a finalizer.
    """
    try:
        return weakref.ref(session), True
    except TypeError:
        return (lambda: session), False


def _default_label(session: Session, registry_id: str) -> str:
    """Derive a readable selector label from the session id (fallback id)."""
    session_id = str(getattr(session, "session_id", "") or "")
    return session_id or registry_id


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


def unregister_session_obj(session: Session) -> None:
    """Unregister whichever entry holds *session* (by identity) from the process."""
    get_registry().unregister_obj(session)


def list_sessions() -> list[LiveSessionSummary]:
    """Return a JSON-safe summary of every registered live session."""
    return get_registry().list()
