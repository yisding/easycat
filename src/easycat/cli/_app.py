"""Typer application construction and top-level ``main`` entry point.

Commands are grouped into *Scaffold* and *Debug with the journal* for
a journey-ordered ``--help``.  Typer does not offer first-class command
grouping, so we render our own menu on the bare ``easycat`` invocation
via a no-argument callback.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from typing import TypedDict

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
Run [cyan]easycat explain <code>[/] for errors.
Run [cyan]easycat explain json-schema[/] for CLI JSON.
"""


class _DocsLink(TypedDict):
    label: str
    path: str
    description: str


class _DocsEntry(_DocsLink):
    url: str


_DOCS_LINKS: list[_DocsLink] = [
    {
        "label": "Quickstart",
        "path": "README.md#install",
        "description": "Install EasyCat and run your first voice agent.",
    },
    {
        "label": "CLI and scaffolds",
        "path": "README.md#cli",
        "description": (
            "Scaffold projects, compare templates with copyable create commands, "
            "and learn CLI JSON envelopes."
        ),
    },
    {
        "label": "Docs map",
        "path": "docs/README.md",
        "description": "Choose the maintained guide for your current task.",
    },
    {
        "label": "Teaching ladder",
        "path": "docs/teaching/",
        "description": "Learn voice pipelines chapter by chapter.",
    },
    {
        "label": "First lesson",
        "path": "docs/teaching/00-hello-audio/",
        "description": "Start with audio chunks before agents or providers.",
    },
    {
        "label": "Examples",
        "path": "examples/README.md",
        "description": "Find runnable local, browser, WebSocket, and telephony apps.",
    },
    {
        "label": "Public API",
        "path": "docs/public-api.md",
        "description": "Review the stable import surface before changing exports.",
    },
    {
        "label": "Contributing",
        "path": "CONTRIBUTING.md",
        "description": "Follow the development loop and validation slices.",
    },
    {
        "label": "Deployment",
        "path": "docs/deployment/docker.md",
        "description": "Package the WebSocket example for container deployment.",
    },
    {
        "label": "Observability",
        "path": "docs/observability.md",
        "description": "Inspect journals, debug bundles, metrics, and traces.",
    },
    {
        "label": "Journal durability",
        "path": "src/easycat/runtime/DURABILITY.md",
        "description": "Understand SQLite journal persistence, recovery, and storage layout.",
    },
    {
        "label": "Validation",
        "path": "README.md#validation-workflow",
        "description": "Pick the right pytest and CLI checks for a change.",
    },
    {
        "label": "Validation reference",
        "path": "plan/validation/reference.md",
        "description": "Read provider and report vocabulary used by validation.",
    },
]
_DOCS_SOURCE_URL = "https://github.com/yisding/easycat"


def _docs_url_for(path: str) -> str:
    """Return the GitHub URL for a docs route path."""
    route, sep, fragment = path.partition("#")
    normalized = route.rstrip("/")
    if route.endswith("/"):
        url = f"{_DOCS_SOURCE_URL}/tree/main/{normalized}"
    else:
        url = f"{_DOCS_SOURCE_URL}/blob/main/{normalized}"
    if sep:
        url = f"{url}#{fragment}"
    return url


def _docs_entries() -> list[_DocsEntry]:
    return [{**entry, "url": _docs_url_for(entry["path"])} for entry in _DOCS_LINKS]


def _format_docs_menu() -> str:
    entries = _docs_entries()
    label_width = max(len(entry["label"]) for entry in entries)
    routes = "\n".join(
        f"  [cyan]{entry['label']}[/]{' ' * (label_width - len(entry['label']) + 2)}"
        f"{entry['path']}\n"
        f"    [dim]{entry['description']}[/]\n"
        f"    [dim]{entry['url']}[/]"
        for entry in entries
    )
    return f"""[bold]EasyCat documentation[/]

{routes}

Online source: {_DOCS_SOURCE_URL}
"""


def _print_journey_menu() -> None:
    """Render the top-level menu on bare ``easycat`` invocation."""
    stdout_console.print(_JOURNEY_MENU)


def docs_command(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Show documentation entry points."""
    if json_output:
        emit_json(json_envelope("docs", entries=_docs_entries(), source_url=_DOCS_SOURCE_URL))
        return
    stdout_console.print(_format_docs_menu(), soft_wrap=True)


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
