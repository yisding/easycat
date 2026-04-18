"""`easycat` command-line interface.

Entry point for the scripted ``easycat`` command.  Import-time work is
kept deliberately trivial so cold startup stays within the 300ms
budget documented in ``plan/peripheral-cli.md``.  Heavy dependencies
(Typer, Rich, template rendering, journal I/O, HTTP probes) are
imported only inside the command that needs them.

The ``--version`` and ``-V`` flags short-circuit *before* importing
Typer/Rich at all — they're the most-hit cold-start path and paying
~300ms of import for a one-line print is unacceptable.
"""

from __future__ import annotations

import sys


def _print_version_fast() -> None:
    """Print the EasyCat version without touching Typer or Rich.

    Called from :func:`main` when ``sys.argv`` is exactly
    ``[prog, '--version']`` or ``[prog, '-V']``.  The regular Typer
    callback in ``_app.py`` produces identical output; this is a
    fast path, not a different behavior.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        v = version("easycat")
    except PackageNotFoundError:  # pragma: no cover — not installed
        v = "unknown"
    print(f"easycat {v}")


def main() -> None:
    """CLI entry point registered as ``[project.scripts] easycat``."""
    if len(sys.argv) == 2 and sys.argv[1] in ("-V", "--version"):
        _print_version_fast()
        return

    # Everything else goes through the full Typer app.
    from easycat.cli._app import main as _app_main

    _app_main()


__all__ = ["main"]
