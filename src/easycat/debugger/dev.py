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

import logging
import os
import sys
import threading
from typing import TYPE_CHECKING

from easycat.debugger._install_hint import DEBUGGER_INSTALL_HINT
from easycat.debugger.session_registry import get_registry, register_session

if TYPE_CHECKING:
    from easycat.session import Session

logger = logging.getLogger("easycat.debugger.dev")

# The dev debugger UI binds loopback-only and is launched at most once per
# process. These guards mirror ``_autolaunch.py`` so the two paths agree.
_DEV_ENV = "EASYCAT_DEV"
_DEV_DISABLE_ENV = "EASYCAT_DEBUGGER_DISABLE"
_DEV_PORT_ENV = "EASYCAT_DEV_DEBUGGER_PORT"
_DEFAULT_DEV_PORT = 8765

# Process-wide "launched once" latch. The dev opt-in fires per session start,
# but the UI must bind a single port exactly once; subsequent sessions only
# register into the already-running registry.
_LAUNCH_LOCK = threading.Lock()
_LAUNCHED = False


def _is_truthy(value: str | None) -> bool:
    """Interpret an env-var string as a boolean opt-in flag."""
    if value is None:
        return False
    return value.strip().lower() not in ("", "0", "false", "no", "off")


def dev_mode_opted_in(*, dev: bool = False) -> bool:
    """Whether dev debugger mode was explicitly requested.

    Opt-in only: armed when the ``EASYCAT_DEV`` env var is truthy, or the caller
    passes ``dev=True`` (resolved from ``VoiceApp(dev=...)``). This is a
    SEPARATE trigger from the ``_autolaunch.py`` opt-ins; ``debug="full"`` alone
    never arms it.
    """
    if dev:
        return True
    return _is_truthy(os.getenv(_DEV_ENV))


def _interactive_context() -> bool:
    """Whether this process looks like an interactive developer terminal.

    Requires stderr to be a TTY and ``CI`` to be unset/falsy — a daemonised
    server or a CI runner should never have a browser tab opened for it.
    """
    if _is_truthy(os.getenv("CI")):
        return False
    try:
        return bool(sys.stderr.isatty())
    except (AttributeError, ValueError):
        return False


def reset_launch_state() -> None:
    """Reset the once-per-process launch latch. Test-only isolation helper."""
    global _LAUNCHED
    with _LAUNCH_LOCK:
        _LAUNCHED = False


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

    No-ops (returns ``None``) when dev mode is not opted in, when the disable
    guard (``EASYCAT_DEBUGGER_DISABLE`` / ``PYTEST_CURRENT_TEST``) is set, or
    when the process is non-interactive (CI, piped, no TTY) — CI must never open
    a tab. ``launch_ui=False`` registers the session and arms the once-latch
    without binding a port (used by the acceptance test, which mocks the launch).

    Returns the registry id when the session was registered, else ``None``.
    """
    global _LAUNCHED
    if not dev_mode_opted_in(dev=dev):
        return None
    if os.getenv("PYTEST_CURRENT_TEST") or _is_truthy(os.getenv(_DEV_DISABLE_ENV)):
        return None
    if not _interactive_context():
        return None

    registry_id = register_session(session)

    with _LAUNCH_LOCK:
        already_launched = _LAUNCHED
        _LAUNCHED = True
    if not already_launched and launch_ui:
        _launch_dev_ui(port=_dev_port())
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
    if os.getenv("PYTEST_CURRENT_TEST") or _is_truthy(os.getenv(_DEV_DISABLE_ENV)):
        return False
    if not _interactive_context():
        return False
    with _LAUNCH_LOCK:
        already_launched = _LAUNCHED
        _LAUNCHED = True
    if already_launched:
        return False
    if launch_ui:
        _launch_dev_ui(port=_dev_port())
    return True


def _dev_port() -> int:
    try:
        return int(os.getenv(_DEV_PORT_ENV, str(_DEFAULT_DEV_PORT)))
    except ValueError:
        return _DEFAULT_DEV_PORT
