"""Auto-launch the interactive debugger UI for a live session.

Called by :func:`easycat.config.create_session` (via a lazy in-function
import) when ``debug="full"``, so the aiohttp / debugger server stays off
the cold-start path for every other session. Lives next to the debugger
package because that is the code it actually drives.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from easycat.session._session import Session

logger = logging.getLogger("easycat.debugger")


def maybe_launch_debugger_ui(session: Session) -> None:
    """Spin up the interactive debugger on localhost when debug="full".

    The debugger is an optional extra (``easycat[debugger]`` → aiohttp);
    when it isn't installed we log once and keep the session usable
    rather than crashing.  Pytest and CI runs are detected via
    ``PYTEST_CURRENT_TEST`` so the auto-launch never fights a test
    harness that already has the port or the loop.  Host/port
    overrides come from ``EASYCAT_DEBUGGER_PORT`` because the debugger
    UI is a local-dev convenience, not a production surface.
    """
    if os.getenv("PYTEST_CURRENT_TEST") or os.getenv("EASYCAT_DEBUGGER_DISABLE"):
        return
    # aiohttp is the real gate — the debugger module imports fine
    # without it, but the server fails the moment ``web.run_app`` is
    # called.  Probe explicitly so we log a clean skip message instead
    # of crashing a background thread.
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        logger.info(
            "debug='full' requested but easycat[debugger] is not installed; "
            "skipping auto-launch. `pip install easycat[debugger]` to enable."
        )
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
