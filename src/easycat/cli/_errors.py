"""Bridge ``EasyCatError`` → CLI exit code + formatted output.

Every CLI command is wrapped with :func:`cli_command`, which catches
any ``EasyCatError`` raised during the command body and converts it
to a ``typer.Exit`` with the mapped exit code.  That makes the
behavior consistent across two invocation paths:

* ``main()`` → ``app()`` → Typer translates ``typer.Exit`` to
  ``sys.exit(code)``.
* ``typer.testing.CliRunner().invoke(app, [...])`` → the runner
  captures ``typer.Exit`` and exposes its code as
  ``result.exit_code``.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Callable
from typing import Any, TypeVar

import typer

from easycat.cli._output import emit_json, error, json_envelope, stderr_console
from easycat.errors import EasyCatError

# ``EASYCAT_Exxx`` → CLI exit code.  Unlisted codes default to 1.
_CODE_TO_EXIT: dict[str, int] = {
    "EASYCAT_E101": 101,
    "EASYCAT_E102": 4,
    "EASYCAT_E103": 2,
    "EASYCAT_E104": 2,
    "EASYCAT_E201": 1,
    "EASYCAT_E202": 4,
    "EASYCAT_E203": 3,
    "EASYCAT_E204": 1,
    "EASYCAT_E205": 4,
    "EASYCAT_E206": 1,
    "EASYCAT_E207": 1,
    "EASYCAT_E208": 1,
    "EASYCAT_E501": 2,
}


def exit_code_for(code: str) -> int:
    """Return the mapped CLI exit code for an ``EASYCAT_Exxx``."""
    return _CODE_TO_EXIT.get(code, 1)


def handle_easycat_error(
    err: EasyCatError, *, json_mode: bool = False, command: str = "unknown"
) -> int:
    """Render ``err`` and return the CLI exit code.

    Respects ``EASYCAT_DEBUG=1`` by printing the full Rich traceback
    after the formatted error; otherwise only the headline is shown.
    """
    code_exit = exit_code_for(err.code)
    if json_mode:
        emit_json(
            json_envelope(
                command,
                status="error",
                code=err.code,
                message=err.message,
                context=err.context,
                exit_code=code_exit,
            )
        )
    else:
        error(err.code, err.message)
    if os.getenv("EASYCAT_DEBUG") == "1":
        stderr_console.print_exception(show_locals=False)
    return code_exit


F = TypeVar("F", bound=Callable[..., Any])


def cli_command(fn: F) -> F:
    """Wrap a CLI command so ``EasyCatError`` renders and exits cleanly.

    Converts any :class:`~easycat.errors.EasyCatError` raised inside
    the wrapped command into a :class:`typer.Exit` with the mapped
    exit code, after rendering the error (human or JSON based on the
    ``json_output`` kwarg).
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except EasyCatError as err:
            json_mode = bool(kwargs.get("json_output"))
            code = handle_easycat_error(err, json_mode=json_mode, command=fn.__name__)
            raise typer.Exit(code) from None

    return wrapper  # type: ignore[return-value]
