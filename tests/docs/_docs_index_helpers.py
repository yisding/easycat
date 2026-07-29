"""Guards for the top-level documentation map."""

# ruff: noqa: F401

import re
import shlex
from pathlib import Path
from urllib.parse import unquote

from easycat.cli._app import (
    _DOCS_COMMAND_NOTE,
    _DOCS_LINKS,
    _DOCS_ONBOARDING_GUARD_COMMANDS,
    _docs_entries,
)
from scripts._justfile import just_guard_recipes
from tests._command_hints import (
    command_hint_problems as _shared_command_hint_problems,
)
from tests._command_hints import (
    command_hint_variants as _shared_command_hint_variants,
)
from tests._command_hints import (
    documented_command_lines as _documented_command_lines,
)
from tests._command_hints import (
    documented_commands as _documented_commands,
)
from tests._markdown import github_markdown_heading_anchors

REPO_ROOT = Path(__file__).resolve().parents[2]

LINK_RE = re.compile(r"\[[^\]]+\]\((?P<target>[^)\n]+)\)")

CODE_SPAN_RE = re.compile(r"`([^`]+)`")

ANGLE_PLACEHOLDER_RE = re.compile(r"<[^>\s]+>")

EXAMPLE_README_ROW_RE = re.compile(
    r"^\| \[(?P<name>[^\]]+\.py)\]\((?P<link>[^)]+\.py)\) "
    r"\| (?P<use_when>[^|]+) "
    r"\| `(?P<run>[^`]+)` "
    r"\| (?P<install>[^|]+) "
    r"\| (?P<env>[^|]+) \|$"
)

ONBOARDING_GUARD_COMMANDS = _DOCS_ONBOARDING_GUARD_COMMANDS

RAW_ONBOARDING_GUARD_COMMANDS = tuple(guard.command for guard in just_guard_recipes(REPO_ROOT))

DOCS_MAP_COMMANDS = ("uv run easycat docs", "uv run easycat docs --json")

AGENT_GUIDE_MACHINE_COMMANDS = (
    "uv run easycat doctor --json",
    "uv run easycat doctor --env-file .env --json",
    "uv run easycat explain json-schema",
    "uv run easycat bundles show PATH --json",
    "uv run easycat bundles export PATH --output DIR --json",
    "uv run easycat replay PATH --json",
    "uv run easycat validate quick",
    "uv run easycat validate quick --json",
    "uv run easycat validate contracts --json",
    "uv run easycat validate release --json",
    "uv run easycat validate report .easycat/validation/latest.json",
    "uv run easycat validate report .easycat/validation/latest.json --json",
)


def _root_relative_doc_links() -> set[str]:
    path = REPO_ROOT / "docs" / "README.md"
    links = {"docs/README.md"}
    for match in LINK_RE.finditer(path.read_text(encoding="utf-8")):
        raw_target = match.group("target")
        target_path, sep, fragment = raw_target.partition("#")
        if target_path.startswith(("http://", "https://")):
            continue
        resolved = (path.parent / target_path).resolve()
        rel = resolved.relative_to(REPO_ROOT).as_posix()
        if raw_target.endswith("/") and not rel.endswith("/"):
            rel += "/"
        if sep:
            rel = f"{rel}#{fragment}"
        links.add(rel)
    return links


def _route_target_text(route: str) -> str:
    path = REPO_ROOT / route.split("#", 1)[0].rstrip("/")
    if path.is_dir():
        path = path / "README.md"
    return path.read_text(encoding="utf-8")


def _root_path_chooser_command_spans() -> tuple[str, ...]:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    section = readme.split("## Choose Your Path", 1)[1].split(
        "## Learn the pipeline from scratch", 1
    )[0]
    return tuple(
        match.group(1)
        for match in CODE_SPAN_RE.finditer(section)
        if match.group(1).startswith(("uv ", "easycat ", "just ", "docker "))
    )


def _cli_docs_command_hint_problems(entries: list[dict[str, object]]) -> list[str]:
    return _shared_command_hint_problems(entries, repo_root=REPO_ROOT)


def _command_hint_variants(command: str) -> set[str]:
    return _shared_command_hint_variants(command)


def _reference_section_field_names(text: str, heading: str) -> set[str]:
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^- `([A-Za-z_][A-Za-z0-9_]*)`", section, flags=re.MULTILINE))
