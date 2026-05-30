"""Shared output helpers: Rich console, human/JSON modes, exit codes.

All CLI commands route their output through this module so the
human/JSON contract stays consistent and exit codes are documented in
exactly one place (:data:`EXIT_CODES`).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from typing import Any

from rich.console import Console

from easycat._console import color_enabled, feedback_console

SCHEMA_VERSION = 1


# Map of exit code → (short-name, description).  ``easycat explain
# exit-codes`` renders this directly.
EXIT_CODES: dict[int, tuple[str, str]] = {
    0: ("ok", "Success"),
    1: ("runtime_error", "Runtime error"),
    2: ("bad_usage", "Bad usage (unknown flag, missing argument)"),
    3: ("missing_credentials", "Missing credentials"),
    4: ("missing_extra", "Missing optional extra, or bad --config JSON"),
    5: ("bundle_corrupt", "Bundle missing or corrupt"),
    6: ("regression", "Regression detected (replay --fail-on-regression)"),
    101: ("target_exists", "Target directory exists (init without --force)"),
    130: ("sigint_hard_exit", "SIGINT hard exit (second Ctrl-C)"),
}


# Backwards-compatible alias: the color policy now lives in ``easycat._console``
# (the single source of truth shared with the logging handler and runtime
# feedback lines).  Kept under the original name for CLI modules that import it.
_color_enabled = color_enabled


# Primary output console (stdout; what scripts capture).
_stdout_color_enabled = color_enabled()
stdout_console = Console(force_terminal=_stdout_color_enabled, no_color=not _stdout_color_enabled)

# Diagnostic/log console (stderr; never captured for JSON output).
# Shared with runtime feedback so ``NO_COLOR``/``CI`` are honored uniformly.
stderr_console = feedback_console


def info(message: str) -> None:
    """Print an informational line to stderr with a two-space prefix."""
    stderr_console.print(f"  {message}")


def success(message: str) -> None:
    """Print a success line to stderr."""
    stderr_console.print(f"  [green]✓[/] {message}")


def warn(message: str) -> None:
    """Print a warning line to stderr."""
    stderr_console.print(f"  [yellow]![/] {message}")


def error(code: str, message: str) -> None:
    """Print an error line to stderr tagged with its ``EASYCAT_Exxx``."""
    stderr_console.print(f"  [red]✗[/] [red]{code}[/]: {message}")
    stderr_console.print(f"    Run [cyan]easycat explain {_short_code(code)}[/] for details.")


def _short_code(code: str) -> str:
    """Drop the ``EASYCAT_`` prefix for the ``explain`` suggestion."""
    return code.removeprefix("EASYCAT_")


def emit_json(payload: Mapping[str, Any]) -> None:
    """Write a JSON payload to stdout as-is.

    Bypasses Rich rendering on purpose — Rich wraps at terminal width
    which would mangle long JSON lines when consumers pipe the output
    into ``jq`` or another parser.
    """
    text = json.dumps(dict(payload), indent=2, sort_keys=False)
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def json_envelope(command: str, status: str = "ok", **extra: Any) -> dict[str, Any]:
    """Construct the standard ``--json`` envelope."""
    return {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "status": status,
        **extra,
    }
