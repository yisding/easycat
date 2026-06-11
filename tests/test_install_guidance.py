"""Static guards for optional-extra install guidance."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

from easycat.cli.diagnose._codes import META_ENTRIES
from scripts._justfile import just_recipe_names
from tests._command_hints import command_hint_problems, documented_commands
from tests._pytest_targets import pytest_target_problems

REPO_ROOT = Path(__file__).resolve().parents[1]
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


def test_optional_extra_guidance_uses_current_uv_commands() -> None:
    """Keep onboarding hints aligned for package users and repo-local developers."""
    stale: list[str] = []

    for path in _iter_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for label, pattern in STALE_INSTALL_PATTERNS:
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                stale.append(f"{rel}:{line}: {label}")

    assert not stale, (
        "Optional-extra guidance should use `uv add 'easycat[...]'` and, for repo-local "
        "setup, `uv sync --extra ... --group dev`: " + "; ".join(stale)
    )


def test_repo_local_uv_sync_extra_guidance_keeps_dev_group() -> None:
    """Repo-local optional-extra setup should not drop development tools."""
    stale: list[str] = []

    for path in _iter_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for match in REPO_UV_SYNC_EXTRA_COMMAND_RE.finditer(text):
            command = match.group(0).strip().rstrip(".,")
            if "--group dev" not in command:
                line = text.count("\n", 0, match.start()) + 1
                stale.append(f"{rel}:{line}: {command}")

    assert not stale, "Repo-local uv sync extra commands missing --group dev:\n" + "\n".join(stale)


def test_repo_local_uv_sync_python_guidance_keeps_dev_group() -> None:
    """Repo-local Python-version sync hints should still install dev tools."""
    stale: list[str] = []

    for path in _iter_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for match in REPO_UV_SYNC_PYTHON_COMMAND_RE.finditer(text):
            command = match.group(0).strip().rstrip(".,")
            if "--group dev" not in command:
                line = text.count("\n", 0, match.start()) + 1
                stale.append(f"{rel}:{line}: {command}")

    assert not stale, "Repo-local uv sync --python commands missing --group dev:\n" + "\n".join(
        stale
    )


def test_optional_extra_guidance_references_known_extras() -> None:
    """Catch typoed extras in source/doc install hints before users copy them."""
    known = _known_extras()
    unknown: list[str] = []

    for path in _iter_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()

        for match in UV_EXTRA_RE.finditer(text):
            extra = _normalize_extra(match.group("extra"))
            if extra not in known:
                line = text.count("\n", 0, match.start()) + 1
                unknown.append(f"{rel}:{line}: unknown --extra {extra!r}")

        for match in EASYCAT_EXTRA_RE.finditer(text):
            for extra in (_normalize_extra(part) for part in match.group("extras").split(",")):
                if not extra or _looks_like_placeholder(extra):
                    continue
                if extra not in known:
                    line = text.count("\n", 0, match.start()) + 1
                    unknown.append(f"{rel}:{line}: unknown easycat extra {extra!r}")

    assert not unknown, "Unknown EasyCat optional extras in install guidance:\n" + "\n".join(
        unknown
    )


def test_teaching_ladder_prerequisites_run_doctor_after_setup() -> None:
    """The teaching overview should send readers through the first-run preflight."""
    readme = (REPO_ROOT / "docs" / "teaching" / "README.md").read_text(encoding="utf-8")
    prerequisites = readme.split("## Prerequisites", 1)[1].split("## Conventions", 1)[0]

    local_index = prerequisites.index("uv sync --extra local --group dev")
    sync_index = prerequisites.index("uv sync --extra quickstart --group dev")
    key_index = prerequisites.index("OPENAI_API_KEY")
    doctor_index = prerequisites.index("uv run easycat doctor")

    assert local_index < sync_index < key_index < doctor_index


def test_teaching_chapter_key_prerequisites_run_doctor() -> None:
    """Self-contained chapter READMEs with API keys should repeat the preflight."""
    missing: list[str] = []
    teaching_root = REPO_ROOT / "docs" / "teaching"

    for path in sorted(teaching_root.glob("[0-9][0-9]-*/README.md")):
        text = path.read_text(encoding="utf-8")
        match = MARKDOWN_PREREQS_RE.search(text)
        if match is None:
            continue
        section = match.group("body")
        if "API_KEY" in section and "uv run easycat doctor" not in section:
            missing.append(path.relative_to(REPO_ROOT).as_posix())

    assert not missing, "Teaching chapter prerequisites missing doctor preflight:\n" + "\n".join(
        missing
    )


def test_readme_optional_dependency_list_has_copyable_install_commands() -> None:
    """The optional-dependency list should expose every non-meta repo extra."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    optional_block = readme.split("Optional dependencies you may need depending on", 1)[1].split(
        "## CLI", 1
    )[0]
    expected_extras = sorted(_known_extras() - {"all", "quickstart"})

    missing_commands = [
        f"uv sync --extra {extra} --group dev"
        for extra in expected_extras
        if f"uv sync --extra {extra} --group dev" not in optional_block
    ]

    assert not missing_commands, "README optional dependency list missing: " + ", ".join(
        missing_commands
    )
    assert "uv pip install krisp_audio" in optional_block


def test_teaching_provider_key_setup_names_required_extras() -> None:
    """Provider-key setup snippets should include the matching optional extra."""
    missing: list[str] = []
    teaching_root = REPO_ROOT / "docs" / "teaching"

    for path in sorted(teaching_root.rglob("*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        doc = ast.get_docstring(module) or ""
        if "Dependencies:" not in doc:
            continue
        rel = path.relative_to(REPO_ROOT)
        for env_var, extra in PROVIDER_EXTRA_BY_ENV_VAR.items():
            if env_var in doc and extra not in doc:
                missing.append(f"{rel}: {env_var} setup missing {extra}")

    readme_paths = sorted({*teaching_root.rglob("README.md"), teaching_root / "README.md"})
    for path in readme_paths:
        text = path.read_text(encoding="utf-8")
        match = MARKDOWN_PREREQS_RE.search(text)
        if match is None:
            continue
        section = match.group("body")
        rel = path.relative_to(REPO_ROOT)
        for env_var, extra in PROVIDER_EXTRA_BY_ENV_VAR.items():
            if env_var in section and extra not in section:
                missing.append(f"{rel}: {env_var} prerequisites missing {extra}")

    assert not missing, "Teaching setup docs missing provider extras:\n" + "\n".join(missing)


def test_quickstart_guidance_does_not_readd_bundled_extras() -> None:
    """``quickstart`` already includes several extras; avoid redundant setup."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]
    quickstart_deps = set(extras["quickstart"])
    bundled_extras = tuple(
        sorted(
            name
            for name, deps in extras.items()
            if name not in {"all", "quickstart"} and deps and set(deps).issubset(quickstart_deps)
        )
    )
    assert {
        "local",
        "openai",
        "openai-agents",
        "rnnoise",
        "silero-vad",
        "smart-turn",
    }.issubset(bundled_extras)

    redundant: list[str] = []
    extra_pattern = "|".join(re.escape(extra) for extra in bundled_extras)
    pattern = re.compile(rf"--extra\s+quickstart[^\n`|]*--extra\s+(?:{extra_pattern})")
    for path in _iter_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            redundant.append(f"{rel}:{line}")

    assert not redundant, (
        "Guidance should not re-add extras that `quickstart` already bundles: "
        + "; ".join(redundant)
    )


def test_silero_guidance_uses_bundled_onnx_not_torch() -> None:
    """Silero install docs should not send newcomers to PyTorch."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    silero_deps = pyproject["project"]["optional-dependencies"]["silero-vad"]
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    stale: list[str] = []

    assert any(dep.startswith("onnxruntime") for dep in silero_deps)
    assert not any(dep.startswith("torch") for dep in silero_deps)
    assert "no torch required" in readme

    for path in _iter_reader_guidance_files():
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(REPO_ROOT).as_posix()
        for match in SILERO_TORCH_REQUIRED_RE.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            stale.append(f"{rel}:{line}: {match.group(0).strip()}")

    assert not stale, "Silero guidance should use bundled ONNX, not torch:\n" + "\n".join(stale)


def test_reader_guidance_lets_easyconfig_read_openai_env_key() -> None:
    """Reader-facing EasyConfig snippets should rely on OPENAI_API_KEY setup."""
    exceptions = {
        # create_app(api_key=...) intentionally supports injection without
        # mutating process env, so this example must pass the key explicitly.
        REPO_ROOT / "examples" / "twilio_app.py",
    }
    stale: list[str] = []

    for path in _iter_reader_guidance_files():
        if path in exceptions:
            continue
        text = path.read_text(encoding="utf-8")
        if "openai_api_key=" in text:
            stale.append(path.relative_to(REPO_ROOT).as_posix())

    assert not stale, (
        "Reader-facing EasyConfig snippets should let EasyConfig read OPENAI_API_KEY:\n"
        + "\n".join(stale)
    )


def test_docs_json_guidance_points_to_schema_contract() -> None:
    """Automation route-map hints should also teach the JSON envelope contract."""
    missing: list[str] = []

    for path in _iter_reader_guidance_files():
        text = path.read_text(encoding="utf-8")
        if "easycat docs --json" not in text:
            continue
        if "easycat explain json-schema" not in text:
            missing.append(path.relative_to(REPO_ROOT).as_posix())

    assert not missing, (
        "`easycat docs --json` guidance should also point scripts/coding agents "
        "to `easycat explain json-schema`:\n" + "\n".join(missing)
    )


def test_env_file_doctor_guidance_points_to_json_variant() -> None:
    """Docs that mention ``.env`` doctor checks should also show the parseable form."""
    missing: list[str] = []

    for path in _iter_reader_guidance_files():
        text = path.read_text(encoding="utf-8")
        if "easycat doctor --env-file .env" not in text:
            continue
        if "easycat doctor --env-file .env --json" not in text:
            missing.append(path.relative_to(REPO_ROOT).as_posix())

    assert not missing, (
        "`.env` doctor guidance should also expose the machine-readable variant:\n"
        + "\n".join(missing)
    )


def test_template_list_guidance_points_to_catalog_json() -> None:
    """Template comparison guidance should expose the machine-readable catalog too."""
    missing: list[str] = []

    for path in _iter_reader_guidance_files():
        text = path.read_text(encoding="utf-8")
        if "easycat init --list-templates" not in text:
            continue
        if "copyable create/preflight/check/fix/docs/json-schema/run commands" not in text:
            continue
        if "easycat init --list-templates --json" not in text:
            missing.append(path.relative_to(REPO_ROOT).as_posix())

    assert not missing, (
        "Template comparison guidance with copyable commands should also point "
        "scripts/coding agents to `easycat init --list-templates --json`:\n" + "\n".join(missing)
    )


def test_readme_cli_explain_examples_are_copyable() -> None:
    """``easycat explain`` requires a code or --list; the README should show one."""
    cli_section = _readme_cli_section()

    assert not re.search(r"(?m)^easycat explain\s+#", cli_section)
    assert "easycat explain E102" in cli_section
    assert "easycat explain json-schema" in cli_section
    assert "easycat explain --list" in cli_section
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    normalized_readme = re.sub(r"\s+", " ", readme)
    assert "standard `--json` envelope" in normalized_readme
    assert "command-specific fields" in normalized_readme
    assert "`entries`, `commands`, `catalog`" in normalized_readme
    assert "`command_note`" in normalized_readme
    assert "`available_audience_filters`" in normalized_readme
    assert "`audience_alias_note`" in normalized_readme
    assert "`base_requirement`, `create_command`, `repo_create_command`" in normalized_readme
    assert "`next_step_commands`" in normalized_readme
    assert "`pyproject_name`, `run_command`" in normalized_readme
    assert "`run_command`, `check_command`, `fix_command`, `environment`, `checks`" in (
        normalized_readme
    )
    assert "`fix_command`, `environment`, `checks`, `validation`" in normalized_readme
    assert "`source_path`, and `fidelity_effective`" in normalized_readme
    assert (
        "Replace uppercase or angle-bracket placeholders in command hints, such as `PATH` "
        "or `<session_id>`"
    ) in normalized_readme


def test_readme_cli_command_examples_are_locally_valid() -> None:
    cli_section = _readme_cli_section()
    commands = documented_commands(
        cli_section,
        prefixes=("easycat ", "uv run easycat "),
    )

    problems = command_hint_problems(
        [
            {
                "label": "README.md CLI",
                "path": "README.md#cli",
                "audience": "app builders",
                "description": "Root README CLI command examples.",
                "commands": commands,
            }
        ],
        repo_root=REPO_ROOT,
    )

    assert commands
    assert not problems, "README.md CLI command examples are stale:\n" + "\n".join(problems)


def test_readme_json_guidance_covers_schema_command_families() -> None:
    """README automation guidance should route agents to each JSON command family."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    normalized_readme = re.sub(r"\s+", " ", readme)
    schema_body = META_ENTRIES["json-schema"].body

    command_family_mentions = {
        "easycat docs --json": "docs route map",
        "easycat docs --audience learners --json": "docs route map",
        "easycat init --list-templates --json": "template catalog",
        "easycat init NAME --json": "scaffold output",
        "easycat doctor --json": "doctor environment/checks output",
        "easycat validate quick --json": "validation quick/contracts/release/report output",
        "easycat validate contracts --json": ("validation quick/contracts/release/report output"),
        "easycat validate release --json": ("validation quick/contracts/release/report output"),
        "easycat validate report PATH --json": (
            "validation quick/contracts/release/report output"
        ),
        "easycat bundles list --json": "bundle list/show/export",
        "easycat bundles show PATH --json": "bundle list/show/export",
        "easycat bundles export PATH --output DIR --json": "bundle list/show/export",
        "easycat inspect PATH --json": "inspect",
        "easycat replay PATH --json": "replay",
    }

    for schema_command, readme_phrase in command_family_mentions.items():
        assert schema_command in schema_body
        assert readme_phrase in normalized_readme
    assert "`audience_filter`" in normalized_readme
    assert "`available_audiences`" in normalized_readme
    assert "`available_audience_filters`" in normalized_readme
    assert "`audience_alias_note`" in normalized_readme


def test_readme_cli_debug_json_examples_are_copyable() -> None:
    """Debug CLI commands should include machine-readable support handoffs."""
    cli_section = _readme_cli_section()

    for command in (
        "easycat bundles list --json",
        "easycat bundles show PATH --json",
        "easycat bundles export PATH --output DIR --json",
        "easycat inspect PATH --json",
        "easycat replay PATH --json",
    ):
        assert command in cli_section
    assert "machine-readable bundle list" in cli_section
    assert "machine-readable bundle/journal summary" in cli_section
    assert "context-pack metadata" in cli_section
    assert "machine-readable replay summary" in cli_section


def test_readme_cli_validate_examples_are_copyable() -> None:
    """Bare ``easycat validate`` shows help; the README should show useful subcommands."""
    cli_section = _readme_cli_section()
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    validation_doc = (REPO_ROOT / "docs" / "validation.md").read_text(encoding="utf-8")

    assert not re.search(r"(?m)^easycat validate\s+#", cli_section)
    assert "easycat validate quick" in cli_section
    assert "easycat validate quick --json" in cli_section
    assert "easycat validate contracts" in cli_section
    assert "easycat validate contracts --json" in cli_section
    assert "easycat validate release" in cli_section
    assert "easycat validate release --json" in cli_section
    assert "easycat validate report .easycat/validation/latest.json" in cli_section
    assert "easycat validate report .easycat/validation/latest.json --json" in cli_section
    assert "uv run easycat validate quick --json" in validation_doc
    assert "uv run easycat validate contracts --json" in validation_doc
    assert "uv run easycat validate release --json" in validation_doc
    assert "uv run easycat validate report .easycat/validation/latest.json --json" in (
        validation_doc
    )
    assert "easycat validate report PATH" not in readme
    assert "easycat validate report PATH" not in validation_doc


def test_readme_cli_doctor_documents_env_file_option() -> None:
    """``easycat doctor`` should show the direct .env path for scaffold users."""
    cli_section = _readme_cli_section()
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    normalized_readme = re.sub(r"\s+", " ", readme)

    assert "easycat doctor --env-file .env" in cli_section
    assert "easycat doctor --json" in cli_section
    assert "environment/check rows without Rich formatting" in normalized_readme


def test_cli_init_examples_name_target_directory() -> None:
    """``easycat init`` requires NAME unless listing templates."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    normalized_readme = re.sub(r"\s+", " ", readme)
    cli_section = _readme_cli_section()
    production_chapter = (
        REPO_ROOT / "docs" / "teaching" / "15-operate-in-production" / "README.md"
    ).read_text(encoding="utf-8")
    normalized_production_chapter = re.sub(r"\s+", " ", production_chapter)

    assert not re.search(r"(?m)^easycat init\s+#", cli_section)
    assert "easycat init my-agent" in cli_section
    assert "easycat init --list-templates" in cli_section
    assert "easycat init --list-templates --json" in cli_section
    assert "`easycat init my-agent` scaffolds" in readme
    assert "`easycat init --list-templates` shows" in readme
    assert "base `easycat[...]` package requirement and extras" in normalized_readme
    assert "required environment variables" in normalized_readme
    assert "optional environment knobs" in normalized_readme
    assert "generated files" in normalized_readme
    assert "copyable create/preflight/check/fix/docs/json-schema/run commands" in (
        normalized_readme
    )
    assert "`uv run easycat init my-agent`" in production_chapter
    assert "`uv run easycat init --list-templates`" in production_chapter
    assert "base `easycat[...]` package requirements and extras" in normalized_production_chapter
    assert "required environment variables" in normalized_production_chapter
    assert "optional environment knobs" in normalized_production_chapter
    assert "generated files" in normalized_production_chapter
    assert "copyable create/preflight/check/fix/docs/json-schema/run commands" in (
        normalized_production_chapter
    )
    assert "**`uv run easycat init`**" not in production_chapter


def test_agent_guides_use_current_live_marker_name() -> None:
    stale: list[str] = []

    for filename in ("AGENTS.md", "CLAUDE.md"):
        text = (REPO_ROOT / filename).read_text(encoding="utf-8")
        if "@pytest.mark.integration`" in text or "@pytest.mark.integration " in text:
            stale.append(filename)
        assert "@pytest.mark.integration_live" in text

    assert not stale, "Agent guides should use integration_live, not integration: " + ", ".join(
        stale
    )


def test_agent_guide_pytest_command_extractor_handles_guide_formats() -> None:
    command_section = """
- `uv run pytest tests/cli/test_app.py::test_docs_command`.

```bash
uv run pytest tests/cli/test_app.py::test_docs_command_json  # Comment
```
"""

    assert _guide_pytest_commands(command_section) == [
        "uv run pytest tests/cli/test_app.py::test_docs_command",
        "uv run pytest tests/cli/test_app.py::test_docs_command_json",
    ]


def test_agent_guide_easycat_command_validator_checks_nested_commands() -> None:
    command_section = """
- `uv run easycat validate not-a-lane`.

```bash
uv run easycat docs --audience time-travelers  # Comment
```
"""
    commands = documented_commands(
        command_section,
        prefixes=("uv run easycat ", "easycat "),
    )

    problems = command_hint_problems(
        [
            {
                "label": "Broken agent guide",
                "path": "AGENTS.md",
                "audience": "coding agents",
                "description": "Regression fixture for guide command validation.",
                "commands": commands,
            }
        ],
        repo_root=REPO_ROOT,
    )

    assert "Broken agent guide: unknown easycat validate command not-a-lane" in problems
    assert "Broken agent guide: unknown docs audience hint time-travelers" in problems


def test_agent_guide_command_examples_are_current() -> None:
    just_recipes = just_recipe_names(REPO_ROOT)
    command_sections = _agent_guide_command_sections()

    for filename, command_section in command_sections.items():
        assert "raw docs/onboarding guard commands" in command_section, filename
        assert "[`CONTRIBUTING.md`](CONTRIBUTING.md#the-development-loop)" in (command_section), (
            filename
        )
        assert "just check" in command_section
        assert "just validate-quick" in command_section
        assert "uv run easycat docs" in command_section
        assert "uv run easycat docs --json" in command_section
        assert "uv run easycat doctor --json" in command_section
        assert "uv run easycat doctor --env-file .env --json" in command_section
        assert "uv run easycat explain json-schema" in command_section
        assert "uv run easycat bundles show PATH --json" in command_section
        assert "uv run easycat bundles export PATH --output DIR --json" in command_section
        assert "uv run easycat replay PATH --json" in command_section
        assert "uv run easycat validate quick" in command_section
        assert "uv run easycat validate report .easycat/validation/latest.json" in (
            command_section
        )
        assert "uv run easycat validate report .easycat/validation/latest.json --json" in (
            command_section
        )
        assert "tests/test_metrics.py" not in command_section, filename

    agents_commands = command_sections["AGENTS.md"]
    assert "uv run easycat docs --audience coding-agents" in agents_commands
    assert "uv run easycat docs --audience coding-agents --json" in agents_commands
    assert 'uv run easycat docs --audience "coding agents"' not in agents_commands
    assert "architecture and maintenance" in agents_commands
    assert "examples, teaching, validation, and operations" not in agents_commands

    claude_commands = command_sections["CLAUDE.md"]
    assert "uv run easycat docs --audience maintainers" in claude_commands
    assert "uv run easycat docs --audience maintainers --json" in claude_commands

    stale_recipes: list[str] = []
    for filename, command_section in command_sections.items():
        for match in GUIDE_JUST_COMMAND_RE.finditer(command_section):
            recipe = match.group("recipe")
            if recipe not in just_recipes:
                stale_recipes.append(f"{filename}: just {recipe}")

    stale_pytest_targets: list[str] = []
    for filename, command_section in command_sections.items():
        for command in _guide_pytest_commands(command_section):
            stale_pytest_targets.extend(
                pytest_target_problems(command, repo_root=REPO_ROOT, label=filename)
            )

    stale_easycat_commands: list[str] = []
    for filename, command_section in command_sections.items():
        commands = documented_commands(
            command_section,
            prefixes=("uv run easycat ", "easycat "),
        )
        stale_easycat_commands.extend(
            command_hint_problems(
                [
                    {
                        "label": filename,
                        "path": filename,
                        "audience": "coding agents",
                        "description": "Agent guide command examples.",
                        "commands": commands,
                    }
                ],
                repo_root=REPO_ROOT,
            )
        )

    assert not stale_recipes, "Agent guide just examples point at missing recipes: " + ", ".join(
        stale_recipes
    )
    assert not stale_pytest_targets, "Agent guide pytest examples are stale:\n" + "\n".join(
        stale_pytest_targets
    )
    assert not stale_easycat_commands, "Agent guide easycat examples are stale:\n" + "\n".join(
        stale_easycat_commands
    )


def test_agent_guide_command_hints_are_locally_valid() -> None:
    stale_commands: list[str] = []

    for filename, command_section in _agent_guide_command_sections().items():
        commands = tuple(
            command
            for command in documented_commands(
                command_section,
                prefixes=("just", "uv sync ", "uv run ", "uvx "),
            )
            if command == "just" or command.startswith(("just ", "uv sync ", "uv run ", "uvx "))
        )
        assert commands, filename
        stale_commands.extend(
            command_hint_problems(
                [
                    {
                        "label": filename,
                        "path": filename,
                        "audience": "coding agents",
                        "description": "Agent guide build, test, docs, and validation commands.",
                        "commands": commands,
                    }
                ],
                repo_root=REPO_ROOT,
            )
        )

    assert not stale_commands, "Agent guide command hints are stale:\n" + "\n".join(stale_commands)


def test_agent_guides_preflight_credentialed_example_runs() -> None:
    stale: list[str] = []
    plain_doctor_re = re.compile(r"(?m)(?:^- `|^)uv run easycat doctor(?:`|\s+#|$)")

    for filename, command_section in _agent_guide_command_sections().items():
        doctor_match = plain_doctor_re.search(command_section)
        for command in (
            "uv run python examples/ws_server.py",
            "uv run python examples/webrtc_server.py",
        ):
            command_index = command_section.find(command)
            if command_index == -1:
                continue
            if doctor_match is None:
                stale.append(f"{filename}: missing plain doctor preflight before `{command}`")
                continue
            if doctor_match.start() > command_index:
                stale.append(f"{filename}: doctor preflight appears after `{command}`")

    assert not stale, (
        "Agent guide credentialed example commands should be preceded by "
        "`uv run easycat doctor`: " + "; ".join(stale)
    )


def test_agent_guides_reference_config_package_layout() -> None:
    assert (REPO_ROOT / "src" / "easycat" / "config").is_dir()
    assert not (REPO_ROOT / "src" / "easycat" / "config.py").exists()

    stale_mentions: list[str] = []
    for filename in ("AGENTS.md", "CLAUDE.md"):
        text = (REPO_ROOT / filename).read_text(encoding="utf-8")
        assert "`config/`" in text, filename
        if "`config.py`" in text:
            stale_mentions.append(filename)

    assert not stale_mentions, (
        "Agent guides should reference config/, not config.py: " + ", ".join(stale_mentions)
    )


def test_agent_guides_name_major_source_packages() -> None:
    """Keep first-contact maintainer maps aligned with major source packages."""
    for package_name in ("cli", "debugger", "vad", "validation"):
        assert (REPO_ROOT / "src" / "easycat" / package_name).is_dir()

    missing: list[str] = []
    for filename in ("AGENTS.md", "CLAUDE.md"):
        text = (REPO_ROOT / filename).read_text(encoding="utf-8")
        for package_name in ("cli", "debugger", "vad", "validation"):
            mention = f"`{package_name}/`"
            if mention not in text:
                missing.append(f"{filename}: {mention}")

    assert not missing, "Agent guides missing major source packages: " + ", ".join(missing)


def test_agent_guides_name_major_test_domains() -> None:
    """Source-package maps should point contributors to matching tests."""
    for package_name in ("cli", "debugger", "vad", "validation"):
        assert (REPO_ROOT / "tests" / package_name).is_dir()

    missing: list[str] = []
    for filename in ("AGENTS.md", "CLAUDE.md"):
        text = (REPO_ROOT / filename).read_text(encoding="utf-8")
        for package_name in ("cli", "debugger", "vad", "validation"):
            mention = f"`tests/{package_name}/`"
            if mention not in text:
                missing.append(f"{filename}: {mention}")

    assert not missing, "Agent guides missing test-domain packages: " + ", ".join(missing)


def test_agent_guide_source_path_mentions_exist() -> None:
    missing: list[str] = []

    for filename, (start_heading, end_heading) in AGENT_GUIDE_SOURCE_PATH_SECTIONS.items():
        text = (REPO_ROOT / filename).read_text(encoding="utf-8")
        section, start_line = _extract_markdown_section(text, start_heading, end_heading)
        for offset, line in enumerate(section.splitlines()):
            line_number = start_line + offset
            for match in CODE_SPAN_RE.finditer(line):
                path_text = _clean_code_span_path(match.group(1))
                candidates = _source_path_candidates_for_agent_guide(
                    filename,
                    line,
                    path_text,
                )
                if candidates and not any(path.exists() for path in candidates):
                    missing.append(f"{filename}:{line_number}: `{path_text}`")

    assert not missing, "Agent guide source path mentions are stale:\n" + "\n".join(missing)


def test_claude_overview_tracks_public_agent_bridges() -> None:
    from easycat.integrations import agents as agent_integrations

    bridge_names = {
        name
        for name in agent_integrations.__all__
        if name.endswith("Bridge") and name != "ExternalAgentBridge"
    }
    missing_display_map = sorted(bridge_names - set(BRIDGE_DISPLAY_NAMES))
    assert not missing_display_map, "CLAUDE.md bridge display map missing: " + ", ".join(
        missing_display_map
    )

    overview = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8").split("## Commands", 1)[0]
    missing_display_names = sorted(
        display_name
        for bridge_name, display_name in BRIDGE_DISPLAY_NAMES.items()
        if bridge_name in bridge_names and display_name not in overview
    )

    assert not missing_display_names, (
        "CLAUDE.md overview missing public bridge labels: " + ", ".join(missing_display_names)
    )


def test_claude_provider_registry_guidance_tracks_factory_names() -> None:
    from easycat.stt import factory as stt_factory
    from easycat.tts import factory as tts_factory

    key_patterns = (
        (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8").split("## Session Lifecycle", 1)[0]
    )

    assert hasattr(stt_factory, "_PROVIDER_TO_CONFIG")
    assert hasattr(tts_factory, "_PROVIDER_TO_CONFIG")
    assert tts_factory._PROVIDERS is tts_factory._PROVIDER_TO_CONFIG

    assert "stt/factory.py" in key_patterns
    assert "tts/factory.py" in key_patterns
    assert "_PROVIDER_TO_CONFIG" in key_patterns
    assert "back-compat alias" in key_patterns
    assert "_PROVIDERS" in key_patterns
