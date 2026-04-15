"""Interactive debugger UI for EasyCat sessions and bundles.

Implements the peripheral-eval-and-debugger-ui.md interactive debugger:
a single-process aiohttp server with a timeline view, per-turn
waterfall, record inspector, and audio playback.  Reads the journal +
artifact store directly — no separate telemetry pipeline.

The UI is opt-in: install ``easycat[debugger]`` to pull aiohttp.

Typical usage:

.. code-block:: python

    from easycat.debugger import serve_bundle
    serve_bundle("recording.zip", port=8765)

or for a live session:

.. code-block:: python

    from easycat.debugger import serve_session
    serve_session(session, port=8765)
"""

from easycat.debugger.server import (
    DebuggerSource,
    serve_bundle,
    serve_session,
)

__all__ = [
    "DebuggerSource",
    "serve_bundle",
    "serve_session",
]
