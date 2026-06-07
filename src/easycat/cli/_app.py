"""Typer application construction and top-level ``main`` entry point.

Commands are grouped into the Scaffold, Debug with the journal, Validation,
and Docs and guidance sections for a journey-ordered bare ``easycat`` menu.
Typer does not offer first-class command grouping, so we render our own menu
on the bare ``easycat`` invocation via a no-argument callback.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from typing import NamedTuple, NotRequired, TypedDict

import typer
from rich.markup import escape

from easycat.cli._errors import handle_easycat_error
from easycat.cli._output import emit_json, json_envelope, stderr_console, stdout_console
from easycat.errors import EasyCatError


def _easycat_version() -> str:
    try:
        return version("easycat")
    except PackageNotFoundError:
        return "unknown"


_CLI_HINTS: tuple[tuple[str, str], ...] = (
    ("easycat <command> --help", "command-specific options."),
    ("easycat docs", "learning, maintenance, validation, and operations routes."),
    ("easycat docs --json", "machine-readable docs routes, audiences, and command hints."),
    ("easycat explain <code>", "errors."),
    ("easycat explain json-schema", "CLI JSON."),
)


def _plain_cli_hint(command: str, purpose: str) -> str:
    return f"Run {command} for {purpose}"


def _rich_cli_hint(command: str, purpose: str) -> str:
    return f"Run [cyan]{escape(command)}[/] for {purpose}"


_APP_HELP = "EasyCat — voice bot framework.\n\n" + "\n".join(
    _plain_cli_hint(command, purpose) for command, purpose in _CLI_HINTS[1:]
)

app = typer.Typer(
    name="easycat",
    help=_APP_HELP,
    no_args_is_help=False,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


class _CommandText(NamedTuple):
    help: str
    journey: str


_COMMAND_TEXT: dict[str, _CommandText] = {
    "init": _CommandText(
        help="Scaffold a new project from a template.",
        journey="Scaffold a new project from a template",
    ),
    "doctor": _CommandText(
        help="Check API keys, optional extras, and provider reachability.",
        journey="Check API keys, optional extras, and provider reachability",
    ),
    "explain": _CommandText(
        help="Look up errors and CLI schema topics.",
        journey="Look up errors and CLI schema topics",
    ),
    "bundles": _CommandText(
        help="Inspect captured debug bundles and crash dumps.",
        journey="List captured debug bundles and crash dumps",
    ),
    "inspect": _CommandText(
        help="Inspect a debug bundle or SQLite journal.",
        journey="Summarise a debug bundle or SQLite journal",
    ),
    "replay": _CommandText(
        help="Replay a debug bundle or SQLite journal.",
        journey="Replay a debug bundle or SQLite journal",
    ),
    "validate": _CommandText(
        help="Run validation checks and inspect validation reports.",
        journey="Run validation checks and inspect validation reports",
    ),
    "docs": _CommandText(
        help="Show docs for learning, maintenance, validation, and operations.",
        journey="Show docs for learning, maintenance, validation, and operations",
    ),
}

_JOURNEY_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Scaffold", ("init", "doctor", "explain")),
    ("Debug with the journal", ("bundles", "inspect", "replay")),
    ("Validation", ("validate",)),
    ("Docs and guidance", ("docs",)),
)

_JOURNEY_FOOTER = tuple(_rich_cli_hint(command, purpose) for command, purpose in _CLI_HINTS)


def _format_journey_menu() -> str:
    """Render the bare ``easycat`` menu from the command text table."""
    lines = ["[bold]EasyCat[/] — voice bot framework", ""]
    command_width = 12
    for section, command_names in _JOURNEY_SECTIONS:
        lines.append(f"  [cyan]{section}[/]")
        for command_name in command_names:
            text = _COMMAND_TEXT[command_name].journey
            padding = " " * (command_width - len(command_name))
            lines.append(f"    [green]{command_name}[/]{padding}{text}")
        lines.append("")
    lines.extend(_JOURNEY_FOOTER)
    return "\n".join(lines) + "\n"


_JOURNEY_MENU = _format_journey_menu()


class _DocsLink(TypedDict):
    label: str
    path: str
    audience: str
    description: str
    commands: NotRequired[tuple[str, ...]]


class _DocsEntry(_DocsLink):
    url: str


_DOCS_LINKS: list[_DocsLink] = [
    {
        "label": "Start here",
        "path": "README.md#choose-your-path",
        "audience": "all readers",
        "description": (
            "Choose the right first route for quickstart, learning, examples, "
            "maintenance, or operations."
        ),
        "commands": (
            "uv sync --extra quickstart --group dev",
            "uv run easycat doctor",
            "easycat init --list-templates",
            "uv run easycat validate quick",
            "uv run pytest tests/test_install_guidance.py",
        ),
    },
    {
        "label": "Quickstart",
        "path": "README.md#install",
        "audience": "new users",
        "description": "Install EasyCat and run your first voice agent.",
        "commands": (
            "uv sync --extra quickstart --group dev",
            "uv run easycat doctor",
            "uv run easycat doctor --env-file .env",
            "uv run python examples/openai_agents_voice.py",
        ),
    },
    {
        "label": "CLI and scaffolds",
        "path": "README.md#cli",
        "audience": "app builders",
        "description": (
            "Scaffold projects, compare templates with base package requirements, "
            "extras, env requirements, optional env knobs, generated files, and "
            "copyable create/preflight/check/docs/run commands, and learn CLI JSON envelopes."
        ),
        "commands": (
            "easycat init --list-templates",
            "easycat init --list-templates --json",
            "easycat init my-agent",
            "easycat doctor --json",
            "easycat doctor --env-file .env --json",
            "easycat explain json-schema",
        ),
    },
    {
        "label": "Docs map",
        "path": "docs/README.md",
        "audience": "all readers",
        "description": "Choose the maintained guide for your current task.",
        "commands": ("easycat docs", "easycat docs --json"),
    },
    {
        "label": "Teaching ladder",
        "path": "docs/teaching/",
        "audience": "learners",
        "description": "Learn voice pipelines chapter by chapter.",
        "commands": (
            "uv sync --extra quickstart --group dev",
            "uv run easycat doctor",
            "uv run python docs/teaching/00-hello-audio/main.py",
        ),
    },
    {
        "label": "First lesson",
        "path": "docs/teaching/00-hello-audio/",
        "audience": "learners",
        "description": "Start with audio chunks before agents or providers.",
        "commands": ("uv run python docs/teaching/00-hello-audio/main.py",),
    },
    {
        "label": "Examples",
        "path": "examples/README.md",
        "audience": "app builders",
        "description": "Find runnable local, browser, WebSocket, and telephony apps.",
        "commands": (
            "uv run easycat doctor",
            "uv run easycat doctor --env-file .env",
            "uv run python examples/openai_agents_voice.py",
            "uv run easycat validate quick",
            "uv run easycat validate report .easycat/validation/latest.json",
            "uv run easycat validate report .easycat/validation/latest.json --json",
        ),
    },
    {
        "label": "Architecture",
        "path": "CLAUDE.md",
        "audience": "maintainers",
        "description": (
            "Orient to the pipeline, packages, provider registries, lifecycle, "
            "and docs/onboarding guards."
        ),
        "commands": (
            "uv run easycat docs",
            "uv run easycat docs --json",
            "uv run pytest tests/test_install_guidance.py",
            "just guard-docs",
            "just guard-examples",
            "just guard-templates",
            "just guard-contributing",
            "just guard-markdown",
        ),
    },
    {
        "label": "Coding agents",
        "path": "AGENTS.md",
        "audience": "coding agents",
        "description": (
            "Follow repo structure, development commands, docs/onboarding guards, "
            "and PR expectations."
        ),
        "commands": (
            "uv run easycat docs",
            "uv run easycat docs --json",
            "just guard-docs",
            "just guard-examples",
            "just guard-templates",
            "just guard-contributing",
            "just guard-markdown",
            "uv run easycat validate quick",
            "uv run easycat validate report .easycat/validation/latest.json",
        ),
    },
    {
        "label": "Public API",
        "path": "docs/public-api.md",
        "audience": "maintainers",
        "description": "Review the stable import surface before changing exports.",
        "commands": ("uv run pytest tests/test_public_api.py",),
    },
    {
        "label": "Contributing",
        "path": "CONTRIBUTING.md",
        "audience": "contributors",
        "description": (
            "Follow the development loop, docs/onboarding guards, and validation slices."
        ),
        "commands": (
            "just guard-docs",
            "just guard-examples",
            "just guard-templates",
            "just guard-contributing",
            "just guard-markdown",
            "uv run pytest",
            "uv run ruff check .",
            "uv run easycat validate quick",
            "uv run easycat validate report .easycat/validation/latest.json",
            "uv run easycat validate report .easycat/validation/latest.json --json",
        ),
    },
    {
        "label": "Deployment",
        "path": "docs/deployment/docker.md",
        "audience": "operators",
        "description": "Package the WebSocket example for container deployment.",
        "commands": (
            "docker compose -f docker/compose.yaml up --build",
            "docker compose -f docker/compose.yaml down",
        ),
    },
    {
        "label": "Observability",
        "path": "docs/observability.md",
        "audience": "operators",
        "description": "Inspect journals, debug bundles, metrics, and traces.",
        "commands": (
            "easycat bundles list",
            "easycat bundles list --json",
            "easycat bundles show PATH",
            "easycat bundles show PATH --json",
            "easycat inspect PATH",
            "easycat inspect PATH --json",
            "easycat replay PATH",
            "easycat replay PATH --json",
            "easycat bundles export PATH",
            "easycat bundles export PATH --output DIR --json",
        ),
    },
    {
        "label": "Journal durability",
        "path": "src/easycat/runtime/DURABILITY.md",
        "audience": "operators and maintainers",
        "description": "Understand SQLite journal persistence, recovery, and storage layout.",
        "commands": ("uv run pytest tests/runtime/test_sqlite_journal.py",),
    },
    {
        "label": "Validation",
        "path": "README.md#validation-workflow",
        "audience": "contributors",
        "description": (
            "Run docs/onboarding guards, the right validation lane, and inspect "
            ".easycat/validation/latest.json."
        ),
        "commands": (
            "just guard-docs",
            "just guard-examples",
            "just guard-templates",
            "just guard-contributing",
            "just guard-markdown",
            "uv run easycat validate quick",
            "uv run easycat validate report .easycat/validation/latest.json",
            "uv run easycat validate report .easycat/validation/latest.json --json",
        ),
    },
    {
        "label": "Validation reference",
        "path": "plan/validation/reference.md",
        "audience": "release maintainers",
        "description": "Read provider and report vocabulary used by validation.",
        "commands": (
            "easycat validate quick --json",
            "easycat validate report .easycat/validation/latest.json --json",
        ),
    },
]
_DOCS_SOURCE_URL = "https://github.com/yisding/easycat"
_DOCS_COMMAND_NOTE = (
    "Bare easycat commands use installed CLI form; from this repo, prefix them with uv run. "
    "Commands already starting with uv run are repo-local and should run from the repository "
    "root. just commands are repo-local shortcuts; install just or use the raw command table "
    "in CONTRIBUTING.md. Replace uppercase placeholders such as PATH and DIR before running."
)


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


def _format_docs_entry(entry: _DocsEntry, *, label_width: int) -> str:
    commands = entry.get("commands")
    command_line = ""
    if commands:
        if len(commands) == 1:
            command_line = f"    [dim]Commands: {escape(commands[0])}[/]\n"
        else:
            command_items = "\n".join(f"      [dim]{escape(command)}[/]" for command in commands)
            command_line = f"    [dim]Commands:[/]\n{command_items}\n"
    return (
        f"  [cyan]{escape(entry['label'])}[/]{' ' * (label_width - len(entry['label']) + 2)}"
        f"{escape(entry['path'])}\n"
        f"    [dim]For: {escape(entry['audience'])}[/]\n"
        f"    [dim]{escape(entry['description'])}[/]\n"
        f"{command_line}"
        f"    [dim]{escape(entry['url'])}[/]"
    )


def _format_docs_menu() -> str:
    entries = _docs_entries()
    label_width = max(len(entry["label"]) for entry in entries)
    routes = "\n".join(_format_docs_entry(entry, label_width=label_width) for entry in entries)
    return f"""[bold]EasyCat documentation[/]

{routes}

Online source: {_DOCS_SOURCE_URL}
Machine-readable routes, audiences, and command hints: easycat docs --json
{_DOCS_COMMAND_NOTE}
"""


def _print_journey_menu() -> None:
    """Render the top-level menu on bare ``easycat`` invocation."""
    stdout_console.print(_JOURNEY_MENU)


def docs_command(
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit the machine-readable docs route map with audiences and command hints.",
    ),
) -> None:
    """Show docs for learning, maintenance, validation, and operations."""
    if json_output:
        emit_json(
            json_envelope(
                "docs",
                entries=_docs_entries(),
                source_url=_DOCS_SOURCE_URL,
                command_note=_DOCS_COMMAND_NOTE,
            )
        )
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

    app.command(name="init", help=_COMMAND_TEXT["init"].help)(init_cmd)
    app.command(name="doctor", help=_COMMAND_TEXT["doctor"].help)(doctor_cmd)
    app.command(name="docs", help=_COMMAND_TEXT["docs"].help)(docs_command)
    app.command(name="explain", help=_COMMAND_TEXT["explain"].help)(explain_cmd)
    app.command(name="inspect", help=_COMMAND_TEXT["inspect"].help)(inspect_bundle)
    app.command(name="replay", help=_COMMAND_TEXT["replay"].help)(replay_bundle)
    app.add_typer(bundles_app, name="bundles", help=_COMMAND_TEXT["bundles"].help)
    app.add_typer(validate_app, name="validate", help=_COMMAND_TEXT["validate"].help)
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
