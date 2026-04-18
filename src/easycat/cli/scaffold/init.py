"""``easycat init`` — scaffold a new EasyCat project from a template.

Implements the first-contact developer surface described in
``plan/peripheral-cli.md``.  Two paths: interactive (TTY prompts with
sensible defaults) and non-interactive (``--config '{...}'`` JSON, the
primary surface for coding-agent scaffolding).
"""

from __future__ import annotations

import subprocess
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import Any

import typer
from rich.prompt import Prompt

from easycat.cli._errors import cli_command
from easycat.cli._output import (
    emit_json,
    info,
    json_envelope,
    stderr_console,
    stdout_console,
    success,
)
from easycat.cli.scaffold._schema import (
    InitConfig,
    available_templates,
    parse_config,
)
from easycat.errors import EASYCAT_E101, EASYCAT_E103

_SCAFFOLD_DEFAULTS: dict[str, str] = {
    "AGENT_NAME": "Support",
    "AGENT_INSTRUCTIONS": (
        "You are a helpful assistant. Keep answers short — you're speaking aloud, not writing."
    ),
}


# Files we'll run through ``string.Template`` before copying.  Anything
# else is copied byte-for-byte.
_TEMPLATED_SUFFIXES: frozenset[str] = frozenset({".py", ".toml", ".md", ".txt", ".example"})


def _templates_root() -> Path:
    """Filesystem path to the bundled templates directory."""
    return Path(str(files("easycat.cli.scaffold").joinpath("templates")))


def _substitutions(cfg: InitConfig, project_name: str) -> dict[str, str]:
    return {
        "AGENT_NAME": cfg.agent_name or _SCAFFOLD_DEFAULTS["AGENT_NAME"],
        "AGENT_INSTRUCTIONS": (cfg.agent_instructions or _SCAFFOLD_DEFAULTS["AGENT_INSTRUCTIONS"]),
        "PROJECT_NAME": project_name,
    }


def _should_template(source: Path) -> bool:
    if source.suffix in _TEMPLATED_SUFFIXES:
        return True
    # ``.env.example`` has suffix ``.example`` — caught above.
    return False


def _render_file(source: Path, dest: Path, mapping: dict[str, str]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if _should_template(source):
        text = source.read_text(encoding="utf-8")
        rendered = Template(text).safe_substitute(mapping)
        dest.write_text(rendered, encoding="utf-8")
    else:
        dest.write_bytes(source.read_bytes())


def _copy_template(template_name: str, target: Path, mapping: dict[str, str]) -> list[Path]:
    src_root = _templates_root() / template_name
    written: list[Path] = []
    for source in sorted(src_root.rglob("*")):
        if source.is_dir():
            continue
        rel = source.relative_to(src_root)
        dest = target / rel
        _render_file(source, dest, mapping)
        written.append(dest)
    return written


def _maybe_git_init(target: Path) -> bool:
    """Run ``git init`` silently.  Returns True on success."""
    try:
        subprocess.run(
            ["git", "init", "--initial-branch=main"],
            cwd=target,
            check=True,
            capture_output=True,
        )
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _prompt_interactive(template_default: str) -> InitConfig:
    templates = available_templates() or ["openai-agents"]
    if template_default not in templates:
        template_default = templates[0]
    template = Prompt.ask(
        "Template",
        choices=templates,
        default=template_default,
        console=stderr_console,
        show_choices=True,
    )
    agent_name = Prompt.ask(
        "Agent name",
        default=_SCAFFOLD_DEFAULTS["AGENT_NAME"],
        console=stderr_console,
    )
    agent_instructions = Prompt.ask(
        "Agent instructions",
        default=_SCAFFOLD_DEFAULTS["AGENT_INSTRUCTIONS"],
        console=stderr_console,
    )
    return InitConfig(
        template=template,
        agent_name=agent_name,
        agent_instructions=agent_instructions,
    )


def _is_non_empty_dir(path: Path) -> bool:
    return path.exists() and path.is_dir() and any(path.iterdir())


@cli_command
def init(
    name: str = typer.Argument(..., metavar="NAME", help="Name of the project directory."),
    template: str = typer.Option(
        "openai-agents",
        "--template",
        "-t",
        help="Template to use (run --list-templates to see the catalog).",
    ),
    config: str | None = typer.Option(
        None,
        "--config",
        "-c",
        help="JSON payload for non-interactive scaffolding.",
    ),
    list_templates: bool = typer.Option(
        False, "--list-templates", help="Print available templates and exit."
    ),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing non-empty directory."
    ),
    no_git: bool = typer.Option(False, "--no-git", help="Skip `git init`."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Scaffold a new EasyCat project from a template."""
    if list_templates:
        templates = available_templates()
        if json_output:
            emit_json(json_envelope("init", templates=templates))
        else:
            for t in templates:
                stdout_console.print(t)
        raise typer.Exit(0)

    # Resolve scaffolding config.  Priority: --config JSON > interactive
    # prompts (TTY only) > --template alone with defaults.
    if config is not None:
        cfg = parse_config(config)
    elif stderr_console.is_terminal and not json_output:
        cfg = _prompt_interactive(template)
    else:
        cfg = InitConfig(template=template)

    if cfg.template not in available_templates():
        raise EASYCAT_E103(
            template=cfg.template,
            available=", ".join(available_templates()),
        )

    target = Path(name).resolve()
    if not force and _is_non_empty_dir(target):
        raise EASYCAT_E101(target=str(target))

    target.mkdir(parents=True, exist_ok=True)

    mapping = _substitutions(cfg, target.name)
    written = _copy_template(cfg.template, target, mapping)
    git_ok = False if no_git else _maybe_git_init(target)

    agent_py = target / "agent.py"
    agent_lines = agent_py.read_text().count("\n") + 1 if agent_py.exists() else 0

    if json_output:
        emit_json(
            json_envelope(
                "init",
                path=str(target),
                template=cfg.template,
                files=[str(p.relative_to(target)) for p in written],
                agent_lines=agent_lines,
                git=git_ok,
            )
        )
        return

    stderr_console.print(f"Creating [cyan]{name}/[/]")
    for p in written:
        rel = p.relative_to(target)
        extra = f" ({agent_lines} lines)" if rel.name == "agent.py" else ""
        success(f"{rel}{extra}")
    if git_ok:
        success("git init")
    elif not no_git:
        info("git init skipped (git not available)")
    stderr_console.print()
    stderr_console.print("[bold]Next steps:[/]")
    stderr_console.print(f"  cd {name}")
    stderr_console.print("  cp .env.example .env  [dim]# then fill in your API keys[/]")
    stderr_console.print("  uv sync")
    stderr_console.print("  uvx easycat doctor    [dim]# verify your setup[/]")
    stderr_console.print("  uv run --env-file .env python agent.py")


__all__: list[str] = ["init"]


# Silence mypy's "unused import" warning for future typing hooks.
_ = Any
