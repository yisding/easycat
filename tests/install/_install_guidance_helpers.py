"""Static guards for optional-extra install guidance."""

from __future__ import annotations

# ruff: noqa: F401
import ast
import re
import tomllib
from pathlib import Path

from easycat.cli.diagnose._codes import META_ENTRIES
from scripts._justfile import just_recipe_names
from tests._command_hints import command_hint_problems, documented_commands
from tests._pytest_targets import pytest_target_problems

REPO_ROOT = Path(__file__).resolve().parents[2]

TEXT_SUFFIXES = {
    ".conf",
    ".html",
    ".md",
    ".py",
    ".rst",
    ".service",
    ".sh",
    ".toml",
    ".txt",
    ".yml",
    ".yaml",
}

GUIDANCE_TARGETS = (
    REPO_ROOT / "src" / "easycat",
    REPO_ROOT / "docs",
    REPO_ROOT / "examples",
    REPO_ROOT / "README.md",
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "AGENTS.md",
    REPO_ROOT / "CLAUDE.md",
)

READER_GUIDANCE_TARGETS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "docs",
    REPO_ROOT / "examples",
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "AGENTS.md",
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "src" / "easycat" / "cli" / "scaffold" / "templates",
)

STALE_INSTALL_PATTERNS = (
    (
        "pip install easycat extra",
        re.compile(r"\bpip install\s+[`'\"]?easycat\["),
    ),
    (
        "editable local extra install",
        re.compile(r"\b(?:uv\s+)?pip\s+install\s+-e\s+[`'\"]?\.\["),
    ),
    (
        "unquoted uv add easycat extra",
        re.compile(r"\buv add\s+easycat\["),
    ),
    (
        "bare install easycat extra",
        re.compile(r"\binstall\s+easycat\[", re.IGNORECASE),
    ),
)

UV_EXTRA_RE = re.compile(r"--extra\s+(?P<extra>[A-Za-z0-9_.-]+)")

EASYCAT_EXTRA_RE = re.compile(r"easycat\[(?P<extras>[^\]]+)\]")

REPO_UV_SYNC_EXTRA_COMMAND_RE = re.compile(r"\buv sync\b(?P<args>[^\n`|;)]*--extra[^\n`|;)]*)")

REPO_UV_SYNC_PYTHON_COMMAND_RE = re.compile(r"\buv sync\b(?P<args>[^\n`|;)]*--python[^\n`|;)]*)")

MARKDOWN_PREREQS_RE = re.compile(
    r"^## Prerequisites\n(?P<body>.*?)(?=^## |\Z)",
    re.MULTILINE | re.DOTALL,
)

PROVIDER_EXTRA_BY_ENV_VAR = {
    "DEEPGRAM_API_KEY": "--extra deepgram",
    "ELEVENLABS_API_KEY": "--extra elevenlabs",
}

CODE_SPAN_RE = re.compile(r"`([^`]+)`")

GUIDE_JUST_COMMAND_RE = re.compile(r"\bjust\s+(?P<recipe>[A-Za-z0-9_-]+)\b")

SILERO_TORCH_REQUIRED_RE = re.compile(
    r"silero[^\n.]{0,120}\b(?:requires|needs|required)\b[^\n.]{0,80}\btorch\b"
    r"|\btorch\b[^\n.]{0,80}\b(?:required|needed)\b[^\n.]{0,120}\bsilero\b"
    r"|\buv\s+pip\s+install\s+torch\b",
    re.IGNORECASE,
)


def _guide_pytest_commands(command_section: str) -> list[str]:
    commands = [
        command.strip()
        for command in CODE_SPAN_RE.findall(command_section)
        if command.strip().startswith("uv run pytest")
    ]

    for line in command_section.splitlines():
        command = line.strip()
        if not command.startswith("uv run pytest"):
            continue
        command = re.split(r"\s+#", command, maxsplit=1)[0].rstrip()
        commands.append(command)

    return commands


def _agent_guide_command_sections() -> dict[str, str]:
    return {
        "AGENTS.md": (REPO_ROOT / "AGENTS.md")
        .read_text(encoding="utf-8")
        .split("## Build, Test, and Development Commands", 1)[1]
        .split("## ", 1)[0],
        "CLAUDE.md": (REPO_ROOT / "CLAUDE.md")
        .read_text(encoding="utf-8")
        .split("## Commands", 1)[1]
        .split("## Architecture", 1)[0],
    }


AGENT_GUIDE_SOURCE_PATH_SECTIONS = {
    "AGENTS.md": ("## Project Structure & Module Organization", "## Build, Test"),
    "CLAUDE.md": ("## Architecture", "## Session Lifecycle"),
    # The architecture explanation moved out of CLAUDE.md; keep its source
    # paths honest with the same guard. Related Pages holds only doc links.
    "docs/architecture.md": ("# EasyCat Architecture", "## Related Pages"),
}

REPO_REL_PATH_PREFIXES = ("src/", "tests/", "docs/", "examples/", "plan/", "scripts/")

SOURCE_REL_PATH_PREFIXES = (
    "cli/",
    "config/",
    "debug/",
    "debugger/",
    "integrations/",
    "models/",
    "runtime/",
    "session/",
    "stages/",
    "stt/",
    "telephony/",
    "transports/",
    "tts/",
    "validation/",
    "vad/",
)

BRIDGE_DISPLAY_NAMES = {
    "GenericWorkflowBridge": "your own async workflow",
    "LangChainBridge": "LangChain",
    "LangGraphBridge": "LangGraph",
    "LlamaAgentsBridge": "LlamaAgents",
    "OpenAIAgentsBridge": "OpenAI Agents SDK",
    "PydanticAIBridge": "PydanticAI",
    "RemoteResponsesAPIBridge": "Remote Responses API",
}


def _iter_text_files(targets: tuple[Path, ...]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        if target.is_file():
            files.append(target)
            continue
        if target.is_dir():
            files.extend(path for path in target.rglob("*") if path.suffix in TEXT_SUFFIXES)
    return sorted(files)


def _iter_guidance_files() -> list[Path]:
    return _iter_text_files(GUIDANCE_TARGETS)


def _iter_reader_guidance_files() -> list[Path]:
    return _iter_text_files(READER_GUIDANCE_TARGETS)


def _known_extras() -> set[str]:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return set(pyproject["project"]["optional-dependencies"])


def _looks_like_placeholder(extra: str) -> bool:
    return any(char in extra for char in "<>{}$") or extra in {"...", "NAME"}


def _normalize_extra(extra: str) -> str:
    return extra.strip().rstrip(".,;:")


def _extract_markdown_section(text: str, start_heading: str, end_heading: str) -> tuple[str, int]:
    start = text.index(start_heading)
    try:
        end = text.index(end_heading, start + len(start_heading))
    except ValueError:
        end = len(text)
    return text[start:end], text.count("\n", 0, start) + 1


def _clean_code_span_path(code_span: str) -> str:
    return code_span.strip().strip(".,;:()[]")


def _readme_cli_section() -> str:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    return readme.split("## CLI", 1)[1].split("## Current capabilities", 1)[0]


def _source_path_candidates_for_agent_guide(
    filename: str,
    line: str,
    code_span: str,
) -> list[Path]:
    path_text = _clean_code_span_path(code_span)
    if not path_text or any(char.isspace() for char in path_text):
        return []
    if "/" not in path_text and not path_text.endswith(".py"):
        return []

    if path_text.startswith(REPO_REL_PATH_PREFIXES):
        return [REPO_ROOT / path_text]

    source_root = REPO_ROOT / "src" / "easycat"
    if path_text.startswith(SOURCE_REL_PATH_PREFIXES):
        return [source_root / path_text]

    if filename == "CLAUDE.md" and line.startswith("  - ") and path_text.endswith(".py"):
        return [source_root / "session" / path_text]

    if filename == "CLAUDE.md" and "Agent bridges" in line and path_text.endswith(".py"):
        return [source_root / "integrations" / "agents" / path_text]

    if path_text.endswith(".py"):
        return [source_root / path_text]

    return []
