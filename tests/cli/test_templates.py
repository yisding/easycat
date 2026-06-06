"""Static checks that every shipped template is valid and within budget.

These tests guard the promise that ``easycat init <template>`` always
produces a runnable project.  They catch:

* ``agent.py`` exceeding its line budget
* Missing required files (``pyproject.toml``, ``.env.example``, README)
* README missing the required sections (Install, Configure, Run, Check,
  Next steps)
* README missing the environment preflight for templates that need OpenAI keys
* ``pyproject.toml`` failing to pin the ``easycat`` extra the template
  advertises
* Templated ``agent.py`` failing to parse with Python's AST after
  substitution of realistic values

The tests do NOT actually run ``uv sync`` or execute the agents — that
belongs in the end-to-end suite.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

from easycat.cli.diagnose.doctor import _parse_env_file
from easycat.cli.scaffold._schema import InitConfig, available_templates
from easycat.cli.scaffold.init import (
    _TEMPLATE_CATALOG,
    _available_template_catalog,
    _render_text,
    _substitutions,
    _templates_root,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# ``agent.py`` line budget per template (counts *all* lines including blanks).
_LINE_BUDGETS: dict[str, int] = {
    "openai-agents": 15,
    "pydantic-ai": 12,
    "pydantic-ai-workflow": 15,
    "text-chat": 8,
    "twilio-phone": 15,
    "webrtc-browser": 12,
}

_EXTRA_TEMPLATE_FILES: dict[str, tuple[str, ...]] = {
    "twilio-phone": ("server.py",),
}

_REQUIRED_FILES: tuple[str, ...] = (
    "agent.py",
    "pyproject.toml",
    "README.md",
    ".env.example",
    ".gitignore",
)

_README_SECTIONS: tuple[str, ...] = (
    "## Install",
    "## Configure",
    "## Run",
    "## Check",
    "## Next steps",
)
_VOICE_TEMPLATE_PRESETS: dict[str, str] = {
    "openai-agents": "mic",
    "pydantic-ai": "mic",
    "pydantic-ai-workflow": "mic",
    "webrtc-browser": "browser",
}
_GITIGNORE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".venv/",
    "__pycache__/",
    "*.pyc",
    ".ruff_cache/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".coverage",
    "htmlcov/",
    "dist/",
    "build/",
    ".easycat/",
)


@pytest.fixture
def templates() -> list[str]:
    return available_templates()


def test_catalog_is_nonempty(templates: list[str]) -> None:
    assert len(templates) >= 5
    for required in (
        "openai-agents",
        "pydantic-ai",
        "pydantic-ai-workflow",
        "text-chat",
        "twilio-phone",
        "webrtc-browser",
    ):
        assert required in templates, f"missing template: {required}"


def test_template_catalog_metadata_covers_available_templates(templates: list[str]) -> None:
    missing = sorted(set(templates) - set(_TEMPLATE_CATALOG))
    stale = sorted(set(_TEMPLATE_CATALOG) - set(templates))

    assert not missing, "Template catalog missing metadata for: " + ", ".join(missing)
    assert not stale, "Template catalog references missing templates: " + ", ".join(stale)

    for name in templates:
        entry = _TEMPLATE_CATALOG[name]
        assert "name" not in entry
        for key in ("mode", "transport", "framework", "description"):
            assert entry[key], f"{name} catalog entry missing {key}"

    emitted = {entry["name"]: entry for entry in _available_template_catalog()}
    assert set(emitted) == set(templates)
    assert all(entry["name"] == name for name, entry in emitted.items())


def _template_dir(name: str) -> Path:
    return _templates_root() / name


def _render_env_example(name: str, cfg: InitConfig) -> str:
    mapping = _substitutions(cfg, project_name="demo")
    source = (_template_dir(name) / ".env.example").read_text(encoding="utf-8")
    rendered = _render_text(source, mapping)
    for placeholder in mapping:
        assert f"${placeholder}" not in rendered
    return rendered


def _uses_run_easyconfig_preset(source: str, preset: str) -> bool:
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "run":
            continue
        if not node.args:
            continue
        first_arg = node.args[0]
        if (
            isinstance(first_arg, ast.Call)
            and isinstance(first_arg.func, ast.Attribute)
            and first_arg.func.attr == preset
            and isinstance(first_arg.func.value, ast.Name)
            and first_arg.func.value.id == "EasyConfig"
        ):
            return True

    return False


def _uses_async_with_create_text_session(source: str) -> bool:
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncWith):
            continue
        for item in node.items:
            expr = item.context_expr
            if (
                isinstance(expr, ast.Call)
                and isinstance(expr.func, ast.Name)
                and expr.func.id == "create_text_session"
            ):
                return True

    return False


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_required_files_present(name: str) -> None:
    d = _template_dir(name)
    for fname in (*_REQUIRED_FILES, *_EXTRA_TEMPLATE_FILES.get(name, ())):
        assert (d / fname).is_file(), f"{name}/{fname} missing"


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_agent_py_within_budget(name: str) -> None:
    budget = _LINE_BUDGETS[name]
    agent = _template_dir(name) / "agent.py"
    lines = agent.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= budget, f"{name}/agent.py has {len(lines)} lines, budget is {budget}"


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_agent_py_renders_and_parses(name: str) -> None:
    """Substitute realistic values and assert the result is valid Python."""
    cfg = InitConfig(
        template=name,
        agent_name="Support",
        agent_instructions="Help the user with billing.",
    )
    mapping = _substitutions(cfg, project_name="demo")
    agent_src = (_template_dir(name) / "agent.py").read_text(encoding="utf-8")
    rendered = _render_text(agent_src, mapping)
    assert "$AGENT_NAME" not in rendered
    assert "$AGENT_INSTRUCTIONS" not in rendered
    ast.parse(rendered)  # raises on syntax error


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_template_python_files_render_and_parse(name: str) -> None:
    """All top-level template Python files must survive substitution."""
    cfg = InitConfig(
        template=name,
        agent_name="Support",
        agent_instructions="Help the user with billing.",
    )
    mapping = _substitutions(cfg, project_name="demo")
    for py_file in _template_dir(name).glob("*.py"):
        rendered = _render_text(py_file.read_text(encoding="utf-8"), mapping)
        ast.parse(rendered, filename=str(py_file))


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_agent_py_escapes_string_literal_substitutions(name: str) -> None:
    """Quotes, backslashes, and newlines in agent text must stay valid Python."""
    cfg = InitConfig(
        template=name,
        agent_name='Support "A\\B"',
        agent_instructions='Line one\\path\nLine two says "hi"',
    )
    mapping = _substitutions(cfg, project_name="demo")
    agent_src = (_template_dir(name) / "agent.py").read_text(encoding="utf-8")
    rendered = _render_text(agent_src, mapping)
    ast.parse(rendered)


@pytest.mark.parametrize(
    "name",
    ["openai-agents", "pydantic-ai", "pydantic-ai-workflow", "webrtc-browser"],
)
def test_agent_py_escapes_provider_shortcut_substitutions(name: str) -> None:
    cfg = InitConfig(
        template=name,
        stt='openai/"bad',
        tts="openai/path\\voice",
    )
    mapping = _substitutions(cfg, project_name="demo")
    agent_src = (_template_dir(name) / "agent.py").read_text(encoding="utf-8")
    rendered = _render_text(agent_src, mapping)
    ast.parse(rendered)


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_readme_has_required_sections(name: str) -> None:
    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    for section in _README_SECTIONS:
        assert section in readme, f"{name}/README.md missing section: {section}"


def test_cli_test_plan_documents_template_readme_contract() -> None:
    test_plan = (REPO_ROOT / "tests" / "cli" / "TEST_PLANS.md").read_text(encoding="utf-8")
    section_names = [section.removeprefix("## ") for section in _README_SECTIONS]

    assert "four required sections" not in test_plan
    assert "five required sections" in test_plan
    assert "uv run easycat doctor --env-file .env" in test_plan
    for section in section_names:
        assert section in test_plan


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_readme_has_local_syntax_check(name: str) -> None:
    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    assert "uv run python -m py_compile agent.py" in readme


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_readme_has_doctor_preflight_when_template_needs_openai_key(name: str) -> None:
    env_example = (_template_dir(name) / ".env.example").read_text(encoding="utf-8")
    if "OPENAI_API_KEY" not in env_example:
        pytest.skip(f"{name} does not require an OpenAI key")

    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    assert "uv run easycat doctor --env-file .env" in readme
    assert "uv run --env-file .env easycat doctor" not in readme
    assert "\nuv run easycat doctor\n" not in readme
    assert "Run `easycat doctor`" not in readme


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_readme_run_command_loads_env_file(name: str) -> None:
    """Readers who just filled ``.env`` should run with that file loaded."""
    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    run_section = readme.split("## Run", 1)[1].split("## Check", 1)[0]
    primary_run = run_section.split("Or export", 1)[0]

    assert "uv run --env-file .env" in primary_run
    assert "uv run python agent.py" not in primary_run


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_template_readme_next_steps_point_to_docs_command(name: str) -> None:
    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    next_steps = readme.split("## Next steps", 1)[1]

    assert "uv run easycat docs" in next_steps


@pytest.mark.parametrize("name", sorted(_VOICE_TEMPLATE_PRESETS))
def test_voice_templates_use_canonical_preset_shape(name: str) -> None:
    preset = _VOICE_TEMPLATE_PRESETS[name]
    agent = (_template_dir(name) / "agent.py").read_text(encoding="utf-8")
    assert _uses_run_easyconfig_preset(agent, preset), (
        f"{name}/agent.py must call run(EasyConfig.{preset}(...))"
    )

    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    assert f"EasyConfig.{preset}(" in readme or f"EasyConfig.{preset}(...)" in readme


def test_text_chat_readme_points_voice_upgrade_to_mic_preset() -> None:
    readme = (_template_dir("text-chat") / "README.md").read_text(encoding="utf-8")
    assert "EasyConfig.mic(agent=agent)" in readme
    assert "EasyConfig(agent=agent)" not in readme
    assert "uv run easycat init my-voice-agent --template openai-agents" in readme
    assert "`easycat init my-voice-agent --template openai-agents`" not in readme


def test_text_chat_template_uses_public_session_lifecycle() -> None:
    agent = (_template_dir("text-chat") / "agent.py").read_text(encoding="utf-8")
    assert _uses_async_with_create_text_session(agent)


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_template_debug_guidance_points_to_public_inspect_cli(name: str) -> None:
    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    assert "~/.cache/easycat" not in readme
    assert "RunBundle journal" not in readme
    assert ".easycat/journals/" in readme
    assert "uv run easycat inspect .easycat/journals/<session_id>.sqlite" in readme


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_pyproject_pins_easycat_with_extras(name: str) -> None:
    """Every template's pyproject.toml declares an easycat extras dep."""
    pyproject = (_template_dir(name) / "pyproject.toml").read_text(encoding="utf-8")
    assert "easycat[" in pyproject, f"{name}/pyproject.toml must pin easycat[...]"
    # The generated pyproject uses $PROJECT_NAME — assert the literal is
    # present pre-substitution so rendering is the only path that sets
    # the project name.
    assert "$PROJECT_NAME" in pyproject


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_env_example_mentions_openai(name: str) -> None:
    """Every template today needs at least ``OPENAI_API_KEY`` by default."""
    env_example = (_template_dir(name) / ".env.example").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" in env_example


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_env_example_renders_for_doctor_env_file(name: str, tmp_path: Path) -> None:
    """Generated env examples must stay parseable by ``easycat doctor --env-file``."""
    rendered = _render_env_example(name, InitConfig(template=name))
    env_file = tmp_path / f"{name}.env"
    env_file.write_text(rendered, encoding="utf-8")

    parsed = _parse_env_file(env_file)

    assert parsed["OPENAI_API_KEY"] == "sk-your-key-here"


@pytest.mark.parametrize("name", sorted(_VOICE_TEMPLATE_PRESETS))
def test_voice_env_example_renders_selected_provider_keys(name: str, tmp_path: Path) -> None:
    cfg = InitConfig(
        template=name,
        stt="deepgram/flux",
        tts="elevenlabs/eleven_flash_v2_5",
    )
    rendered = _render_env_example(name, cfg)
    env_file = tmp_path / f"{name}.env"
    env_file.write_text(rendered, encoding="utf-8")

    parsed = _parse_env_file(env_file)

    assert parsed["OPENAI_API_KEY"] == "sk-your-key-here"
    assert parsed["DEEPGRAM_API_KEY"] == ""
    assert parsed["ELEVENLABS_API_KEY"] == ""


def test_pydantic_ai_readme_points_to_workflow_template() -> None:
    readme = (_template_dir("pydantic-ai") / "README.md").read_text(encoding="utf-8")
    assert "pydantic-ai-workflow" in readme
    assert "uv run easycat init my-workflow --template pydantic-ai-workflow" in readme


def test_pydantic_ai_template_beta_pin_matches_project_extra() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject["project"]["optional-dependencies"]["pydantic-ai-v2-beta"]
    pins = [dep for dep in deps if dep.startswith("pydantic-ai==")]
    assert len(pins) == 1

    _, version = pins[0].split("==", 1)
    readme = (_template_dir("pydantic-ai") / "README.md").read_text(encoding="utf-8")
    assert f"=={version}" in readme


def test_openai_agents_deepgram_swap_mentions_extra_key_and_sync() -> None:
    readme = (_template_dir("openai-agents") / "README.md").read_text(encoding="utf-8")
    env_example = (_template_dir("openai-agents") / ".env.example").read_text(encoding="utf-8")

    for text in (readme, env_example):
        assert "deepgram" in text
        assert "DEEPGRAM_API_KEY" in text
    assert "pyproject.toml" in readme
    assert "uv sync" in readme
    assert "pyproject.toml" in env_example
    assert "uv sync" in env_example


def test_no_placeholder_leak_in_non_templated_files() -> None:
    """``.gitignore`` is never templated and should contain no ``$VAR``."""
    for name in _LINE_BUDGETS:
        gi = (_template_dir(name) / ".gitignore").read_text(encoding="utf-8")
        assert "$" not in gi, f"{name}/.gitignore contains an unintended placeholder"


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_template_gitignore_covers_local_artifacts(name: str) -> None:
    """Generated projects should not invite local env/cache artifacts into git."""
    patterns = (_template_dir(name) / ".gitignore").read_text(encoding="utf-8").splitlines()

    for pattern in _GITIGNORE_PATTERNS:
        assert pattern in patterns, f"{name}/.gitignore missing {pattern!r}"
