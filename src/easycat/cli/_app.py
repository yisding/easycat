"""Typer application construction and top-level ``main`` entry point.

Commands are grouped into *Scaffold* and *Debug with the journal* for
a journey-ordered ``--help``.  Typer does not offer first-class command
grouping, so we render our own menu on the bare ``easycat`` invocation
via a no-argument callback.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version

import typer

from easycat.cli._errors import handle_easycat_error
from easycat.cli._output import emit_json, json_envelope, stderr_console, stdout_console
from easycat.errors import EasyCatError


def _easycat_version() -> str:
    try:
        return version("easycat")
    except PackageNotFoundError:
        return "unknown"


app = typer.Typer(
    name="easycat",
    help="EasyCat — voice bot framework.",
    no_args_is_help=False,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


_JOURNEY_MENU = """[bold]EasyCat[/] — voice bot framework

  [cyan]Scaffold[/]
    [green]init[/]        Scaffold a new project from a template
    [green]doctor[/]      Check environment and provider reachability
    [green]explain[/]     Look up an error code (like `cargo --explain`)

  [cyan]Debug with the journal[/]
    [green]bundles[/]     List captured debug bundles and crash dumps
    [green]inspect[/]     Summarise a debug bundle or SQLite journal
    [green]replay[/]      Replay a debug bundle or SQLite journal

  [cyan]Validation[/]
    [green]validate[/]    Run validation checks and inspect reports

  [cyan]Learn[/]
    [green]docs[/]        Show documentation entry points

Run [cyan]easycat <command> --help[/] for command-specific options.
Run [cyan]easycat explain <code>[/] to understand an error.
"""


_DOCS_MENU = """[bold]EasyCat documentation[/]

  [cyan]Quickstart[/]       README.md#install
  [cyan]Docs map[/]         docs/README.md
  [cyan]Teaching ladder[/]  docs/teaching/
  [cyan]Examples[/]         examples/README.md
  [cyan]Public API[/]       docs/public-api.md
  [cyan]Contributing[/]     CONTRIBUTING.md
  [cyan]Validation[/]       README.md#validation-workflow

Online source: [cyan]https://github.com/yisding/easycat[/]
"""


_DOCS_LINKS = [
    {"label": "Quickstart", "path": "README.md#install"},
    {"label": "Docs map", "path": "docs/README.md"},
    {"label": "Teaching ladder", "path": "docs/teaching/"},
    {"label": "Examples", "path": "examples/README.md"},
    {"label": "Public API", "path": "docs/public-api.md"},
    {"label": "Contributing", "path": "CONTRIBUTING.md"},
    {"label": "Validation", "path": "README.md#validation-workflow"},
]
_DOCS_SOURCE_URL = "https://github.com/yisding/easycat"


def _print_journey_menu() -> None:
    """Render the top-level menu on bare ``easycat`` invocation."""
    stdout_console.print(_JOURNEY_MENU)


def docs_command(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Show documentation entry points."""
    if json_output:
        emit_json(json_envelope("docs", entries=_DOCS_LINKS, source_url=_DOCS_SOURCE_URL))
        return
    stdout_console.print(_DOCS_MENU)


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    show_version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Print the EasyCat version and exit.",
        is_eager=True,
    ),
) -> None:
    """Entry callback — handles bare ``easycat`` and ``--version``."""
    if show_version:
        stdout_console.print(f"easycat {_easycat_version()}")
        raise typer.Exit(0)
    if ctx.invoked_subcommand is None:
        _print_journey_menu()


# ── Command registrations ──────────────────────────────────────────
#
# Commands are imported lazily inside ``main()`` so bare ``easycat
# --version`` and ``--help`` stay under the 300ms cold-import budget.


_COMMANDS_REGISTERED = False


def _register_commands() -> None:
    global _COMMANDS_REGISTERED

    if _COMMANDS_REGISTERED:
        return

    from easycat.cli.debug.bundles import bundles_app, inspect_bundle, replay_bundle
    from easycat.cli.diagnose.doctor import doctor as doctor_cmd
    from easycat.cli.diagnose.explain import explain as explain_cmd
    from easycat.cli.scaffold.init import init as init_cmd
    from easycat.cli.validate import validate_app

    app.command(name="init", help="Scaffold a new project from a template.")(init_cmd)
    app.command(name="doctor", help="Check environment and provider reachability.")(doctor_cmd)
    app.command(name="docs", help="Show documentation entry points.")(docs_command)
    app.command(name="explain", help="Look up an error code.")(explain_cmd)
    app.command(name="inspect", help="Inspect a debug bundle or SQLite journal.")(inspect_bundle)
    app.command(name="replay", help="Replay a debug bundle or SQLite journal.")(replay_bundle)
    app.add_typer(bundles_app, name="bundles")
    app.add_typer(validate_app, name="validate")
    _COMMANDS_REGISTERED = True


def main() -> None:
    """CLI entry point registered as ``[project.scripts] easycat``."""
    _register_commands()
    try:
        app()
    except EasyCatError as err:
        exit_code = handle_easycat_error(err)
        sys.exit(exit_code)
    except (SystemExit, typer.Exit):
        raise
    except KeyboardInterrupt:
        stderr_console.print()  # newline after ^C
        sys.exit(130)
