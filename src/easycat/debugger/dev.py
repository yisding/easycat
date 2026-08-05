"""Dev debugger mode — the always-available local timeline.

Activated by ``EASYCAT_DEV=1 easycat serve`` or programmatically with
``VoiceApp(agent=..., dev=True)``. Dev mode is a developer convenience that
makes the journal/timeline debugger the default local feedback loop:

- it defaults to durable debugging when no explicit debug config was supplied,
- it registers every live session in the process-local
  :mod:`~easycat.debugger.session_registry`,
- it launches ONE loopback debugger UI per process (a session selector lets the
  developer switch between concurrently running sessions).

**Purely additive over the existing autolaunch guard (R7).** The
``debug="full"``-alone-never-autolaunches guarantee lives in
:mod:`easycat.debugger._autolaunch` and is unchanged: ``debug="full"`` on its
own still arms no port bind and no browser tab. Dev mode adds a *separate*,
explicit opt-in trigger (``EASYCAT_DEV`` / ``dev=True``); it never relaxes the
``debug="full"`` behavior. The launch is funnelled through one hook
(:func:`maybe_launch_dev_debugger`) so the acceptance test can assert it fires
exactly once for the dev opt-in and never for ``debug="full"`` alone.

Safety mirrors :mod:`easycat.debugger._autolaunch`: loopback bind only, never in
CI / non-interactive / pytest contexts, and the dev UI is gated behind the
optional ``easycat[debugger]`` extra (a clean skip when aiohttp is absent).
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import threading
from typing import TYPE_CHECKING

from easycat._env import is_truthy
from easycat.debugger._install_hint import DEBUGGER_INSTALL_HINT
from easycat.debugger.session_registry import (
    get_registry,
    register_session,
    unregister_session_obj,
)

if TYPE_CHECKING:
    from easycat.session import Session

logger = logging.getLogger("easycat.debugger.dev")

# The dev debugger UI binds loopback-only and is launched at most once per
# process. These guards mirror ``_autolaunch.py`` so the two paths agree.
_DEV_ENV = "EASYCAT_DEV"
_DEV_DISABLE_ENV = "EASYCAT_DEBUGGER_DISABLE"
_DEV_PORT_ENV = "EASYCAT_DEV_DEBUGGER_PORT"
_DEFAULT_DEV_PORT = 8765
_MIN_TCP_PORT = 1
_MAX_TCP_PORT = 65535
# How many consecutive loopback ports to try when the default is taken, so a
# second dev process (or anything sitting on 8765) gets the next free port
# instead of permanently suppressing the UI.
_DEV_PORT_SCAN_SPAN = 11
_DEV_WATCHER_MEMBER_PREFIX = "debugger_dev_watcher"

# Process-wide "launched once" latch. The dev opt-in fires per session start,
# but the UI must bind a single port exactly once; subsequent sessions only
# register into the already-running registry.
_LAUNCH_LOCK = threading.Lock()
_LAUNCHED = False

# Separate, process-wide latch for SESSION REGISTRATION. Registration is
# decoupled from UI launch: the UI launch is gated by the interactive/CI
# guards (a browser tab must never open in CI), but registration must still
# populate the registry headless and under test so the per-connection sessions
# of server modes — built downstream via ``create_session`` — show up in the
# selector. Armed whenever dev mode is opted in (env or ``dev=True``).
_DEV_REGISTRATION_ARMED = False


def dev_mode_opted_in(*, dev: bool = False) -> bool:
    """Whether dev debugger mode was explicitly requested.

    Opt-in only: armed when the ``EASYCAT_DEV`` env var is truthy, or the caller
    passes ``dev=True`` (resolved from ``VoiceApp(dev=...)``). This is a
    SEPARATE trigger from the ``_autolaunch.py`` opt-ins; ``debug="full"`` alone
    never arms it.
    """
    if dev:
        return True
    return is_truthy(os.getenv(_DEV_ENV))


def _interactive_context() -> bool:
    """Whether this process looks like an interactive developer terminal.

    Requires stderr to be a TTY and ``CI`` to be unset/falsy — a daemonised
    server or a CI runner should never have a browser tab opened for it.
    """
    if is_truthy(os.getenv("CI")):
        return False
    try:
        return bool(sys.stderr.isatty())
    except (AttributeError, ValueError):
        return False


def _registration_armed() -> bool:
    """Whether sessions should auto-register into the dev registry.

    True when ``EASYCAT_DEV`` is set OR a launch hook armed it for this process
    (so ``VoiceApp(dev=True)`` without the env var still populates the registry).
    Unlike the UI launch, this is NOT gated by the interactive/CI/pytest guards:
    the registry must reflect every live session even headless or under test.
    """
    return is_truthy(os.getenv(_DEV_ENV)) or _DEV_REGISTRATION_ARMED


def _arm_registration() -> None:
    """Latch on session auto-registration for the rest of the process."""
    global _DEV_REGISTRATION_ARMED
    with _LAUNCH_LOCK:
        _DEV_REGISTRATION_ARMED = True


def _reset_launched_latch() -> None:
    """Roll back the launch-once latch so a later session can retry the bind."""
    global _LAUNCHED
    with _LAUNCH_LOCK:
        _LAUNCHED = False


def reset_launch_state() -> None:
    """Reset the once-per-process launch + registration latches (test-only)."""
    global _LAUNCHED, _DEV_REGISTRATION_ARMED
    with _LAUNCH_LOCK:
        _LAUNCHED = False
        _DEV_REGISTRATION_ARMED = False


def arm_dev_session(session: Session) -> str | None:
    """Register *session* in the dev registry and watch it unregister on close.

    Called from the single ``create_session`` funnel so EVERY mode — local plus
    the per-connection server modes (browser/websocket/twilio/webrtc) whose
    sessions are built downstream — populates the selector uniformly. A no-op
    (returns ``None``) unless dev registration is armed.

    When an event loop is running (true in every server-mode accept handler) a
    tiny watcher task unregisters the session the moment it closes, so the
    selector reflects reality without leaking dead entries. With no running loop
    (a synchronous local build) registration still happens and the registry's
    weakref pruning drops the entry once the caller releases the session.

    Returns the registry id when the session was registered, else ``None``.
    """
    if not _registration_armed():
        return None
    registry_id = register_session(session)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return registry_id
    scope = session._runtime_scope
    member_name = f"{_DEV_WATCHER_MEMBER_PREFIX}:{registry_id}"
    if scope.tasks(member_name):
        return registry_id
    try:
        scope.create_task(
            member_name,
            _unregister_on_close(session),
            task_name=member_name,
        )
    except RuntimeError:
        # A Session whose root has already closed cannot become live in the
        # debugger registry again. Match the watcher's eventual cleanup now.
        unregister_session_obj(session)
        logger.debug("dev session runtime is closed; registration removed")
    return registry_id


async def _unregister_on_close(session: Session) -> None:
    """Await *session* closing, then drop it from the dev registry."""
    waiter = getattr(session, "wait_closed", None)
    try:
        if callable(waiter):
            await waiter()
    except Exception:
        logger.debug("dev session wait_closed failed; unregistering anyway", exc_info=True)
    finally:
        unregister_session_obj(session)


def _launch_dev_ui(*, port: int) -> None:
    """Spin up the registry-backed dev debugger UI on a background thread.

    Probes for aiohttp first (the debugger is an optional extra) so a missing
    dependency degrades to a logged skip rather than crashing the live session.
    Binds loopback-only; the registry-backed app serves a session selector over
    every session the process registers.
    """
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        logger.info(
            "EASYCAT_DEV requested but %s Skipping dev debugger UI.", DEBUGGER_INSTALL_HINT
        )
        return

    try:
        from easycat.debugger.server import serve_dev_registry
    except ImportError:
        logger.info("EASYCAT_DEV requested but the debugger module is unavailable; skipping.")
        return

    try:
        serve_dev_registry(
            get_registry(),
            host="127.0.0.1",
            port=port,
            open_browser=os.getenv("EASYCAT_DEBUGGER_OPEN_BROWSER", "1") != "0",
            in_thread=True,
        )
    except OSError as exc:
        logger.warning("Could not start dev debugger UI on port %s: %s", port, exc)
    except Exception:
        logger.exception("Dev debugger UI failed to start; continuing without it.")


def maybe_launch_dev_debugger(
    session: Session,
    *,
    dev: bool = False,
    launch_ui: bool = True,
) -> str | None:
    """Register *session* for dev mode and launch the debugger UI once.

    This is the single dev-mode HOOK (acceptance: it fires exactly once per
    process for the dev opt-in, and never for ``debug="full"`` alone). When dev
    mode is opted in:

    1. *session* is registered in the process-local session registry (so the UI
       selector lists it), and
    2. the loopback dev debugger UI is launched ONCE per process — subsequent
       sessions only register, they do not re-bind the port.

    No-ops (returns ``None``) when dev mode is not opted in or when the disable
    guard (``EASYCAT_DEBUGGER_DISABLE``) is set. Pytest/CI/non-interactive
    contexts suppress only the UI bind/browser launch — registration still
    happens so headless server-mode selectors reflect live sessions. CI must
    never open a tab. ``launch_ui=False`` registers the session and arms the
    once-latch without binding a port (used by the acceptance test, which mocks
    the launch).

    Returns the registry id when the session was registered, else ``None``.
    """
    global _LAUNCHED
    if not dev_mode_opted_in(dev=dev):
        return None
    # Arm registration first (decoupled from the UI guards below) so the
    # create_session funnel registers every session even when the UI itself is
    # suppressed in CI / non-interactive shells.
    _arm_registration()
    if is_truthy(os.getenv(_DEV_DISABLE_ENV)):
        return None
    registry_id = arm_dev_session(session)
    if os.getenv("PYTEST_CURRENT_TEST"):
        return registry_id
    if not _interactive_context():
        return registry_id

    with _LAUNCH_LOCK:
        already_launched = _LAUNCHED
        _LAUNCHED = True
    if not already_launched and launch_ui:
        port = _dev_port()
        if port is None:
            _reset_launched_latch()
            logger.warning(
                "No free dev debugger port in %d..%d; skipping UI.",
                _DEFAULT_DEV_PORT,
                _DEFAULT_DEV_PORT + _DEV_PORT_SCAN_SPAN - 1,
            )
        else:
            _launch_dev_ui(port=port)
    return registry_id


def maybe_launch_dev_registry_ui(*, dev: bool = False, launch_ui: bool = True) -> bool:
    """Launch the dev debugger registry UI once for a server-mode process.

    The per-connection server modes (browser/websocket/twilio) build sessions
    downstream of ``VoiceApp``, so there is no single session to hand to
    :func:`maybe_launch_dev_debugger` at serve time. This launches the
    registry-backed UI eagerly (the selector fills in as sessions register
    themselves), gated by the same dev opt-in and interactive/CI guards.

    Returns ``True`` when the launch hook fired this call (the once-per-process
    latch flips), else ``False``. ``launch_ui=False`` arms the latch without
    binding a port (used by tests that mock the launch).
    """
    global _LAUNCHED
    if not dev_mode_opted_in(dev=dev):
        return False
    # Arm registration up front so per-connection sessions register as they are
    # built downstream, even though the UI launch below is gated by CI/TTY.
    _arm_registration()
    if os.getenv("PYTEST_CURRENT_TEST") or is_truthy(os.getenv(_DEV_DISABLE_ENV)):
        return False
    if not _interactive_context():
        return False
    with _LAUNCH_LOCK:
        already_launched = _LAUNCHED
        _LAUNCHED = True
    if already_launched:
        return False
    if not launch_ui:
        return True
    port = _dev_port()
    if port is None:
        _reset_launched_latch()
        logger.warning(
            "No free dev debugger port in %d..%d; skipping UI.",
            _DEFAULT_DEV_PORT,
            _DEFAULT_DEV_PORT + _DEV_PORT_SCAN_SPAN - 1,
        )
        return False
    _launch_dev_ui(port=port)
    return True


def _port_is_free(host: str, port: int) -> bool:
    """Whether ``(host, port)`` can be bound right now (loopback probe)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _find_free_dev_port(host: str, start: int, span: int) -> int | None:
    """Return the first free loopback port in ``[start, start+span)``, else None."""
    for port in range(start, start + span):
        if _port_is_free(host, port):
            return port
    return None


def _dev_port() -> int | None:
    """Resolve the port for the dev debugger UI.

    An explicit, valid ``EASYCAT_DEV_DEBUGGER_PORT`` is honored verbatim (no
    scan) so a developer can pin a port. Invalid overrides fall back to the
    normal scan: port ``0`` would bind an ephemeral server port while the
    browser opens ``:0``, and out-of-range values fail later in the bind path.
    Otherwise scan a small loopback range from the default so a second dev
    process — or anything already on 8765 — lands on the next free port instead
    of silently never showing a UI. Returns ``None`` when the whole range is
    occupied (the caller leaves the launch latch un-consumed so a later session
    can retry).
    """
    override = os.getenv(_DEV_PORT_ENV)
    if override is not None:
        try:
            port = int(override)
        except ValueError:
            port = 0
        if _MIN_TCP_PORT <= port <= _MAX_TCP_PORT:
            return port
    return _find_free_dev_port("127.0.0.1", _DEFAULT_DEV_PORT, _DEV_PORT_SCAN_SPAN)
