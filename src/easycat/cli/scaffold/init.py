"""``easycat init`` — scaffold a new EasyCat project from a template.

Two paths: interactive (TTY prompts with sensible defaults) and
non-interactive (``--config '{...}'`` JSON, the primary surface for
coding-agent scaffolding).
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from difflib import get_close_matches
from importlib.resources import files
from pathlib import Path
from string import Template
from typing import TypedDict
from urllib.parse import unquote, urlsplit

import typer
from rich.markup import escape
from rich.prompt import Prompt

from easycat._provider_registry import provider_env_vars, provider_extras
from easycat.cli._errors import cli_command
from easycat.cli._output import (
    emit_command_error,
    emit_json,
    info,
    json_envelope,
    stderr_console,
    stdout_console,
    success,
)
from easycat.cli.scaffold._schema import (
    TEMPLATE_ARTIFACT_DIRECTORY_NAMES,
    InitConfig,
    available_templates,
    parse_config,
)
from easycat.config import _VALID_MCP_SCHEMES
from easycat.errors import (
    EASYCAT_E101,
    EASYCAT_E102,
    EASYCAT_E103,
    EASYCAT_E104,
    EasyCatError,
)
from easycat.stt.factory import available_stt_providers
from easycat.tts.factory import available_tts_providers

_SCAFFOLD_DEFAULTS: dict[str, str] = {
    "AGENT_NAME": "Support",
    "AGENT_INSTRUCTIONS": (
        "You are a helpful assistant. Keep answers short — you're speaking aloud, not writing."
    ),
}


# Files we'll run through ``string.Template`` before copying.  Anything
# else is copied byte-for-byte.
_TEMPLATED_SUFFIXES: frozenset[str] = frozenset({".py", ".toml", ".md", ".txt", ".example"})


def _provider_to_extra() -> dict[str, str]:
    """Provider name → optional extra that ships its SDK.

    Used to keep the scaffolded ``pyproject.toml`` in sync with the
    requested providers (e.g. ``stt="deepgram/flux"`` adds ``deepgram``
    to the extras list).  Derived from the live STT/TTS provider catalogs
    (entry-point discovery included) so new providers scaffold correctly
    without touching this file.
    """
    return provider_extras()


def _provider_to_env_var() -> dict[str, str]:
    """Provider name → env var that holds its API key.

    Used to extend the scaffolded ``.env.example`` so the developer sees
    every key they need.  Derived from the live STT/TTS provider catalogs
    (entry-point discovery included), so third-party providers registered
    via ``register_stt_provider`` / ``register_tts_provider`` or the
    ``easycat.stt_providers`` / ``easycat.tts_providers`` entry-point
    groups surface here too.
    """
    return provider_env_vars()


_FALLBACK_EASYCAT_VERSION_FLOOR = "0.1.0"


@dataclass(frozen=True, slots=True)
class _TemplateSpec:
    """Single source of template metadata and scaffold capabilities."""

    mode: str
    transport: str
    framework: str
    best_for: str
    required_env: tuple[str, ...]
    optional_env: tuple[str, ...]
    description: str
    base_extras: tuple[str, ...]
    run_command: str = "uv run --env-file .env python agent.py"
    check_files: tuple[str, ...] = ("agent.py",)
    docs_audience: str = "app-builders"
    expected_transport: str | None = None
    supports_audio_config: bool = False


@dataclass(frozen=True, slots=True)
class _ResolvedEasyCatSource:
    """One rendered dependency source for the generated project."""

    local_path: Path | None = None
    git_url: str | None = None
    git_rev: str | None = None


class _TemplateCatalogEntry(TypedDict):
    name: str
    mode: str
    transport: str
    framework: str
    best_for: str
    required_env: tuple[str, ...]
    optional_env: tuple[str, ...]
    description: str
    base_extras: tuple[str, ...]
    base_requirement: str
    files: tuple[str, ...]
    create_command: str
    repo_create_command: str
    next_step_commands: list[str]
    run_command: str
    check_command: str
    fix_command: str


_TEMPLATE_SPECS: dict[str, _TemplateSpec] = {
    "openai-agents": _TemplateSpec(
        mode="voice",
        transport="local mic",
        framework="OpenAI Agents",
        best_for="First local voice agent and default OpenAI Agents scaffold.",
        required_env=("OPENAI_API_KEY",),
        optional_env=(),
        description="Local microphone/speaker voice agent.",
        base_extras=("openai-agents", "local"),
        check_files=("agent.py", "tools.py"),
        expected_transport="local",
        supports_audio_config=True,
    ),
    "pydantic-ai": _TemplateSpec(
        mode="voice",
        transport="local mic",
        framework="Pydantic AI",
        best_for="Teams already building agents with Pydantic AI.",
        required_env=("OPENAI_API_KEY",),
        optional_env=(),
        description="Local voice agent using Pydantic AI.",
        base_extras=("pydantic-ai", "local"),
        expected_transport="local",
        supports_audio_config=True,
    ),
    "pydantic-ai-workflow": _TemplateSpec(
        mode="voice",
        transport="local mic",
        framework="Pydantic AI workflow",
        best_for="Small workflow examples around a Pydantic AI agent.",
        required_env=("OPENAI_API_KEY",),
        optional_env=(),
        description="Local voice agent with a small workflow object.",
        base_extras=("pydantic-ai", "local"),
        expected_transport="local",
        supports_audio_config=True,
    ),
    "text-chat": _TemplateSpec(
        mode="text",
        transport="terminal",
        framework="OpenAI Agents",
        best_for="Testing agent behavior without microphone or speaker setup.",
        required_env=("OPENAI_API_KEY",),
        optional_env=(),
        description="Text-only REPL for testing agent behavior without audio.",
        base_extras=("openai-agents",),
    ),
    "twilio-phone": _TemplateSpec(
        mode="voice",
        transport="Twilio",
        framework="OpenAI Agents",
        best_for="Phone-call prototypes and Twilio Media Streams servers.",
        required_env=("OPENAI_API_KEY", "TWILIO_STREAM_URL", "TWILIO_AUTH_TOKEN"),
        optional_env=(
            "TWILIO_WS_PORT",
            "TWILIO_STREAM_TOKEN_SECRET",
            "TWILIO_MAX_SESSIONS",
            "TWILIO_START_TIMEOUT_S",
            "TWILIO_DRAIN_TIMEOUT_S",
            "TWILIO_FORCE_SHUTDOWN_TIMEOUT_S",
            "TWILIO_PUBLIC_TWIML_URL",
            "TRUST_PROXY_HEADERS",
        ),
        description="Phone-call voice agent with a Twilio WebSocket server.",
        base_extras=("openai-agents", "telephony", "telephony-fastapi"),
        run_command=(
            "uv run --env-file .env uvicorn server:create_app --factory --host 0.0.0.0 --port 8000"
        ),
        check_files=("agent.py", "server.py"),
        expected_transport="twilio",
        supports_audio_config=True,
    ),
    "telnyx-phone": _TemplateSpec(
        mode="voice",
        transport="Telnyx",
        framework="OpenAI Agents",
        best_for="Phone-call prototypes and Telnyx Call Control media-stream servers.",
        required_env=(
            "OPENAI_API_KEY",
            "TELNYX_STREAM_URL",
            "TELNYX_API_KEY",
            "TELNYX_PUBLIC_KEY",
        ),
        optional_env=(
            "TELNYX_WS_PORT",
            "TELNYX_STREAM_TOKEN_SECRET",
            "TELNYX_MAX_SESSIONS",
            "TELNYX_START_TIMEOUT_S",
            "TELNYX_DRAIN_TIMEOUT_S",
            "TELNYX_FORCE_SHUTDOWN_TIMEOUT_S",
        ),
        description="Phone-call voice agent with a Telnyx Call Control server.",
        base_extras=("openai-agents", "telnyx", "telephony-fastapi"),
        run_command=(
            "uv run --env-file .env uvicorn server:create_app --factory --host 0.0.0.0 --port 8000"
        ),
        check_files=("agent.py", "server.py"),
        expected_transport="telnyx",
        supports_audio_config=True,
    ),
    "webrtc-browser": _TemplateSpec(
        mode="voice",
        transport="WebRTC",
        framework="OpenAI Agents",
        best_for="Browser-based voice apps using WebRTC.",
        required_env=("OPENAI_API_KEY",),
        optional_env=("TURN_SERVER_URL", "TURN_USERNAME", "TURN_CREDENTIAL"),
        description="Browser voice agent using WebRTC audio.",
        base_extras=("openai-agents", "webrtc"),
        expected_transport="webrtc",
        supports_audio_config=True,
    ),
    "provider": _TemplateSpec(
        mode="voice",
        transport="local mic",
        framework="OpenAI Agents",
        best_for="Publishing a custom EasyCat VAD provider package out of tree.",
        required_env=("OPENAI_API_KEY",),
        optional_env=(),
        description="External VAD provider package skeleton with conformance test and live demo.",
        base_extras=("openai-agents", "local"),
        check_files=("agent.py", "custom_vad.py", "test_custom_vad.py"),
        docs_audience="provider-maintainers",
    ),
    "provider-stt": _TemplateSpec(
        mode="voice",
        transport="local mic",
        framework="OpenAI Agents",
        best_for="Publishing a custom EasyCat STT provider package out of tree.",
        required_env=("OPENAI_API_KEY",),
        optional_env=(),
        description="External STT provider skeleton with registration and offline contracts.",
        base_extras=("openai-agents", "local"),
        check_files=("agent.py", "custom_stt.py", "test_custom_stt.py"),
        docs_audience="provider-maintainers",
    ),
    "provider-tts": _TemplateSpec(
        mode="voice",
        transport="local mic",
        framework="OpenAI Agents",
        best_for="Publishing a custom EasyCat TTS provider package out of tree.",
        required_env=("OPENAI_API_KEY",),
        optional_env=(),
        description="External TTS provider skeleton with registration and offline contracts.",
        base_extras=("openai-agents", "local"),
        check_files=("agent.py", "custom_tts.py", "test_custom_tts.py"),
        docs_audience="provider-maintainers",
    ),
}
_UNKNOWN_TEMPLATE_SPEC = _TemplateSpec(
    mode="unknown",
    transport="unknown",
    framework="unknown",
    best_for="Template selection guidance has not been documented yet.",
    required_env=(),
    optional_env=(),
    description="Template metadata has not been documented yet.",
    base_extras=(),
)
_INIT_COMMAND_NOTE = (
    "create_command uses installed CLI form; repo_create_command runs from this repository "
    "root; catalog next_step_commands preview the my-agent post-create sequence; "
    "run_command, check_command, and fix_command run after cd into the scaffolded project."
)
_INIT_HUMAN_COMMAND_NOTE = (
    "Command note: Create uses installed CLI form; Repo create runs from this repository root; "
    "JSON catalog next_step_commands previews the my-agent post-create sequence; "
    "Doctor, Doctor JSON, Check, Fix, Docs, Audience docs, Audience docs JSON, "
    "Docs JSON, JSON schema, and Run after cd are run inside the scaffolded project."
)
_INIT_MACHINE_READABLE_HINT = (
    "Machine-readable template catalog: easycat init --list-templates --json"
)
_NEXT_STEP_DOCTOR_COMMAND = "uv run easycat doctor --env-file .env"
_NEXT_STEP_DOCTOR_JSON_COMMAND = "uv run easycat doctor --env-file .env --json"
_NEXT_STEP_DOCS_COMMAND = "uv run easycat docs"
_NEXT_STEP_DOCS_JSON_COMMAND = "uv run easycat docs --json"
_NEXT_STEP_EXPLAIN_JSON_SCHEMA_COMMAND = "uv run easycat explain json-schema"

_TRANSPORT_ALIASES: dict[str, str] = {
    "browser": "webrtc",
    "local-mic": "local",
    "phone": "twilio",
    "telephony": "twilio",
}

# Directory names that may sit in the live template source at install time
# (cache and local tool artifacts from running validation or coding agents
# against the templates) but must never ship into a freshly scaffolded
# project. Bytecode and local secret files are filtered separately by suffix.
_COPY_IGNORE: frozenset[str] = TEMPLATE_ARTIFACT_DIRECTORY_NAMES
_COPY_FILE_IGNORE: frozenset[str] = frozenset({".coverage", "coverage.xml"})
_COPY_FILE_PREFIX_IGNORE: tuple[str, ...] = (".coverage.",)
_COPY_PART_SUFFIX_IGNORE: tuple[str, ...] = (".egg-info",)
_COPY_SUFFIX_IGNORE: frozenset[str] = frozenset({".key", ".pem", ".pyc", ".pyo"})


def _templates_root() -> Path:
    """Filesystem path to the bundled templates directory."""
    return Path(str(files("easycat.cli.scaffold").joinpath("templates")))


def _template_sources(template_name: str) -> list[Path]:
    """Return files from a template source tree that will be copied."""
    src_root = _templates_root() / template_name
    sources: list[Path] = []
    for source in sorted(src_root.rglob("*")):
        if source.is_dir():
            continue
        # Filter on the path *relative to the template root* — the absolute
        # path may legitimately contain ignored names (e.g. a checkout under
        # a `.claude/worktrees/...` directory) that must not hide templates.
        rel_parts = source.relative_to(src_root).parts
        template_parts = (template_name, *rel_parts)
        ignored_directory = any(part in _COPY_IGNORE for part in template_parts)
        ignored_file = source.name in _COPY_FILE_IGNORE or source.name.startswith(
            _COPY_FILE_PREFIX_IGNORE
        )
        ignored_part_suffix = any(
            part.endswith(_COPY_PART_SUFFIX_IGNORE) for part in template_parts
        )
        ignored_suffix = source.suffix in _COPY_SUFFIX_IGNORE
        if ignored_directory or ignored_file or ignored_part_suffix or ignored_suffix:
            continue
        sources.append(source)
    return sources


def _template_file_names(template_name: str) -> tuple[str, ...]:
    """Return relative file names generated by ``template_name``."""
    src_root = _templates_root() / template_name
    return tuple(
        source.relative_to(src_root).as_posix() for source in _template_sources(template_name)
    )


def _base_requirement(template_name: str) -> str:
    """Return the EasyCat package requirement generated for template defaults."""
    spec = _TEMPLATE_SPECS.get(template_name, _UNKNOWN_TEMPLATE_SPEC)
    extras = ",".join(spec.base_extras)
    version = _easycat_version_floor()
    return f"easycat[{extras}]>={version}" if extras else f"easycat>={version}"


def _easycat_version_floor() -> str:
    """Return the EasyCat version used as generated dependency lower bound."""
    try:
        return _ordered_specifier_version_floor(importlib.metadata.version("easycat"))
    except importlib.metadata.PackageNotFoundError:
        return _FALLBACK_EASYCAT_VERSION_FLOOR


def _ordered_specifier_version_floor(version: str) -> str:
    """Return ``version`` in a form accepted after ordered specifiers.

    PEP 440 local version labels (for example ``0.1.0+local``) are valid
    distribution versions, but packaging tools reject them with ordered
    comparisons such as ``>=``.  The public version segment is the closest
    lower-bound floor for generated scaffold dependencies.
    """
    public_version, _separator, _local_label = version.partition("+")
    return public_version


def _editable_easycat_source() -> Path | None:
    """Return the local EasyCat checkout for an editable/repo install.

    Pre-launch, ``easycat`` is not on PyPI, so a scaffold that only
    declares ``easycat[...]>=X`` in its dependencies can never resolve.
    When the running CLI comes from an editable install (``uv sync`` in
    the repo, ``pip install -e``), PEP 610 ``direct_url.json`` points at
    the checkout — the scaffold can then pin that path via
    ``[tool.uv.sources]``.  Returns ``None`` for published installs.
    """
    try:
        dist = importlib.metadata.distribution("easycat")
    except importlib.metadata.PackageNotFoundError:
        return None
    try:
        raw = dist.read_text("direct_url.json")
    except OSError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data.get("dir_info", {}).get("editable"):
        return None
    url = data.get("url", "")
    if not isinstance(url, str) or not url.startswith("file://"):
        return None
    source = Path(unquote(urlsplit(url).path))
    return source if (source / "pyproject.toml").is_file() else None


def _invalid_git_source(problem: str) -> EasyCatError:
    return EASYCAT_E102(problem=f"invalid easycat Git source: {problem}")


def _validate_git_source_url(value: str) -> str:
    """Validate a portable, credential-free Git remote for generated TOML."""
    if not value or value != value.strip() or any(char.isspace() for char in value):
        raise _invalid_git_source("URL must be a non-empty string without whitespace")

    parsed = urlsplit(value)
    if parsed.query or parsed.fragment:
        raise _invalid_git_source("URLs must not include query strings or fragments")
    if parsed.scheme:
        if parsed.scheme not in {"git", "http", "https", "ssh"}:
            raise _invalid_git_source(
                "URL must use git://, http://, https://, ssh://, or Git's user@host:path form"
            )
        if not parsed.hostname or not parsed.path.strip("/"):
            raise _invalid_git_source("URL must include a host and repository path")
        if parsed.scheme in {"http", "https"} and (parsed.username or parsed.password):
            raise _invalid_git_source(
                "HTTP(S) URLs must not embed credentials; use a Git credential helper"
            )
        if parsed.password:
            raise _invalid_git_source("URLs must not embed passwords or tokens")
        return value

    # Git's scp-like SSH syntax, for example git@github.com:org/repo.git.
    if re.fullmatch(r"[^/@:\s]+@[^/@:\s]+:[^\s]+", value):
        return value
    raise _invalid_git_source(
        "URL must use git://, http://, https://, ssh://, or Git's user@host:path form"
    )


def _validate_git_revision(value: str) -> str:
    """Validate a branch, tag, or commit without constraining Git's ref grammar."""
    if not value or value != value.strip() or any(char.isspace() for char in value):
        raise _invalid_git_source("revision must be non-empty and contain no whitespace")
    if value.startswith("-"):
        raise _invalid_git_source("revision must not start with '-'")
    return value


def _resolve_easycat_source(
    cli_source: str | None,
    cli_git: str | None,
    cli_git_rev: str | None,
    cfg: InitConfig,
) -> _ResolvedEasyCatSource:
    """Resolve one local or portable Git dependency source.

    Explicit local and Git inputs are mutually exclusive across CLI flags and
    JSON config. Same-kind CLI values override their config counterparts. With
    no explicit source, editable/repo installs retain the historical local-path
    auto-detection behavior.
    """
    local_declared = cli_source is not None or cfg.easycat_source is not None
    git_declared = any(
        value is not None for value in (cli_git, cli_git_rev, cfg.easycat_git, cfg.easycat_git_rev)
    )
    if local_declared and git_declared:
        raise EASYCAT_E102(
            problem=(
                "easycat local and Git sources are mutually exclusive; use either "
                "--easycat-source/easycat_source or "
                "--easycat-git/easycat_git (with an optional Git revision)"
            )
        )

    if git_declared:
        git_url = cli_git if cli_git is not None else cfg.easycat_git
        if git_url is None:
            raise _invalid_git_source(
                "a revision requires --easycat-git or the easycat_git config field"
            )
        # A CLI URL is a complete source override. A revision-only CLI flag can
        # still pin the URL supplied by JSON config.
        git_rev = (
            cli_git_rev
            if cli_git_rev is not None
            else (None if cli_git is not None else cfg.easycat_git_rev)
        )
        return _ResolvedEasyCatSource(
            git_url=_validate_git_source_url(git_url),
            git_rev=_validate_git_revision(git_rev) if git_rev is not None else None,
        )

    explicit = cli_source if cli_source is not None else cfg.easycat_source
    if explicit is None:
        return _ResolvedEasyCatSource(local_path=_editable_easycat_source())
    if not explicit.strip():
        raise EASYCAT_E102(problem="easycat_source must be a non-empty checkout path")
    source = Path(explicit).expanduser().resolve()
    if not (source / "pyproject.toml").is_file():
        raise EASYCAT_E102(
            problem=(
                f"easycat_source {explicit!r} is not an EasyCat checkout "
                "(no pyproject.toml found there). Point it at the repository "
                "root, e.g. --easycat-source ~/Code/easycat."
            )
        )
    return _ResolvedEasyCatSource(local_path=source)


def _sources_block(
    easycat_source: Path | None,
    easycat_git: str | None = None,
    easycat_git_rev: str | None = None,
) -> str:
    """Render the ``[tool.uv.sources]`` block for the generated pyproject.

    Returns an empty string for published installs — the
    ``$EASYCAT_SOURCES_BLOCK`` placeholder line is then dropped entirely
    by :func:`_render_text`.
    """
    if easycat_git is not None:
        fields = [f"git = {json.dumps(easycat_git)}"]
        if easycat_git_rev is not None:
            fields.append(f"rev = {json.dumps(easycat_git_rev)}")
        return (
            "\n"
            "# Portable Git source selected by easycat init. Pin a reviewed\n"
            "# revision for reproducible CI and collaborator installs.\n"
            "[tool.uv.sources]\n"
            f"easycat = {{ {', '.join(fields)} }}"
        )
    if easycat_source is None:
        return ""
    path_literal = json.dumps(str(easycat_source))
    return (
        "\n"
        "# EasyCat is not on PyPI yet, so resolve it from the local checkout\n"
        "# this project was scaffolded from. Delete this block and run\n"
        "# `uv sync` again once you depend on the published package.\n"
        "[tool.uv.sources]\n"
        f"easycat = {{ path = {path_literal}, editable = true }}"
    )


def _available_template_catalog() -> list[_TemplateCatalogEntry]:
    """Return template metadata in the same order as ``available_templates()``."""
    catalog: list[_TemplateCatalogEntry] = []
    for name in available_templates():
        spec = _TEMPLATE_SPECS.get(name, _UNKNOWN_TEMPLATE_SPEC)
        catalog.append(
            {
                "name": name,
                "mode": spec.mode,
                "transport": spec.transport,
                "framework": spec.framework,
                "best_for": spec.best_for,
                "required_env": spec.required_env,
                "optional_env": spec.optional_env,
                "description": spec.description,
                "base_extras": spec.base_extras,
                "base_requirement": _base_requirement(name),
                "files": _template_file_names(name),
                "create_command": _create_template_command(name),
                "repo_create_command": _create_template_command(name, repo_local=True),
                "next_step_commands": _next_step_commands(Path("my-agent"), name),
                "run_command": _next_step_run_command(name),
                "check_command": _next_step_check_command(name),
                "fix_command": _next_step_fix_command(name),
            }
        )
    return catalog


def _create_template_command(template: str, *, repo_local: bool = False) -> str:
    """Return the copyable command for scaffolding a project from ``template``."""
    command = f"easycat init my-agent --template {template}"
    return f"uv run {command}" if repo_local else command


def _format_template_catalog(catalog: list[_TemplateCatalogEntry]) -> str:
    """Render template metadata for ``easycat init --list-templates``."""
    if not catalog:
        return ""
    rows = []
    for entry in catalog:
        metadata = f"{entry['mode']}; {entry['transport']}; {entry['framework']}"
        optional_env = entry.get("optional_env", ())
        optional_env_line = (
            f"  [dim]Optional env:[/] {escape(', '.join(optional_env))}\n" if optional_env else ""
        )
        base_extras = ", ".join(entry["base_extras"]) or "none"
        generated_files = ", ".join(entry["files"]) or "none"
        audience_docs, audience_docs_json = _next_step_audience_docs_commands(entry["name"])
        rows.append(
            f"[cyan]{escape(entry['name'])}[/]\n"
            f"  {escape(entry['description'])}\n"
            f"  [dim]Best for:[/] {escape(entry['best_for'])}\n"
            f"  [dim]Required env:[/] {escape(', '.join(entry['required_env']) or 'none')}\n"
            f"{optional_env_line}"
            f"  [dim]Base extras:[/] {escape(base_extras)}\n"
            f"  [dim]Base package:[/] {escape(entry['base_requirement'])}\n"
            f"  [dim]Files:[/] {escape(generated_files)}\n"
            f"  [dim]{escape(metadata)}[/]\n"
            f"  [dim]Create:[/] {escape(entry['create_command'])}\n"
            f"  [dim]Repo create:[/] {escape(entry['repo_create_command'])}\n"
            f"  [dim]Doctor after cd:[/] {escape(_NEXT_STEP_DOCTOR_COMMAND)}\n"
            f"  [dim]Doctor JSON after cd:[/] {escape(_NEXT_STEP_DOCTOR_JSON_COMMAND)}\n"
            f"  [dim]Check after cd:[/] {escape(entry['check_command'])}\n"
            f"  [dim]Fix if needed after cd:[/] {escape(entry['fix_command'])}\n"
            f"  [dim]Docs after cd:[/] {escape(_NEXT_STEP_DOCS_COMMAND)}\n"
            f"  [dim]Audience docs after cd:[/] {escape(audience_docs)}\n"
            f"  [dim]Audience docs JSON after cd:[/] {escape(audience_docs_json)}\n"
            f"  [dim]Docs JSON after cd:[/] {escape(_NEXT_STEP_DOCS_JSON_COMMAND)}\n"
            f"  [dim]JSON schema after cd:[/] {escape(_NEXT_STEP_EXPLAIN_JSON_SCHEMA_COMMAND)}\n"
            f"  [dim]Run after cd:[/] {escape(entry['run_command'])}"
        )
    return (
        "\n".join(rows)
        + f"\n\n[dim]{escape(_INIT_HUMAN_COMMAND_NOTE)}[/]"
        + f"\n[dim]{escape(_INIT_MACHINE_READABLE_HINT)}[/]"
    )


def _next_step_run_command(template: str) -> str:
    """Return the primary run command for the scaffold success footer."""
    return _TEMPLATE_SPECS.get(template, _UNKNOWN_TEMPLATE_SPEC).run_command


def _next_step_check_command(template: str) -> str:
    """Return the scaffold-local lint/syntax check command for the success footer."""
    filenames = _TEMPLATE_SPECS.get(template, _UNKNOWN_TEMPLATE_SPEC).check_files
    return "uv run ruff check " + " ".join(filenames)


def _next_step_fix_command(template: str) -> str:
    """Return the scaffold-local auto-fix command for Ruff-fixable lint findings."""
    filenames = _TEMPLATE_SPECS.get(template, _UNKNOWN_TEMPLATE_SPEC).check_files
    return "uv run ruff check --fix " + " ".join(filenames)


def _next_step_audience_docs_commands(template: str) -> tuple[str, str]:
    """Return the human and JSON docs filters appropriate for a scaffold."""
    audience = _TEMPLATE_SPECS.get(template, _UNKNOWN_TEMPLATE_SPEC).docs_audience
    command = f"uv run easycat docs --audience {audience}"
    return command, f"{command} --json"


def _next_step_commands(target: Path, template: str) -> list[str]:
    """Return the ordered post-scaffold command sequence."""
    audience_docs, audience_docs_json = _next_step_audience_docs_commands(template)
    return [
        f"cd {shlex.quote(str(target))}",
        "cp .env.example .env",
        "uv sync",
        _NEXT_STEP_DOCTOR_COMMAND,
        _NEXT_STEP_DOCTOR_JSON_COMMAND,
        _next_step_check_command(template),
        _next_step_fix_command(template),
        _NEXT_STEP_DOCS_COMMAND,
        audience_docs,
        audience_docs_json,
        _NEXT_STEP_DOCS_JSON_COMMAND,
        _NEXT_STEP_EXPLAIN_JSON_SCHEMA_COMMAND,
        _next_step_run_command(template),
    ]


def _provider_name(spec: str) -> str:
    """Extract the provider name from a ``"provider/model"`` spec."""
    return spec.partition("/")[0].strip().lower()


def _transport_name(value: str) -> str:
    """Normalize scaffold transport aliases used in ``--config``."""
    normalized = value.strip().lower()
    return _TRANSPORT_ALIASES.get(normalized, normalized)


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

    template_spec = _TEMPLATE_SPECS.get(cfg.template, _UNKNOWN_TEMPLATE_SPEC)
    expected_transport = template_spec.expected_transport
    if cfg.transport is not None:
        requested_transport = _transport_name(cfg.transport)
        if expected_transport is None:
            raise EASYCAT_E102(
                problem=(
                    f"template {cfg.template!r} does not use an audio transport; "
                    "remove 'transport' from --config."
                )
            )
        if requested_transport != expected_transport:
            aliases = " or 'browser'" if expected_transport == "webrtc" else ""
            raise EASYCAT_E102(
                problem=(
                    f"template {cfg.template!r} uses transport={expected_transport!r}; "
                    f"remove 'transport' or set it to {expected_transport!r}{aliases}."
                )
            )

    if not template_spec.supports_audio_config:
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

    The templates use the parseable sentinel ``**__EASYCAT_CONFIG_EXTRA__``.
    Rendering replaces that sentinel with this comma-separated keyword
    argument fragment.
    """
    if not _TEMPLATE_SPECS.get(cfg.template, _UNKNOWN_TEMPLATE_SPEC).supports_audio_config:
        return ""
    parts: list[str] = []
    if cfg.stt:
        parts.append(f"stt={json.dumps(cfg.stt)}")
    if cfg.tts:
        parts.append(f"tts={json.dumps(cfg.tts)}")
    if cfg.mcp_servers:
        parts.append(f"mcp_servers={cfg.mcp_servers!r}")
    if not parts:
        return ""
    return ", ".join(parts)


def _extras_for(cfg: InitConfig) -> str:
    """Render the comma-separated extras list for ``pyproject.toml``."""
    extras = list(_TEMPLATE_SPECS.get(cfg.template, _UNKNOWN_TEMPLATE_SPEC).base_extras)
    seen = set(extras)
    for spec in (cfg.stt, cfg.tts):
        if not spec:
            continue
        extra = _provider_to_extra().get(_provider_name(spec))
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
        var = _provider_to_env_var().get(_provider_name(spec))
        if var and var not in seen:
            extra.append(f"{var}=")
            seen.add(var)
    if not extra:
        return ""
    return "\n" + "\n".join(extra) + "\n"


def _required_env_for(cfg: InitConfig) -> tuple[str, ...]:
    """Return every env var the rendered scaffold needs on its default path."""
    required = list(_TEMPLATE_SPECS.get(cfg.template, _UNKNOWN_TEMPLATE_SPEC).required_env)
    seen = set(required)
    for provider_spec in (cfg.stt, cfg.tts):
        if not provider_spec:
            continue
        var = _provider_to_env_var().get(_provider_name(provider_spec))
        if var and var not in seen:
            required.append(var)
            seen.add(var)
    return tuple(required)


def _optional_env_for(cfg: InitConfig) -> tuple[str, ...]:
    """Return optional scaffold knobs, excluding anything required dynamically."""
    required = set(_required_env_for(cfg))
    return tuple(
        name
        for name in _TEMPLATE_SPECS.get(cfg.template, _UNKNOWN_TEMPLATE_SPEC).optional_env
        if name not in required
    )


def _escape_surrogates(value: str) -> str:
    """Escape surrogate code points so rendered files stay UTF-8 encodable."""
    return "".join(
        f"\\u{ord(char):04x}" if 0xD800 <= ord(char) <= 0xDFFF else char for char in value
    )


def _python_string_literal_contents(value: str) -> str:
    """Render escaped contents for a double-quoted Python string literal.

    ``ensure_ascii=False`` keeps non-ASCII characters (em-dashes, accents,
    CJK, …) intact in the generated ``agent.py`` instead of emitting
    ``\\uXXXX`` escapes, while still escaping ``\\``, ``"``, and newlines.
    JSON also accepts lone surrogate escapes, which cannot be written raw as
    UTF-8, so escape only those surrogate code points after rendering.
    """
    return _escape_surrogates(json.dumps(value, ensure_ascii=False))[1:-1]


def _substitutions(
    cfg: InitConfig,
    project_name: str,
    *,
    easycat_source: Path | None = None,
    easycat_git: str | None = None,
    easycat_git_rev: str | None = None,
) -> dict[str, str]:
    return {
        "AGENT_NAME": _python_string_literal_contents(
            cfg.agent_name or _SCAFFOLD_DEFAULTS["AGENT_NAME"]
        ),
        "AGENT_INSTRUCTIONS": _python_string_literal_contents(
            cfg.agent_instructions or _SCAFFOLD_DEFAULTS["AGENT_INSTRUCTIONS"]
        ),
        "PROJECT_NAME": project_name,
        "PYPROJECT_NAME": _pyproject_name(project_name),
        "TEMPLATE": cfg.template,
        "REQUIRED_ENV": json.dumps(list(_required_env_for(cfg))),
        "OPTIONAL_ENV": json.dumps(list(_optional_env_for(cfg))),
        "EASYCAT_CONFIG_EXTRA": _config_extra_kwargs(cfg),
        "EASYCAT_VERSION_FLOOR": _easycat_version_floor(),
        "EXTRAS": _extras_for(cfg),
        "EXTRA_ENV_VARS": _extra_env_vars(cfg),
        "EASYCAT_SOURCES_BLOCK": _sources_block(
            easycat_source,
            easycat_git,
            easycat_git_rev,
        ),
    }


def _pyproject_name(project_name: str) -> str:
    """Return a valid, stable project metadata name for pyproject.toml."""
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", project_name).strip("-").lower()
    return normalized or "easycat-agent"


def _should_template(source: Path) -> bool:
    # ``.env.example`` has suffix ``.example`` — caught above.
    return source.suffix in _TEMPLATED_SUFFIXES


def _render_text(text: str, mapping: dict[str, str]) -> str:
    if not mapping.get("EASYCAT_SOURCES_BLOCK"):
        # Published install: drop the optional local-source block entirely.
        text = text.replace("$EASYCAT_SOURCES_BLOCK\n", "")
    rendered = Template(text).safe_substitute(mapping)
    extra_kwargs = mapping["EASYCAT_CONFIG_EXTRA"]
    # Longest indent first: an 8-space pattern also matches inside a 12-space
    # line and would leave four orphaned spaces behind when the sentinel drops.
    for indent in ("            ", "        "):
        rendered = rendered.replace(
            f"{indent}**__EASYCAT_CONFIG_EXTRA__,  # noqa: F821\n",
            f"{indent}{extra_kwargs},\n" if extra_kwargs else "",
        )
    # Drop the sentinel together with the separator that introduced it, so no
    # dangling comma survives and the renderer needs no per-template knowledge.
    rendered = rendered.replace(
        ", **__EASYCAT_CONFIG_EXTRA__", f", {extra_kwargs}" if extra_kwargs else ""
    )
    rendered = rendered.replace("**__EASYCAT_CONFIG_EXTRA__", extra_kwargs)
    rendered = rendered.replace("  # noqa: F821", "")
    return rendered


def _render_file(source: Path, dest: Path, mapping: dict[str, str]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if _should_template(source):
        text = source.read_text(encoding="utf-8")
        rendered = _render_text(text, mapping)
        dest.write_text(rendered, encoding="utf-8")
    else:
        dest.write_bytes(source.read_bytes())


def _copy_template(template_name: str, target: Path, mapping: dict[str, str]) -> list[Path]:
    src_root = _templates_root() / template_name
    written: list[Path] = []
    for source in _template_sources(template_name):
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
    return path.is_symlink() or (path.exists() and not path.is_dir())


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
        help="JSON payload for non-interactive scaffolding; see easycat explain init-schema.",
    ),
    list_templates: bool = typer.Option(
        False,
        "--list-templates",
        help=(
            "Show template guidance, base package requirements, extras, env vars, files, "
            "and preflight/check/fix/docs/json-schema/run commands."
        ),
    ),
    easycat_source: str | None = typer.Option(
        None,
        "--easycat-source",
        help=(
            "Path to a local EasyCat checkout the generated project should "
            "resolve `easycat` from (auto-detected for editable/repo installs)."
        ),
    ),
    easycat_git: str | None = typer.Option(
        None,
        "--easycat-git",
        help=(
            "Portable EasyCat Git repository URL for generated projects; mutually "
            "exclusive with --easycat-source."
        ),
    ),
    easycat_git_rev: str | None = typer.Option(
        None,
        "--easycat-git-rev",
        help="Optional branch, tag, or commit to pin with --easycat-git.",
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
        catalog = _available_template_catalog()
        if json_output:
            emit_json(
                json_envelope(
                    "init",
                    templates=templates,
                    catalog=catalog,
                    command_note=_INIT_COMMAND_NOTE,
                )
            )
        else:
            stdout_console.print(_format_template_catalog(catalog), soft_wrap=True)
        raise typer.Exit(0)

    if name is None:
        message = (
            "Missing argument 'NAME'. Usage: easycat init NAME [OPTIONS]. "
            "Or: easycat init --list-templates."
        )
        if json_output:
            emit_command_error("init", message, json_output=True)
        else:
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
    source = _resolve_easycat_source(
        easycat_source,
        easycat_git,
        easycat_git_rev,
        cfg,
    )

    raw_target = Path(name)
    if os.path.lexists(raw_target):
        # Broken or valid symlink/file must be rejected before resolve() follows it.
        candidate = raw_target if raw_target.is_symlink() else raw_target.resolve()
        if _is_existing_non_dir(raw_target) or (not force and _is_non_empty_dir(candidate)):
            raise EASYCAT_E101(target=str(candidate))
        if raw_target.is_symlink():
            raise EASYCAT_E101(target=str(raw_target.resolve()))
    target = Path(name).resolve()
    if _is_existing_non_dir(target) or (not force and _is_non_empty_dir(target)):
        raise EASYCAT_E101(target=str(target))

    target.mkdir(parents=True, exist_ok=True)

    mapping = _substitutions(
        cfg,
        target.name,
        easycat_source=source.local_path,
        easycat_git=source.git_url,
        easycat_git_rev=source.git_rev,
    )
    written = _copy_template(cfg.template, target, mapping)
    git_ok = False if no_git else _maybe_git_init(target)

    agent_py = target / "agent.py"
    agent_lines = agent_py.read_text(encoding="utf-8").count("\n") + 1 if agent_py.exists() else 0

    if json_output:
        emit_json(
            json_envelope(
                "init",
                path=str(target),
                template=cfg.template,
                pyproject_name=_pyproject_name(target.name),
                files=[str(p.relative_to(target)) for p in written],
                agent_lines=agent_lines,
                easycat_source=str(source.local_path) if source.local_path else None,
                easycat_git=source.git_url,
                easycat_git_rev=source.git_rev,
                git=git_ok,
                run_command=_next_step_run_command(cfg.template),
                check_command=_next_step_check_command(cfg.template),
                fix_command=_next_step_fix_command(cfg.template),
                next_step_commands=_next_step_commands(target, cfg.template),
                command_note=_INIT_COMMAND_NOTE,
            )
        )
        return

    stderr_console.print(f"Creating [cyan]{escape(name)}/[/]")
    for p in written:
        rel = p.relative_to(target)
        extra = f" ({agent_lines} lines)" if rel.name == "agent.py" else ""
        success(f"{rel}{extra}")
    if source.local_path is not None:
        info(f"easycat resolved from local checkout: {escape(str(source.local_path))}")
    elif source.git_url is not None:
        revision = f" at {source.git_rev}" if source.git_rev else ""
        info(f"easycat resolved from portable Git source: {escape(source.git_url + revision)}")
    if git_ok:
        success("git init")
    elif not no_git:
        info("git init skipped (git not available)")
    stderr_console.print()
    stderr_console.print("[bold]Next steps:[/]")
    stderr_console.print(f"  cd {escape(shlex.quote(name))}")
    stderr_console.print("  cp .env.example .env  [dim]# then fill in your API keys[/]")
    stderr_console.print(f"  uv sync && {_NEXT_STEP_DOCTOR_COMMAND}", soft_wrap=True)
    stderr_console.print(f"  {_next_step_run_command(cfg.template)}", soft_wrap=True)
    stderr_console.print(
        "[dim]The full post-scaffold command catalog stays in "
        "`easycat init --list-templates --json` (next_step_commands).[/]",
        soft_wrap=True,
    )


__all__: list[str] = ["init"]
