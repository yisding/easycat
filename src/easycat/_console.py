"""Shared color policy and stderr Rich console for the whole package.

This is the single source of truth for whether ANSI color is allowed.  The
policy honors ``NO_COLOR`` and ``CI`` (the widely-adopted disable conventions)
and otherwise falls back to a stderr TTY check.  The CLI output module
(:mod:`easycat.cli._output`), the console log handler
(:mod:`easycat._logging`), and the runtime feedback lines in
:mod:`easycat.helpers` all route their color decisions through here so the
behavior stays consistent — in particular so ``NO_COLOR``/``CI`` are honored
everywhere, not just by the CLI.
"""

from __future__ import annotations

import os
import sys

from rich.console import Console


def color_enabled() -> bool:
    """Return whether ANSI color is allowed on stderr.

    Honors ``NO_COLOR`` and ``CI=true`` (color off) before falling back to a
    stderr TTY check.
    """
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("CI") == "true":
        return False
    return sys.stderr.isatty()


# Diagnostic/feedback console (stderr; never captured for JSON output).
# The color decision is evaluated once at import time (Rich freezes it on the
# Console). Tests/automation that toggle NO_COLOR/CI must do so before importing
# easycat; mid-run env changes will not retroactively recolor this console.
_feedback_color_enabled = color_enabled()
feedback_console = Console(
    stderr=True,
    force_terminal=_feedback_color_enabled or None,
    no_color=not _feedback_color_enabled,
)
