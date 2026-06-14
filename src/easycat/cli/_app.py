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
from easycat.cli._guard_commands import (
    DOCS_ONBOARDING_GUARD_COMMANDS as _DOCS_ONBOARDING_GUARD_COMMANDS,
)
from easycat.cli._guard_commands import (
    DOCS_ONBOARDING_RAW_GUARD_COMMANDS as _DOCS_ONBOARDING_RAW_GUARD_COMMANDS,
)
from easycat.cli._output import (
    emit_command_error,
    emit_json,
    json_envelope,
    stderr_console,
    stdout_console,
)
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
    "console": _CommandText(
        help="Try EasyCat in your terminal — no API keys required.",
        journey="Try EasyCat in your terminal with no API keys",
    ),
    "init": _CommandText(
        help="Scaffold a new project from a template.",
        journey="Scaffold a new project from a template",
    ),
    "doctor": _CommandText(
        help="Check API keys, optional extras, and provider reachability.",
        journey="Check API keys, optional extras, and provider reachability",
    ),
    "serve": _CommandText(
        help="Serve the browser voice playground on localhost.",
        journey="Serve the browser voice playground on localhost",
    ),
    "explain": _CommandText(
        help="Look up errors and CLI schema topics.",
        journey="Route a call problem by symptom, or look up an error code",
    ),
    "bundles": _CommandText(
        help="Inspect captured debug bundles and crash dumps.",
        journey="List captured debug bundles and crash dumps",
    ),
    "debugger": _CommandText(
        help="Open the browser debugging UI for bundles and journals.",
        journey="Open the browser debugger for a captured call",
    ),
    "inspect": _CommandText(
        help="Inspect a debug bundle or SQLite journal.",
        journey="Summarise a debug bundle or SQLite journal",
    ),
    "replay": _CommandText(
        help="Replay a debug bundle or SQLite journal.",
        journey="Replay a debug bundle or SQLite journal",
    ),
    "latency": _CommandText(
        help="Summarise critical-path latency percentiles for a bundle.",
        journey="Summarise critical-path latency percentiles for a bundle",
    ),
    "diff": _CommandText(
        help="Diff two bundles turn-by-turn: milestone, transcript, and cost deltas.",
        journey="Diff two bundles turn-by-turn for milestone and cost regressions",
    ),
    "journal": _CommandText(
        help="Search and tail captured journals and crash dumps.",
        journey="Search and tail captured journals and crash dumps",
    ),
    "tail": _CommandText(
        help="Live-tail a SQLite journal as it grows.",
        journey="Live-tail a SQLite journal as it grows",
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
    ("Scaffold", ("console", "init", "doctor", "serve")),
    (
        "Debug with the journal",
        (
            "bundles",
            "debugger",
            "inspect",
            "replay",
            "latency",
            "diff",
            "journal",
            "tail",
            "explain",
        ),
    ),
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
    diataxis: str
    description: str
    commands: NotRequired[tuple[str, ...]]


class _DocsEntry(_DocsLink):
    url: str


_DOCS_LINKS: list[_DocsLink] = [
    {
        "label": "Start here",
        "path": "README.md#choose-your-path",
        "audience": "all readers",
        "diataxis": "how-to",
        "description": (
            "Choose the right first route for quickstart, learning, examples, "
            "maintenance, or operations."
        ),
        "commands": (
            "uv sync --extra quickstart --group dev",
            "uv run easycat doctor",
            "uv run easycat doctor --json",
            "uv run easycat doctor --env-file .env",
            "uv run easycat doctor --env-file .env --json",
            "uv run --env-file .env python examples/openai_agents_voice.py",
            "uv run easycat console",
            "uv run python examples/journal_demo.py",
            "uv run easycat init --list-templates",
            "uv run easycat init my-agent",
            "uv run easycat docs --audience maintainers",
            "uv run easycat docs --audience coding-agents",
            "uv run easycat validate quick",
            "easycat bundles list",
            "uv sync --extra debugger --group dev",
        ),
    },
    {
        "label": "Quickstart",
        "path": "README.md#install",
        "audience": "new users",
        "diataxis": "tutorial",
        "description": "Install EasyCat and run your first voice agent.",
        "commands": (
            "uv sync --extra quickstart --group dev",
            "uv run easycat doctor",
            "uv run easycat doctor --json",
            "uv run easycat doctor --env-file .env",
            "uv run easycat doctor --env-file .env --json",
            "uv run python examples/openai_agents_voice.py",
            "uv run --env-file .env python examples/openai_agents_voice.py",
        ),
    },
    {
        "label": "CLI and scaffolds",
        "path": "README.md#cli",
        "audience": "app builders",
        "diataxis": "how-to",
        "description": (
            "Scaffold projects, compare templates with base package requirements, "
            "extras, env requirements, optional env knobs, generated files, and "
            "copyable create/preflight/check/fix/docs/json-schema/run commands, "
            "and learn CLI JSON envelopes."
        ),
        "commands": (
            "easycat console",
            "easycat console --voice-demo",
            "easycat init --list-templates",
            "easycat init --list-templates --json",
            "easycat init my-agent",
            "easycat doctor --json",
            "easycat doctor --env-file .env --json",
            "easycat docs",
            "easycat docs --audience learners",
            "easycat docs --audience learners --json",
            "easycat docs --audience app-builders",
            "easycat docs --audience app-builders --json",
            "easycat docs --audience operators",
            "easycat docs --audience operators --json",
            "easycat docs --audience maintainers",
            "easycat docs --audience maintainers --json",
            "easycat docs --json",
            "easycat explain json-schema",
        ),
    },
    {
        "label": "Docs map",
        "path": "docs/README.md",
        "audience": "all readers",
        "diataxis": "reference",
        "description": "Choose the maintained guide for your current task.",
        "commands": (
            "easycat docs",
            "easycat docs --audience learners",
            "easycat docs --audience app-builders",
            "easycat docs --audience operators",
            "easycat docs --audience maintainers",
            "easycat docs --json",
        ),
    },
    {
        "label": "Teaching ladder",
        "path": "docs/teaching/",
        "audience": "learners",
        "diataxis": "tutorial",
        "description": "Learn voice pipelines chapter by chapter.",
        "commands": (
            "uv sync --extra local --group dev",
            "uv sync --extra quickstart --group dev",
            "uv run easycat doctor",
            "uv run easycat doctor --json",
            "uv run easycat doctor --env-file .env",
            "uv run easycat doctor --env-file .env --json",
            "uv run easycat docs --audience learners",
            "uv run easycat docs --audience learners --json",
            "uv run python docs/teaching/00-hello-audio/main.py",
            "uv run easycat validate quick",
            "uv run easycat validate quick --json",
            "uv run easycat validate report .easycat/validation/latest.json",
            "uv run easycat validate report .easycat/validation/latest.json --json",
        ),
    },
    {
        "label": "First lesson",
        "path": "docs/teaching/00-hello-audio/",
        "audience": "learners",
        "diataxis": "tutorial",
        "description": "Start with audio chunks before agents or providers.",
        "commands": (
            "uv sync --extra local --group dev",
            "uv run python docs/teaching/00-hello-audio/main.py",
        ),
    },
    {
        "label": "Examples",
        "path": "examples/README.md",
        "audience": "app builders",
        "diataxis": "how-to",
        "description": "Find runnable local, browser, WebSocket, and telephony apps.",
        "commands": (
            "uv run easycat init --list-templates",
            "uv run easycat init my-agent",
            "uv run easycat init --list-templates --json",
            "uv run easycat doctor",
            "uv run easycat doctor --json",
            "uv run easycat doctor --env-file .env",
            "uv run easycat doctor --env-file .env --json",
            "uv run python examples/journal_demo.py",
            "uv run python examples/telephony_helpers.py",
            "uv run python examples/openai_agents_voice.py",
            "uv run --env-file .env python examples/openai_agents_voice.py",
            "uv run easycat validate quick",
            "uv run easycat validate quick --json",
            "uv run easycat validate report .easycat/validation/latest.json",
            "uv run easycat validate report .easycat/validation/latest.json --json",
        ),
    },
    {
        "label": "Architecture",
        "path": "docs/architecture.md",
        "audience": "maintainers",
        "diataxis": "explanation",
        "description": (
            "Understand the pipeline, session collaborators, stages, providers, and agent bridges."
        ),
        "commands": (
            "uv run easycat docs --audience maintainers",
            "uv run easycat docs --audience maintainers --json",
        ),
    },
    {
        "label": "Maintainer guide",
        "path": "CLAUDE.md",
        "audience": "maintainers",
        "diataxis": "how-to",
        "description": (
            "Orient to the pipeline, packages, provider registries, lifecycle, "
            "and docs/onboarding guards."
        ),
        "commands": (
            "uv run easycat docs",
            "uv run easycat docs --audience maintainers",
            "uv run easycat docs --audience maintainers --json",
            "uv run easycat docs --json",
            "uv run easycat doctor --json",
            "uv run easycat doctor --env-file .env --json",
            "uv run easycat explain json-schema",
            "uv run easycat bundles show PATH --json",
            "uv run easycat bundles export PATH --output DIR --json",
            "uv run easycat replay PATH --json",
            "uv run pytest tests/install/test_install_guidance.py",
            *_DOCS_ONBOARDING_GUARD_COMMANDS,
            *_DOCS_ONBOARDING_RAW_GUARD_COMMANDS,
            "uv run easycat validate quick",
            "uv run easycat validate quick --json",
            "uv run easycat validate contracts --json",
            "uv run easycat validate release --json",
            "uv run easycat validate report .easycat/validation/latest.json",
            "uv run easycat validate report .easycat/validation/latest.json --json",
        ),
    },
    {
        "label": "Coding agents",
        "path": "AGENTS.md",
        "audience": "coding agents",
        "diataxis": "how-to",
        "description": (
            "Follow repo structure, development commands, docs/onboarding guards, "
            "and PR expectations."
        ),
        "commands": (
            "uv run easycat docs",
            "uv run easycat docs --audience coding-agents",
            "uv run easycat docs --audience coding-agents --json",
            "uv run easycat docs --json",
            "uv run easycat doctor --json",
            "uv run easycat doctor --env-file .env --json",
            "uv run easycat explain json-schema",
            "uv run easycat bundles show PATH --json",
            "uv run easycat bundles export PATH --output DIR --json",
            "uv run easycat replay PATH --json",
            *_DOCS_ONBOARDING_GUARD_COMMANDS,
            *_DOCS_ONBOARDING_RAW_GUARD_COMMANDS,
            "uv run easycat validate quick",
            "uv run easycat validate quick --json",
            "uv run easycat validate contracts --json",
            "uv run easycat validate release --json",
            "uv run easycat validate report .easycat/validation/latest.json",
            "uv run easycat validate report .easycat/validation/latest.json --json",
        ),
    },
    {
        "label": "Session graduation",
        "path": "docs/from-easyconfig-to-session.md",
        "audience": "app builders",
        "diataxis": "how-to",
        "description": (
            "Graduate from the EasyConfig quickstart to the production Session "
            "API: lifecycle, events, text turns, session actions, and replayable "
            "debug bundles."
        ),
        "commands": (
            "uv run easycat docs --audience app-builders",
            "uv run easycat docs --audience app-builders --json",
            "uv run easycat replay PATH",
            "uv run easycat replay PATH --json",
            "uv run easycat inspect .easycat/journals/<session_id>.sqlite",
        ),
    },
    {
        "label": "Testing and evals",
        "path": "docs/testing-and-evals.md",
        "audience": "app builders",
        "diataxis": "how-to",
        "description": (
            "Climb the eval ladder: bundle fixtures, offline text turns, "
            "latency budgets and LLM-as-judge, then live audio validation."
        ),
        "commands": (
            "uv run pytest tests/debug/test_testing_helpers.py",
            "uv run python docs/teaching/12-evals-and-latency/llm_judge.py "
            "docs/teaching/12-evals-and-latency/bundles/turn_01_fast.bundle",
            "uv run easycat validate latency --smoke",
            "uv run easycat validate live --provider openai",
        ),
    },
    {
        "label": "Events reference",
        "path": "docs/reference/events.md",
        "audience": "app builders",
        "diataxis": "reference",
        "description": ("Look up every public session event type and when it is emitted."),
        "commands": (
            "uv run easycat explain events",
            "uv run easycat docs --audience app-builders",
        ),
    },
    {
        "label": "EasyConfig reference",
        "path": "docs/reference/easyconfig.md",
        "audience": "app builders",
        "diataxis": "reference",
        "description": (
            "Look up every EasyConfig field, grouped config object, and legacy alias."
        ),
        "commands": ("uv run easycat docs --audience app-builders",),
    },
    {
        "label": "Session lifecycle",
        "path": "docs/reference/session-lifecycle.md",
        "audience": "app builders",
        "diataxis": "reference",
        "description": ("Start, stop, force-stop, and read the journal after teardown."),
        "commands": ("uv run easycat explain journal",),
    },
    {
        "label": "Browser playground",
        "path": "docs/browser-playground.md",
        "audience": "app builders",
        "diataxis": "how-to",
        "description": (
            "Talk to a bot in the browser with one command, and read the "
            "WebSocket/WebRTC wire protocol behind the playground page."
        ),
        "commands": (
            "uv sync --extra quickstart --extra webrtc --group dev",
            "uv run easycat doctor",
            "uv run easycat doctor --json",
            "uv run easycat serve",
            "uv run python examples/webrtc_server.py",
            "uv run pytest tests/transports/test_webrtc_auth_browser_playground.py",
        ),
    },
    {
        "label": "Public API",
        "path": "docs/public-api.md",
        "audience": "maintainers",
        "diataxis": "reference",
        "description": "Review the stable import surface before changing exports.",
        "commands": (
            "uv run easycat docs",
            "uv run easycat docs --audience maintainers",
            "uv run easycat docs --json",
            "uv run easycat docs --audience maintainers --json",
            "uv run easycat explain json-schema",
            "uv run pytest tests/test_public_api.py",
            "just guard-docs",
            _DOCS_ONBOARDING_RAW_GUARD_COMMANDS[0],
        ),
    },
    {
        "label": "Provider contracts",
        "path": "tests/contracts/README.md",
        "audience": "provider maintainers",
        "diataxis": "how-to",
        "description": (
            "Maintain offline provider, protocol, cassette, and bridge contract coverage."
        ),
        "commands": (
            "uv run easycat docs --audience provider-maintainers",
            "uv run easycat docs --audience provider-maintainers --json",
            "uv run easycat validate contracts",
            "uv run easycat validate contracts --json",
            "uv run pytest tests/contracts",
            "uv run pytest tests/contracts/test_provider_session_matrix.py",
        ),
    },
    {
        "label": "Extending providers",
        "path": "docs/extending/",
        "audience": "provider maintainers",
        "diataxis": "how-to",
        "description": (
            "Build custom STT, TTS, VAD, transport, and agent-bridge providers "
            "out of tree and verify conformance."
        ),
        "commands": (
            "uv run easycat docs --audience provider-maintainers",
            "uv run easycat docs --audience provider-maintainers --json",
            "uv run easycat init my-provider --template provider",
            "uv run python examples/custom_transport.py",
            "uv run pytest tests/test_public_api.py",
            "uv run pytest tests/contracts",
        ),
    },
    {
        "label": "Contributing",
        "path": "CONTRIBUTING.md",
        "audience": "contributors",
        "diataxis": "how-to",
        "description": (
            "Follow the development loop, docs/onboarding guards, and validation slices."
        ),
        "commands": (
            *_DOCS_ONBOARDING_GUARD_COMMANDS,
            *_DOCS_ONBOARDING_RAW_GUARD_COMMANDS,
            "uv run easycat docs --audience contributors",
            "uv run easycat docs --audience contributors --json",
            "uv run pytest",
            "uv run ruff check .",
            "uv run easycat validate quick",
            "uv run easycat validate socket",
            "uv run easycat validate stress",
            "uv run easycat validate contracts",
            "uv run easycat validate latency --smoke",
            "uv run easycat validate live --provider openai",
            "uv run easycat validate release",
            "uv run easycat validate report .easycat/validation/latest.json",
            "uv run easycat validate quick --json",
            "uv run easycat validate contracts --json",
            "uv run easycat validate release --json",
            "uv run easycat validate report .easycat/validation/latest.json --json",
        ),
    },
    {
        "label": "Deployment",
        "path": "docs/deployment/docker.md",
        "audience": "operators",
        "diataxis": "how-to",
        "description": "Package the WebSocket example for container deployment.",
        "commands": (
            "uv run easycat docs --audience operators",
            "uv run easycat docs --audience operators --json",
            "docker compose -f docker/compose.yaml up --build",
            "python -m http.server 8080 --directory examples",
            "docker compose --env-file docker/.env -f docker/compose.yaml up --build",
            "docker compose -f docker/compose.yaml down",
        ),
    },
    {
        "label": "Production servers",
        "path": "docs/deployment/production-servers.md",
        "audience": "operators",
        "diataxis": "how-to",
        "description": (
            "Run multi-client WebSocket, WebRTC, WebTransport, and Twilio servers "
            "with one isolated EasyCat session per client or call."
        ),
        "commands": (
            "uv run easycat docs --audience operators",
            "uv run easycat docs --audience operators --json",
            "uv run python examples/ws_server.py",
            "uv run python examples/webrtc_server.py",
            "uv run python examples/webtransport_server.py",
            "uv run uvicorn examples.twilio_app:create_app --factory --host 0.0.0.0",
            "uv run pytest tests/transports/test_websocket_session_server.py",
            "uv run pytest tests/transports/test_webrtc_config.py",
            "uv run pytest tests/transports/test_webrtc_lifecycle_server.py",
            "uv run pytest tests/transports/test_webtransport_session.py",
        ),
    },
    {
        "label": "Observability",
        "path": "docs/observability.md",
        "audience": "operators",
        "diataxis": "how-to",
        "description": "Inspect journals, debug bundles, the debugger UI, metrics, and traces.",
        "commands": (
            "uv run easycat docs --audience operators",
            "uv run easycat docs --audience operators --json",
            "easycat bundles list",
            "easycat bundles list --json",
            "easycat bundles show PATH",
            "easycat bundles show PATH --json",
            "easycat debugger serve PATH",
            "easycat debugger serve PATH --no-open-browser",
            "easycat inspect PATH",
            "easycat inspect PATH --json",
            "easycat replay PATH",
            "easycat replay PATH --json",
            "easycat latency PATH",
            "easycat latency PATH --json",
            "easycat diff PATH PATH",
            "easycat journal grep PATH --query TEXT",
            "easycat journal follow PATH",
            "easycat journal promote PATH TURN_ID --out FILE",
            "easycat tail PATH",
            "easycat bundles export PATH",
            "easycat bundles export PATH --output DIR --json",
            "uv sync --extra debugger --group dev",
        ),
    },
    {
        "label": "Latency",
        "path": "docs/latency.md",
        "audience": "operators",
        "diataxis": "how-to",
        "description": (
            'Answer "why was that turn slow?" with per-turn CLI waterfalls and the '
            "table of latency-adding defaults."
        ),
        "commands": (
            "uv run easycat docs --audience operators",
            "easycat bundles show PATH --json",
            "easycat inspect PATH --json",
            "easycat latency PATH",
            "easycat latency PATH --json",
            "uv run easycat validate latency --smoke",
        ),
    },
    {
        "label": "Journal durability",
        "path": "src/easycat/runtime/DURABILITY.md",
        "audience": "operators and maintainers",
        "diataxis": "explanation",
        "description": "Understand SQLite journal persistence, recovery, and storage layout.",
        "commands": (
            "uv run easycat docs --audience operators-and-maintainers",
            "uv run easycat docs --audience operators-and-maintainers --json",
            "uv run pytest tests/runtime/test_sqlite_journal.py",
            "uv run easycat inspect .easycat/journals/<session_id>.sqlite",
            "uv run easycat inspect .easycat/journals/<session_id>.sqlite --json",
            "uv run easycat inspect .easycat/crash-dumps/<session_id>.sqlite --json",
        ),
    },
    {
        "label": "Validation",
        "path": "docs/validation.md",
        "audience": "contributors",
        "diataxis": "how-to",
        "description": (
            "Run docs/onboarding guards, the right validation lane, and inspect "
            ".easycat/validation/latest.json."
        ),
        "commands": (
            *_DOCS_ONBOARDING_GUARD_COMMANDS,
            *_DOCS_ONBOARDING_RAW_GUARD_COMMANDS,
            "uv run easycat validate quick",
            "uv run easycat validate socket",
            "uv run easycat validate stress",
            "uv run easycat validate contracts",
            "uv run easycat validate latency --smoke",
            "uv run easycat validate live",
            "uv run easycat validate release",
            "uv run easycat validate report .easycat/validation/latest.json",
            "uv run easycat validate quick --json",
            "uv run easycat validate contracts --json",
            "uv run easycat validate release --json",
            "uv run easycat validate report .easycat/validation/latest.json --json",
        ),
    },
    {
        "label": "Validation reference",
        "path": "plan/validation/reference.md",
        "audience": "release maintainers",
        "diataxis": "reference",
        "description": "Read provider and report vocabulary used by validation.",
        "commands": (
            "easycat docs --audience release-maintainers --json",
            "easycat validate quick --json",
            "easycat validate contracts --json",
            "easycat validate release --json",
            "easycat validate report .easycat/validation/latest.json --json",
        ),
    },
]
_DOCS_SOURCE_URL = "https://github.com/yisding/easycat"
_DOCS_COMMAND_NOTE = (
    "Bare easycat commands use installed CLI form; from this repo, prefix them with uv run. "
    "Commands already starting with uv run are repo-local and should run from the repository "
    "root. just commands are repo-local shortcuts; install just or use the raw command table "
    "in CONTRIBUTING.md. Replace uppercase or angle-bracket placeholders such as PATH, DIR, "
    "TEXT, TURN_ID, FILE, and <session_id> before running."
)
_DOCS_AUDIENCE_ALIAS_NOTE = (
    "Multi-word audiences also accept hyphens or underscores, such as app-builders. "
    "The maintainers and operators filters also include compound labels such as "
    "provider maintainers, release maintainers, and operators and maintainers."
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


def _normalize_docs_audience(value: str) -> str:
    """Normalize an audience filter or label for forgiving CLI matching."""
    return " ".join(value.casefold().replace("-", " ").replace("_", " ").split())


def _available_docs_audiences() -> tuple[str, ...]:
    return tuple(
        sorted(
            {entry["audience"] for entry in _DOCS_LINKS},
            key=lambda value: _normalize_docs_audience(value),
        )
    )


def _docs_audience_filter_alias(value: str) -> str:
    """Return the shell-friendly filter token for a docs audience label."""
    return _normalize_docs_audience(value).replace(" ", "-")


def _available_docs_audience_filters() -> tuple[str, ...]:
    return tuple(_docs_audience_filter_alias(value) for value in _available_docs_audiences())


def _docs_audience_matches(audience_filter: str, audience_label: str) -> bool:
    needle = _normalize_docs_audience(audience_filter)
    normalized_label = _normalize_docs_audience(audience_label)
    if needle == normalized_label:
        return True
    if needle in {"maintainers", "operators"}:
        return needle in normalized_label.split()
    return False


def _filter_docs_entries(entries: list[_DocsEntry], audience: str | None) -> list[_DocsEntry]:
    if audience is None:
        return entries

    return [entry for entry in entries if _docs_audience_matches(audience, entry["audience"])]


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


def _format_docs_menu(entries: list[_DocsEntry], *, audience_filter: str | None = None) -> str:
    label_width = max(len(entry["label"]) for entry in entries)
    routes = "\n".join(_format_docs_entry(entry, label_width=label_width) for entry in entries)
    available_audiences = ", ".join(_available_docs_audiences())
    available_filters = ", ".join(_available_docs_audience_filters())
    filter_note = (
        f"Audience filter: {audience_filter}\n"
        if audience_filter is not None
        else "Filter by audience: easycat docs --audience learners\n"
    )
    return f"""[bold]EasyCat documentation[/]

{routes}

Online source: {_DOCS_SOURCE_URL}
Machine-readable routes, audiences, and command hints: easycat docs --json
Filtered machine-readable routes: easycat docs --audience maintainers --json
Available audiences: {available_audiences}
Available filters: {available_filters}
{_DOCS_AUDIENCE_ALIAS_NOTE}
{filter_note}
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
    audience: str | None = typer.Option(
        None,
        "--audience",
        help=(
            "Filter routes by exact audience label or broad operators/maintainers role, "
            "such as learners, app builders, coding agents, contributors, operators, "
            "or maintainers. "
            "Multi-word labels also accept hyphens or underscores; operators and "
            "maintainers include compound labels."
        ),
    ),
) -> None:
    """Show docs for learning, maintenance, validation, and operations."""
    entries = _docs_entries()
    filtered_entries = _filter_docs_entries(entries, audience)
    if not filtered_entries:
        available = ", ".join(_available_docs_audiences())
        available_filters = ", ".join(_available_docs_audience_filters())
        message = (
            f"Unknown docs audience {audience!r}. Available audiences: {available}. "
            f"Available filters: {available_filters}. "
            f"{_DOCS_AUDIENCE_ALIAS_NOTE}"
        )
        emit_command_error(
            "docs",
            message,
            json_output=json_output,
            available_audiences=list(_available_docs_audiences()),
            available_audience_filters=list(_available_docs_audience_filters()),
            audience_alias_note=_DOCS_AUDIENCE_ALIAS_NOTE,
            audience_filter=audience,
        )
        raise typer.Exit(2)

    if json_output:
        emit_json(
            json_envelope(
                "docs",
                entries=filtered_entries,
                source_url=_DOCS_SOURCE_URL,
                command_note=_DOCS_COMMAND_NOTE,
                audience_filter=audience,
                available_audiences=list(_available_docs_audiences()),
                available_audience_filters=list(_available_docs_audience_filters()),
                audience_alias_note=_DOCS_AUDIENCE_ALIAS_NOTE,
            )
        )
        return
    stdout_console.print(
        _format_docs_menu(filtered_entries, audience_filter=audience), soft_wrap=True
    )


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

    from easycat.cli.console import console as console_cmd
    from easycat.cli.debug.bundles import (
        bundles_app,
        debugger_app,
        diff_command,
        follow_journal,
        inspect_bundle,
        journal_app,
        latency_command,
        replay_bundle,
    )
    from easycat.cli.diagnose.doctor import doctor as doctor_cmd
    from easycat.cli.diagnose.explain import explain as explain_cmd
    from easycat.cli.scaffold.init import init as init_cmd
    from easycat.cli.serve import serve as serve_cmd
    from easycat.cli.validate import validate_app

    app.command(name="console", help=_COMMAND_TEXT["console"].help)(console_cmd)
    app.command(name="init", help=_COMMAND_TEXT["init"].help)(init_cmd)
    app.command(name="doctor", help=_COMMAND_TEXT["doctor"].help)(doctor_cmd)
    app.command(name="serve", help=_COMMAND_TEXT["serve"].help)(serve_cmd)
    app.command(name="docs", help=_COMMAND_TEXT["docs"].help)(docs_command)
    app.command(name="explain", help=_COMMAND_TEXT["explain"].help)(explain_cmd)
    app.command(name="inspect", help=_COMMAND_TEXT["inspect"].help)(inspect_bundle)
    app.command(name="replay", help=_COMMAND_TEXT["replay"].help)(replay_bundle)
    app.command(name="latency", help=_COMMAND_TEXT["latency"].help)(latency_command)
    app.command(name="diff", help=_COMMAND_TEXT["diff"].help)(diff_command)
    app.command(name="tail", help=_COMMAND_TEXT["tail"].help)(follow_journal)
    app.add_typer(bundles_app, name="bundles", help=_COMMAND_TEXT["bundles"].help)
    app.add_typer(debugger_app, name="debugger", help=_COMMAND_TEXT["debugger"].help)
    app.add_typer(journal_app, name="journal", help=_COMMAND_TEXT["journal"].help)
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
