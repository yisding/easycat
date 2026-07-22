"""Auto-launch the interactive debugger UI for a live session.

Called by :func:`easycat.config.create_session` (via a lazy in-function
import) when ``debug="full"``, so the aiohttp / debugger server stays off
the cold-start path for every other session. Lives next to the debugger
package because that is the code it actually drives.

Auto-launch is strictly **opt-in**. ``debug="full"`` keeps a durable,
crash-survivable journal but never spins up a browser tab or binds a port
on its own — that would break multi-session callers (the WebSocket and
WebTransport servers, the Twilio phone scaffold) where eight concurrent
sessions would each race for the same port and each pop a tab. Launch only
happens when a developer explicitly asks for it (``EASYCAT_DEBUGGER_AUTOLAUNCH``
or ``debugger_autolaunch=True``) *and* the process is an interactive
terminal (not CI, not a pytest run, stderr is a TTY).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import TYPE_CHECKING

from easycat._env import is_truthy
from easycat.debugger._install_hint import DEBUGGER_INSTALL_HINT

if TYPE_CHECKING:
    from easycat.session._session import Session

logger = logging.getLogger("easycat.debugger")


def _autolaunch_opted_in(config_opt_in: bool) -> bool:
    """Whether auto-launch was explicitly requested.

    Opt-in only: armed when ``EASYCAT_DEBUGGER_AUTOLAUNCH`` is truthy in the
    environment, or the caller passes ``config_opt_in=True`` (resolved from the
    ``debugger_autolaunch`` config knob). ``debug="full"`` alone
    never arms it — a durable journal must not drag a browser tab and a port
    bind onto every session.
    """
    if config_opt_in:
        return True
    return is_truthy(os.getenv("EASYCAT_DEBUGGER_AUTOLAUNCH"))


def _interactive_context() -> bool:
    """Whether this process looks like an interactive developer terminal.

    Requires stderr to be a TTY and ``CI`` to be unset/falsy. A daemonised
    server, a CI runner, or a piped process should never have a browser tab
    opened for it.
    """
    if is_truthy(os.getenv("CI")):
        return False
    try:
        return bool(sys.stderr.isatty())
    except (AttributeError, ValueError):
        # A replaced/closed stderr stream is treated as non-interactive.
        return False


def maybe_launch_debugger_ui(session: Session, *, config_opt_in: bool = False) -> None:
    """Spin up the interactive debugger on localhost, but only when opted in.

    No-ops unless **all** of the following hold:

    - Auto-launch is explicitly opted in via ``EASYCAT_DEBUGGER_AUTOLAUNCH``
      (truthy) or ``config_opt_in=True`` (resolved by the caller from the
      ``debugger_autolaunch`` config knob). ``debug="full"`` on
      its own is *not* an opt-in.
    - The process is interactive: ``sys.stderr.isatty()`` is true and ``CI``
      is unset/falsy.
    - The test/disable guards are clear: ``PYTEST_CURRENT_TEST`` and
      ``EASYCAT_DEBUGGER_DISABLE`` are both unset.

    The debugger is an optional extra (``easycat[debugger]`` → aiohttp);
    install it with ``uv add 'easycat[debugger]'`` or, from the EasyCat
    repo, ``uv sync --extra debugger --group dev``. When it isn't installed we
    log once and keep the session usable rather than crashing. Host/port
    overrides come from ``EASYCAT_DEBUGGER_PORT`` because the debugger UI is
    a local-dev convenience, not a production surface.
    """
    if os.getenv("PYTEST_CURRENT_TEST") or os.getenv("EASYCAT_DEBUGGER_DISABLE"):
        return
    if not _autolaunch_opted_in(config_opt_in):
        return
    if not _interactive_context():
        return
    # aiohttp is the real gate — the debugger module imports fine
    # without it, but the server fails the moment ``web.run_app`` is
    # called.  Probe explicitly so we log a clean skip message instead
    # of crashing a background thread.
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        logger.info("debug='full' requested but %s Skipping auto-launch.", DEBUGGER_INSTALL_HINT)
        return

    try:
        from easycat.debugger import serve_session
    except ImportError:
        logger.info(
            "debug='full' requested but the debugger module is unavailable; skipping auto-launch."
        )
        return

    try:
        port = int(os.getenv("EASYCAT_DEBUGGER_PORT", "8765"))
    except ValueError:
        port = 8765
    open_browser = os.getenv("EASYCAT_DEBUGGER_OPEN_BROWSER", "1") != "0"
    try:
        serve_session(
            session,
            port=port,
            open_browser=open_browser,
            in_thread=True,
        )
    except OSError as exc:
        logger.warning("Could not start debugger UI on port %s: %s", port, exc)
    except Exception:
        logger.exception("Debugger UI failed to start; continuing without it.")
