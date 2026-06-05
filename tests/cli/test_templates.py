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

from easycat.cli.scaffold._schema import InitConfig, available_templates
from easycat.cli.scaffold.init import _render_text, _substitutions, _templates_root

REPO_ROOT = Path(__file__).resolve().parents[2]

# ``agent.py`` line budget per template (counts *all* lines including blanks).
_LINE_BUDGETS: dict[str, int] = {
    "openai-agents": 25,
    "pydantic-ai": 22,
    "text-chat": 18,
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
_VOICE_TEMPLATES: tuple[str, ...] = ("openai-agents", "pydantic-ai")


@pytest.fixture
def templates() -> list[str]:
    return available_templates()


def test_catalog_is_nonempty(templates: list[str]) -> None:
    assert len(templates) >= 3
    for required in ("openai-agents", "pydantic-ai", "text-chat"):
        assert required in templates, f"missing template: {required}"


def _template_dir(name: str) -> Path:
    return _templates_root() / name


def _uses_run_easyconfig_mic(source: str) -> bool:
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
            and first_arg.func.attr == "mic"
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
    for fname in _REQUIRED_FILES:
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


@pytest.mark.parametrize("name", ["openai-agents", "pydantic-ai"])
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
    assert "uv run easycat doctor" in test_plan
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
    assert "uv run easycat doctor" in readme


@pytest.mark.parametrize("name", _VOICE_TEMPLATES)
def test_voice_templates_use_canonical_mic_quickstart_shape(name: str) -> None:
    agent = (_template_dir(name) / "agent.py").read_text(encoding="utf-8")
    assert _uses_run_easyconfig_mic(agent), f"{name}/agent.py must call run(EasyConfig.mic(...))"

    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    assert "EasyConfig.mic(" in readme or "EasyConfig.mic(...)" in readme


def test_text_chat_readme_points_voice_upgrade_to_mic_preset() -> None:
    readme = (_template_dir("text-chat") / "README.md").read_text(encoding="utf-8")
    assert "EasyConfig.mic(agent=agent)" in readme
    assert "EasyConfig(agent=agent)" not in readme


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


def test_pydantic_ai_readme_does_not_reference_missing_workflow_template() -> None:
    readme = (_template_dir("pydantic-ai") / "README.md").read_text(encoding="utf-8")
    assert "pydantic-ai-workflow" not in readme


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
