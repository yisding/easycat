"""``easycat init`` — scaffold a new EasyCat project from a template.

Two paths: interactive (TTY prompts with sensible defaults) and
non-interactive (``--config '{...}'`` JSON, the primary surface for
coding-agent scaffolding).
"""

from __future__ import annotations

import subprocess
from difflib import get_close_matches
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
from easycat.config import _VALID_MCP_SCHEMES
from easycat.errors import EASYCAT_E101, EASYCAT_E102, EASYCAT_E103, EASYCAT_E104
from easycat.stt.factory import available_providers as available_stt_providers
from easycat.tts.factory import available_providers as available_tts_providers

_SCAFFOLD_DEFAULTS: dict[str, str] = {
    "AGENT_NAME": "Support",
    "AGENT_INSTRUCTIONS": (
        "You are a helpful assistant. Keep answers short — you're speaking aloud, not writing."
    ),
}


# Files we'll run through ``string.Template`` before copying.  Anything
# else is copied byte-for-byte.
_TEMPLATED_SUFFIXES: frozenset[str] = frozenset({".py", ".toml", ".md", ".txt", ".example"})

# Provider name → optional extra that ships its SDK.  Used to keep the
# scaffolded ``pyproject.toml`` in sync with the requested providers
# (e.g. ``stt="deepgram/flux"`` adds ``deepgram`` to the extras list).
_PROVIDER_TO_EXTRA: dict[str, str] = {
    "openai": "openai",
    "openai-realtime": "openai",
    "deepgram": "deepgram",
    "elevenlabs": "elevenlabs",
    "cartesia": "cartesia",
}

# Provider name → env var that holds its API key.  Used to extend the
# scaffolded ``.env.example`` so the developer sees every key they need.
_PROVIDER_TO_ENV_VAR: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "openai-realtime": "OPENAI_API_KEY",
    "deepgram": "DEEPGRAM_API_KEY",
    "elevenlabs": "ELEVENLABS_API_KEY",
    "cartesia": "CARTESIA_API_KEY",
}

# Per-template baseline extras that must always be present in the
# generated ``pyproject.toml`` regardless of provider choices.
_TEMPLATE_BASE_EXTRAS: dict[str, tuple[str, ...]] = {
    "openai-agents": ("openai-agents", "local"),
    "pydantic-ai": ("pydantic-ai", "local"),
    "text-chat": ("openai-agents",),
}

# Templates that accept ``stt`` / ``tts`` / ``mcp_servers`` because they
# instantiate :class:`EasyConfig`.  Text-only templates (REPLs) bypass
# the audio pipeline entirely, so those fields are rejected up front.
_VOICE_TEMPLATES: frozenset[str] = frozenset({"openai-agents", "pydantic-ai"})


def _templates_root() -> Path:
    """Filesystem path to the bundled templates directory."""
    return Path(str(files("easycat.cli.scaffold").joinpath("templates")))


def _provider_name(spec: str) -> str:
    """Extract the provider name from a ``"provider/model"`` spec."""
    return spec.partition("/")[0].strip().lower()


def _validate_for_template(cfg: InitConfig) -> None:
    """Reject fields that the scaffold cannot wire for the chosen template.

    The schema accepts ``stt`` / ``tts`` / ``llm`` / ``transport`` /
    ``tools`` / ``mcp_servers`` so coding agents can describe a full
    project, but only a subset is wired in this release.  Rather than
    silently dropping the caller's intent, reject unsupported requests
    with a stable error code.
    """
    if cfg.llm is not None:
        raise EASYCAT_E102(
            problem=(
                "'llm' is not yet supported by `easycat init` — wire the "
                "LLM directly in the generated `agent.py` for now."
            )
        )
    if cfg.tools:
        raise EASYCAT_E102(
            problem=(
                "'tools' is not yet supported by `easycat init` — add "
                "`@function_tool` (or framework equivalent) decorators in "
                "the generated `agent.py` for now."
            )
        )
    if cfg.transport != "local":
        raise EASYCAT_E102(
            problem=(
                f"transport={cfg.transport!r} is not yet supported by "
                "`easycat init` — only the default 'local' transport is "
                "scaffolded in this release."
            )
        )
    if cfg.template not in _VOICE_TEMPLATES:
        for field_name in ("stt", "tts"):
            if getattr(cfg, field_name) is not None:
                raise EASYCAT_E102(
                    problem=(
                        f"template {cfg.template!r} does not use the audio "
                        f"pipeline; remove {field_name!r} from --config or "
                        "pick a voice template (e.g. 'openai-agents')."
                    )
                )
        if cfg.mcp_servers:
            raise EASYCAT_E102(
                problem=(
                    f"template {cfg.template!r} does not yet wire "
                    "'mcp_servers'; pick a voice template or remove the field."
                )
            )
        return

    # Voice template — validate provider strings and MCP URIs up front so
    # the scaffolded ``agent.py`` cannot fail on first run for values that
    # ``easycat init`` accepted.  Without this, a typo like
    # ``stt="deepgrm/flux"`` writes happily and explodes with
    # ``EASYCAT_E104`` only when the user runs ``python agent.py``.
    if cfg.stt:
        _validate_provider_spec(cfg.stt, available_stt_providers(), kind="STT")
    if cfg.tts:
        _validate_provider_spec(cfg.tts, available_tts_providers(), kind="TTS")
    if cfg.mcp_servers:
        for uri in cfg.mcp_servers:
            if not any(uri.startswith(scheme) for scheme in _VALID_MCP_SCHEMES):
                raise EASYCAT_E102(
                    problem=(
                        f"invalid MCP server URI {uri!r}. Must start with one "
                        f"of {', '.join(_VALID_MCP_SCHEMES)} (e.g. "
                        "'stdio://npx -y @modelcontextprotocol/server-filesystem')."
                    )
                )


def _validate_provider_spec(spec: str, available: list[str], *, kind: str) -> None:
    """Ensure ``"provider/model"`` shortcuts use a known provider name.

    Mirrors the registry/fuzzy-suggest behavior of ``parse_stt_string`` /
    ``parse_tts_string`` without requiring an API key — which the user
    typically has not exported yet at scaffold time.
    """
    provider = spec.partition("/")[0].strip().lower()
    if provider in available:
        return
    suggestion = get_close_matches(provider, available, n=1, cutoff=0.5)
    hint = f" Did you mean {suggestion[0]!r}?" if suggestion else ""
    raise EASYCAT_E104(
        provider=f"{provider} ({kind})",
        available=", ".join(available),
        hint=hint,
    )


def _config_extra_kwargs(cfg: InitConfig) -> str:
    """Render extra ``EasyConfig(...)`` kwargs (or empty string).

    Comma-prefixed inline form so a single placeholder works in both the
    multi-line ``openai-agents`` template and the single-line
    ``pydantic-ai`` template; ruff format will reflow long lines after
    the developer runs it.
    """
    if cfg.template not in _VOICE_TEMPLATES:
        return ""
    parts: list[str] = []
    if cfg.stt:
        parts.append(f'stt="{cfg.stt}"')
    if cfg.tts:
        parts.append(f'tts="{cfg.tts}"')
    if cfg.mcp_servers:
        parts.append(f"mcp_servers={cfg.mcp_servers!r}")
    if not parts:
        return ""
    return ", " + ", ".join(parts)


def _extras_for(cfg: InitConfig) -> str:
    """Render the comma-separated extras list for ``pyproject.toml``."""
    extras = list(_TEMPLATE_BASE_EXTRAS.get(cfg.template, ()))
    seen = set(extras)
    for spec in (cfg.stt, cfg.tts):
        if not spec:
            continue
        extra = _PROVIDER_TO_EXTRA.get(_provider_name(spec))
        if extra and extra not in seen:
            extras.append(extra)
            seen.add(extra)
    return ",".join(extras)


def _extra_env_vars(cfg: InitConfig) -> str:
    """Render extra ``KEY=`` lines beyond the template's baseline.

    ``OPENAI_API_KEY`` is in every template's ``.env.example`` already;
    only non-OpenAI keys need to be added here.  Returned with a leading
    newline when non-empty so it appends cleanly to the existing file.
    """
    seen: set[str] = {"OPENAI_API_KEY"}
    extra: list[str] = []
    for spec in (cfg.stt, cfg.tts):
        if not spec:
            continue
        var = _PROVIDER_TO_ENV_VAR.get(_provider_name(spec))
        if var and var not in seen:
            extra.append(f"{var}=")
            seen.add(var)
    if not extra:
        return ""
    return "\n" + "\n".join(extra) + "\n"


def _substitutions(cfg: InitConfig, project_name: str) -> dict[str, str]:
    return {
        "AGENT_NAME": cfg.agent_name or _SCAFFOLD_DEFAULTS["AGENT_NAME"],
        "AGENT_INSTRUCTIONS": (cfg.agent_instructions or _SCAFFOLD_DEFAULTS["AGENT_INSTRUCTIONS"]),
        "PROJECT_NAME": project_name,
        "EASYCAT_CONFIG_EXTRA": _config_extra_kwargs(cfg),
        "EXTRAS": _extras_for(cfg),
        "EXTRA_ENV_VARS": _extra_env_vars(cfg),
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


def _is_existing_non_dir(path: Path) -> bool:
    """True if ``path`` exists as something other than a directory.

    Regular files, symlinks-to-files, and special nodes all collide
    with ``mkdir(parents=True, exist_ok=True)`` (which only silences
    the error when the existing target is a directory).  We refuse
    these up front — even with ``--force`` — so ``easycat init foo``
    raises a stable E101 instead of a raw ``FileExistsError``.
    """
    return path.exists() and not path.is_dir()


@cli_command
def init(
    name: str | None = typer.Argument(
        None,
        metavar="NAME",
        help="Name of the project directory (omit with --list-templates).",
    ),
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

    if name is None:
        stderr_console.print("[red]✗[/] Missing argument 'NAME'.")
        stderr_console.print("  [dim]Usage:[/] easycat init NAME [OPTIONS]")
        stderr_console.print("  [dim]Or:[/]    easycat init --list-templates")
        raise typer.Exit(2)

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

    _validate_for_template(cfg)

    target = Path(name).resolve()
    if _is_existing_non_dir(target) or (not force and _is_non_empty_dir(target)):
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
    stderr_console.print("  uv run easycat doctor [dim]# verify your setup[/]")
    stderr_console.print("  uv run --env-file .env python agent.py")


__all__: list[str] = ["init"]


# Silence mypy's "unused import" warning for future typing hooks.
_ = Any
