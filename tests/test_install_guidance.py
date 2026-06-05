"""Static guards for optional-extra install guidance."""

from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

from tests._justfile import just_recipe_names

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
AGENT_GUIDE_SOURCE_PATH_SECTIONS = {
    "AGENTS.md": ("## Project Structure & Module Organization", "## Build, Test"),
    "CLAUDE.md": ("## Architecture", "## Session Lifecycle"),
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


def _iter_guidance_files() -> list[Path]:
    files: list[Path] = []
    for target in GUIDANCE_TARGETS:
        if target.is_file():
            files.append(target)
            continue
        if target.is_dir():
            files.extend(path for path in target.rglob("*") if path.suffix in TEXT_SUFFIXES)
    return sorted(files)


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
        "setup, `uv sync --extra ...`: " + "; ".join(stale)
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

    sync_index = readme.index("uv sync --extra quickstart --group dev")
    key_index = readme.index("OPENAI_API_KEY")
    doctor_index = readme.index("uv run easycat doctor")

    assert sync_index < key_index < doctor_index


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
        f"uv sync --extra {extra}"
        for extra in expected_extras
        if f"uv sync --extra {extra}" not in optional_block
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
    """``quickstart`` already includes common local extras; avoid redundant setup."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]
    bundled_extras = ("rnnoise", "smart-turn")
    for extra in bundled_extras:
        assert set(extras[extra]).issubset(set(extras["quickstart"]))

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


def test_agent_guide_command_examples_are_current() -> None:
    just_recipes = just_recipe_names(REPO_ROOT)
    command_sections = {
        "AGENTS.md": (REPO_ROOT / "AGENTS.md")
        .read_text(encoding="utf-8")
        .split("## Build, Test, and Development Commands", 1)[1]
        .split("## ", 1)[0],
        "CLAUDE.md": (REPO_ROOT / "CLAUDE.md")
        .read_text(encoding="utf-8")
        .split("## Commands", 1)[1]
        .split("## ", 1)[0],
    }

    for filename, command_section in command_sections.items():
        assert "just check" in command_section
        assert "just validate-quick" in command_section
        assert "uv run easycat validate quick" in command_section
        assert "tests/test_metrics.py" not in command_section, filename

    stale_recipes: list[str] = []
    for filename, command_section in command_sections.items():
        for match in GUIDE_JUST_COMMAND_RE.finditer(command_section):
            recipe = match.group("recipe")
            if recipe not in just_recipes:
                stale_recipes.append(f"{filename}: just {recipe}")

    stale_paths: list[str] = []
    for filename, command_section in command_sections.items():
        for match in re.finditer(r"uv run pytest\s+(?P<target>tests/\S+)", command_section):
            path_text = match.group("target").split("::", 1)[0].strip("`.,:;")
            if not (REPO_ROOT / path_text).exists():
                stale_paths.append(f"{filename}: {path_text}")

    assert not stale_recipes, "Agent guide just examples point at missing recipes: " + ", ".join(
        stale_recipes
    )
    assert not stale_paths, "Agent guide pytest examples point at missing paths: " + ", ".join(
        stale_paths
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
