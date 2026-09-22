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
import re
import shlex
import tomllib
from pathlib import Path

import pytest

from easycat.cli._app import _available_docs_audience_filters
from easycat.cli.diagnose.doctor import _parse_env_file
from easycat.cli.scaffold._schema import (
    TEMPLATE_ARTIFACT_DIRECTORY_NAMES,
    InitConfig,
    available_templates,
)
from easycat.cli.scaffold.init import (
    _COPY_FILE_IGNORE,
    _COPY_FILE_PREFIX_IGNORE,
    _COPY_IGNORE,
    _COPY_PART_SUFFIX_IGNORE,
    _COPY_SUFFIX_IGNORE,
    _TEMPLATE_SPECS,
    _available_template_catalog,
    _base_requirement,
    _easycat_version_floor,
    _next_step_audience_docs_commands,
    _next_step_commands,
    _render_text,
    _substitutions,
    _template_file_names,
    _template_sources,
    _templates_root,
)
from tests._release_artifacts import (
    GENERATED_PROJECT_GITIGNORE_PATTERNS,
    SCAFFOLD_COPY_IGNORED_DIRECTORIES,
    SCAFFOLD_COPY_IGNORED_FILE_PREFIXES,
    SCAFFOLD_COPY_IGNORED_FILES,
    SCAFFOLD_COPY_IGNORED_PART_SUFFIXES,
    SCAFFOLD_COPY_IGNORED_SUFFIXES,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

# ``agent.py`` line budget per template (counts *all* lines including blanks).
# Every template ships the importable make_agent()/make_app()/make_config()
# shape with app-protecting generated tests (DX3).
_LINE_BUDGETS: dict[str, int] = {
    "openai-agents": 23,
    "provider": 25,
    "provider-stt": 25,
    "provider-tts": 25,
    "pydantic-ai": 28,
    "pydantic-ai-workflow": 46,
    "text-chat": 25,
    "twilio-phone": 13,
    "telnyx-phone": 13,
    "webrtc-browser": 25,
}

# Templates whose SDK-free logic lives in a separate tools.py — the static
# mutation guard and the offline-test guard both need to know which templates
# ship one, since ``import tools`` is only meaningful where tools.py exists.
_TOOLS_MODULE_TEMPLATES: frozenset[str] = frozenset(
    {
        "openai-agents",
        "pydantic-ai",
        "pydantic-ai-workflow",
        "twilio-phone",
        "telnyx-phone",
        "webrtc-browser",
    }
)

# The generated test's primary factory import per template — the static
# mutation guard (T4) requires each template's test to import at least this
# name, so renaming or deleting it fails the guard with no SDK installed.
_PRIMARY_FACTORY: dict[str, str] = {
    "openai-agents": "make_agent",
    "pydantic-ai": "make_agent",
    "pydantic-ai-workflow": "make_workflow",
    "text-chat": "make_agent",
    "twilio-phone": "make_agent",
    "telnyx-phone": "make_agent",
    "webrtc-browser": "make_agent",
    "provider": "make_agent",
    "provider-stt": "make_agent",
    "provider-tts": "make_agent",
}

# Line budgets for the SDK-free support modules the migrated templates ship.
_SUPPORT_FILE_BUDGETS: dict[str, dict[str, int]] = {
    "openai-agents": {"tools.py": 10},
    "pydantic-ai": {"tools.py": 8},
    "pydantic-ai-workflow": {"tools.py": 8},
    "twilio-phone": {"tools.py": 6},
    "telnyx-phone": {"tools.py": 6},
    "webrtc-browser": {"tools.py": 6},
}

_EXTRA_TEMPLATE_FILES: dict[str, tuple[str, ...]] = {
    "openai-agents": ("tools.py",),
    "pydantic-ai": ("tools.py",),
    "pydantic-ai-workflow": ("tools.py",),
    "provider": ("custom_vad.py", "test_custom_vad.py"),
    "provider-stt": ("custom_stt.py", "test_custom_stt.py"),
    "provider-tts": ("custom_tts.py", "test_custom_tts.py"),
    "twilio-phone": ("server.py", "tools.py"),
    "telnyx-phone": ("server.py", "tools.py"),
    "webrtc-browser": ("tools.py",),
}

# Per-template dev dependency groups; the provider package skeleton ships a
# conformance test, so it also pins pytest.
_TEMPLATE_DEV_GROUPS: dict[str, list[str]] = {
    "provider": ["ruff>=0.9", "pytest>=8", "pytest-asyncio>=0.24"],
    "provider-stt": ["ruff>=0.9", "pytest>=8", "pytest-asyncio>=0.24"],
    "provider-tts": ["ruff>=0.9", "pytest>=8", "pytest-asyncio>=0.24"],
}

_REQUIRED_FILES: tuple[str, ...] = (
    "agent.py",
    "pyproject.toml",
    "README.md",
    "AGENTS.md",
    "tests/test_agent.py",
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
    "pydantic-ai": "mic",
    "pydantic-ai-workflow": "mic",
    "webrtc-browser": "browser",
}
_CODE_SPAN_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")


@pytest.fixture
def templates() -> list[str]:
    return available_templates()


def test_catalog_is_nonempty(templates: list[str]) -> None:
    assert len(templates) >= 5
    for required in (
        "openai-agents",
        "provider",
        "provider-stt",
        "provider-tts",
        "pydantic-ai",
        "pydantic-ai-workflow",
        "text-chat",
        "twilio-phone",
        "telnyx-phone",
        "webrtc-browser",
    ):
        assert required in templates, f"missing template: {required}"


def test_template_catalog_metadata_covers_available_templates(templates: list[str]) -> None:
    missing = sorted(set(templates) - set(_TEMPLATE_SPECS))
    stale = sorted(set(_TEMPLATE_SPECS) - set(templates))

    assert not missing, "Template catalog missing metadata for: " + ", ".join(missing)
    assert not stale, "Template catalog references missing templates: " + ", ".join(stale)

    available_docs_audiences = set(_available_docs_audience_filters())
    for name in templates:
        spec = _TEMPLATE_SPECS[name]
        assert spec.mode, f"{name} catalog entry missing mode"
        assert spec.transport, f"{name} catalog entry missing transport"
        assert spec.framework, f"{name} catalog entry missing framework"
        assert spec.best_for, f"{name} catalog entry missing best_for"
        assert spec.required_env, f"{name} catalog entry missing required_env"
        assert spec.description, f"{name} catalog entry missing description"
        assert spec.base_extras, f"{name} catalog entry missing base_extras"
        assert spec.docs_audience in available_docs_audiences, (
            f"{name} catalog docs audience is not a live docs filter: {spec.docs_audience}"
        )
        env_example = (_template_dir(name) / ".env.example").read_text(encoding="utf-8")
        for env_var in spec.required_env:
            assert env_var.isupper(), f"{name} catalog env var is not uppercase: {env_var}"
            assert f"{env_var}=" in env_example, (
                f"{name} catalog required_env {env_var} missing from .env.example"
            )
        for env_var in spec.optional_env:
            assert env_var.isupper(), (
                f"{name} catalog optional env var is not uppercase: {env_var}"
            )
            assert env_var not in spec.required_env, (
                f"{name} catalog optional_env duplicates required_env: {env_var}"
            )
            assert f"{env_var}=" in env_example, (
                f"{name} catalog optional_env {env_var} missing from .env.example"
            )

    emitted = {entry["name"]: entry for entry in _available_template_catalog()}
    assert set(emitted) == set(templates)
    assert all(entry["name"] == name for name, entry in emitted.items())
    for name, entry in emitted.items():
        spec = _TEMPLATE_SPECS[name]
        assert entry["base_extras"] == spec.base_extras
        assert entry["base_requirement"] == _base_requirement(name)
        assert entry["files"] == _template_file_names(name)
        assert entry["next_step_commands"] == _next_step_commands(Path("my-agent"), name)
        assert entry["run_command"] == spec.run_command
        assert entry["check_command"]
        assert entry["fix_command"]
        assert entry["required_env"] == spec.required_env
        assert entry["optional_env"] == spec.optional_env


def test_template_specs_make_audio_capabilities_explicit() -> None:
    audio_templates = {
        name for name, spec in _TEMPLATE_SPECS.items() if spec.supports_audio_config
    }

    assert audio_templates == {
        "openai-agents",
        "pydantic-ai",
        "pydantic-ai-workflow",
        "twilio-phone",
        "telnyx-phone",
        "webrtc-browser",
    }
    assert {
        name: spec.expected_transport
        for name, spec in _TEMPLATE_SPECS.items()
        if spec.expected_transport is not None
    } == {
        "openai-agents": "local",
        "pydantic-ai": "local",
        "pydantic-ai-workflow": "local",
        "twilio-phone": "twilio",
        "telnyx-phone": "telnyx",
        "webrtc-browser": "webrtc",
    }
    # Provider-authoring packages have focused voice demos but intentionally
    # do not accept app-level provider overrides through the scaffold command.
    for name in ("provider", "provider-stt", "provider-tts"):
        assert _TEMPLATE_SPECS[name].mode == "voice"
        assert not _TEMPLATE_SPECS[name].supports_audio_config


def test_template_env_var_collector_reads_twilio_server_code() -> None:
    required, referenced = _template_code_env_vars("twilio-phone")

    assert required == {"OPENAI_API_KEY", "TWILIO_STREAM_URL", "TWILIO_AUTH_TOKEN"}
    assert "TWILIO_WS_PORT" in referenced
    assert "TWILIO_STREAM_TOKEN_SECRET" in referenced
    assert "TWILIO_MAX_SESSIONS" in referenced
    assert "TWILIO_START_TIMEOUT_S" in referenced
    assert "TRUST_PROXY_HEADERS" in referenced
    assert "TWILIO_WS_PORT" not in required
    assert "TWILIO_STREAM_TOKEN_SECRET" not in required
    assert "TWILIO_MAX_SESSIONS" not in required
    assert "TWILIO_START_TIMEOUT_S" not in required
    assert "TRUST_PROXY_HEADERS" not in required


def test_template_env_var_collector_reads_telnyx_server_code() -> None:
    required, referenced = _template_code_env_vars("telnyx-phone")

    assert required == {
        "OPENAI_API_KEY",
        "TELNYX_STREAM_URL",
        "TELNYX_API_KEY",
        "TELNYX_PUBLIC_KEY",
    }
    for name in (
        "TELNYX_WS_PORT",
        "TELNYX_STREAM_TOKEN_SECRET",
        "TELNYX_MAX_SESSIONS",
        "TELNYX_START_TIMEOUT_S",
    ):
        assert name in referenced
        assert name not in required


def test_scaffold_templates_keep_easyconfig_env_first_for_openai_key() -> None:
    """Templates preflight OPENAI_API_KEY but let EasyConfig consume the env var."""
    stale: list[str] = []

    for template in available_templates():
        for filename in _template_python_filenames(template):
            path = _template_dir(template) / filename
            source = path.read_text(encoding="utf-8")
            if "openai_api_key=" in source:
                stale.append(f"{template}/{filename}")

    assert not stale, "Scaffold templates should let EasyConfig read OPENAI_API_KEY: " + ", ".join(
        stale
    )


def test_twilio_scaffold_keeps_runtime_feedback_opt_in() -> None:
    source = (_template_dir("twilio-phone") / "server.py").read_text(encoding="utf-8")

    assert "WebSocketSessionRuntime(" in source
    assert "session_factory=build_session" in source
    assert "runtime_feedback=True" not in source
    assert "attach_runtime_feedback" not in source


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_template_catalog_env_covers_template_code(name: str) -> None:
    code_required, code_referenced = _template_code_env_vars(name)
    catalog_required = set(_TEMPLATE_SPECS[name].required_env)
    catalog_optional = set(_TEMPLATE_SPECS[name].optional_env)
    catalog_env = catalog_required | catalog_optional

    missing = sorted(code_referenced - catalog_env)
    missing_required = sorted(code_required - catalog_required)
    required_marked_optional = sorted(code_required & catalog_optional)

    assert not missing, f"{name} catalog missing env vars read by template code: {missing}"
    assert not missing_required, (
        f"{name} catalog required_env missing required template env vars: {missing_required}"
    )
    assert not required_marked_optional, (
        f"{name} catalog optional_env includes required vars: {required_marked_optional}"
    )


def _catalog_command_problems(entry: dict[str, object]) -> list[str]:
    name = str(entry["name"])
    files = set(entry["files"])
    next_step_commands = list(entry["next_step_commands"])
    problems: list[str] = []

    expected_create = ["easycat", "init", "my-agent", "--template", name]
    create_tokens = shlex.split(str(entry["create_command"]))
    repo_create_tokens = shlex.split(str(entry["repo_create_command"]))
    if create_tokens != expected_create:
        problems.append(f"{name}: create_command is not installed CLI form")
    if repo_create_tokens != ["uv", "run", *expected_create]:
        problems.append(f"{name}: repo_create_command is not repo-local CLI form")

    expected_prefix = [
        "cd my-agent",
        "cp .env.example .env",
        "uv sync",
        "uv run easycat doctor --env-file .env",
        "uv run easycat doctor --env-file .env --json",
    ]
    audience_docs, audience_docs_json = _next_step_audience_docs_commands(name)
    expected_middle = [
        str(entry["check_command"]),
        str(entry["fix_command"]),
        "uv run easycat docs",
        audience_docs,
        audience_docs_json,
        "uv run easycat docs --json",
        "uv run easycat explain json-schema",
    ]
    expected_sequence = expected_prefix + expected_middle + [str(entry["run_command"])]
    if next_step_commands != expected_sequence:
        problems.append(f"{name}: next_step_commands are not the canonical post-create sequence")

    check_tokens = shlex.split(str(entry["check_command"]))
    if check_tokens[:4] != ["uv", "run", "ruff", "check"]:
        problems.append(f"{name}: check_command is not a repo-local ruff check")
    for target in check_tokens[4:]:
        if target not in files:
            problems.append(f"{name}: check_command target {target} is not generated")

    fix_tokens = shlex.split(str(entry["fix_command"]))
    if fix_tokens[:5] != ["uv", "run", "ruff", "check", "--fix"]:
        problems.append(f"{name}: fix_command is not a repo-local ruff auto-fix command")
    for target in fix_tokens[5:]:
        if target not in files:
            problems.append(f"{name}: fix_command target {target} is not generated")
    if fix_tokens[:4] == check_tokens[:4] and fix_tokens[5:] != check_tokens[4:]:
        problems.append(f"{name}: fix_command targets do not match check_command targets")

    run_tokens = shlex.split(str(entry["run_command"]))
    if run_tokens[:4] != ["uv", "run", "--env-file", ".env"]:
        problems.append(f"{name}: run_command does not load .env through uv")
        return problems

    app_tokens = run_tokens[4:]
    if app_tokens[:1] == ["python"]:
        script = app_tokens[1] if len(app_tokens) > 1 else ""
        if script not in files:
            problems.append(
                f"{name}: run_command Python target {script or '<missing>'} is not generated"
            )
    elif app_tokens[:1] == ["uvicorn"]:
        target = app_tokens[1] if len(app_tokens) > 1 else ""
        module_name = target.partition(":")[0]
        module_file = f"{module_name}.py" if module_name else ""
        if module_file not in files:
            problems.append(
                f"{name}: run_command ASGI target {target or '<missing>'} is not generated"
            )
    else:
        command = app_tokens[0] if app_tokens else "<missing>"
        problems.append(f"{name}: run_command uses unsupported app command {command}")

    return problems


def test_template_catalog_commands_are_copyable_and_resolve() -> None:
    problems: list[str] = []

    for entry in _available_template_catalog():
        problems.extend(_catalog_command_problems(entry))

    assert not problems, "Template catalog command hints are stale:\n" + "\n".join(problems)


def test_template_catalog_command_validator_checks_generated_targets() -> None:
    broken_entry = {
        "name": "broken",
        "files": ("agent.py", "pyproject.toml"),
        "create_command": "uv run easycat init my-agent --template broken",
        "repo_create_command": "easycat init my-agent --template broken",
        "next_step_commands": ("cd my-agent", "uv sync"),
        "check_command": "uv run ruff check missing.py",
        "fix_command": "uv run ruff check --fix missing.py",
        "run_command": "uv run --env-file .env python missing.py",
    }

    problems = _catalog_command_problems(broken_entry)

    assert "broken: create_command is not installed CLI form" in problems
    assert "broken: repo_create_command is not repo-local CLI form" in problems
    assert "broken: next_step_commands are not the canonical post-create sequence" in problems
    assert "broken: check_command target missing.py is not generated" in problems
    assert "broken: fix_command target missing.py is not generated" in problems
    assert "broken: run_command Python target missing.py is not generated" in problems


def test_scaffold_dependency_floor_tracks_project_version() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert _easycat_version_floor() == pyproject["project"]["version"]


def test_provider_template_registers_and_selects_named_vad() -> None:
    cfg = InitConfig(template="provider")
    mapping = _substitutions(cfg, project_name="demo")
    template = _template_dir("provider")
    pyproject = tomllib.loads(
        _render_text((template / "pyproject.toml").read_text(encoding="utf-8"), mapping)
    )
    custom_vad = (template / "custom_vad.py").read_text(encoding="utf-8")
    agent = (template / "agent.py").read_text(encoding="utf-8")

    assert pyproject["project"]["entry-points"]["easycat.vad_providers"] == {
        "energy": "custom_vad:register"
    }
    assert pyproject["build-system"]["build-backend"] == "setuptools.build_meta"
    assert pyproject["tool"]["setuptools"]["py-modules"] == ["custom_vad"]
    assert "register_vad_provider(" in custom_vad
    assert 'vad="energy"' in agent


@pytest.mark.parametrize(
    ("template_name", "module_name", "entry_point_group", "provider_name", "role"),
    [
        ("provider-stt", "custom_stt", "easycat.stt_providers", "scripted", "stt"),
        ("provider-tts", "custom_tts", "easycat.tts_providers", "tone", "tts"),
    ],
)
def test_speech_provider_templates_ship_registration_and_authoring_contracts(
    template_name: str,
    module_name: str,
    entry_point_group: str,
    provider_name: str,
    role: str,
) -> None:
    mapping = _substitutions(InitConfig(template=template_name), project_name="demo")
    template = _template_dir(template_name)
    pyproject = tomllib.loads(
        _render_text((template / "pyproject.toml").read_text(encoding="utf-8"), mapping)
    )
    provider_source = (template / f"{module_name}.py").read_text(encoding="utf-8")
    test_source = (template / f"test_{module_name}.py").read_text(encoding="utf-8")
    readme = (template / "README.md").read_text(encoding="utf-8")

    assert pyproject["project"]["entry-points"][entry_point_group] == {
        provider_name: f"{module_name}:register"
    }
    assert pyproject["tool"]["setuptools"]["py-modules"] == [module_name]
    assert f"register_{role}_provider(" in provider_source
    assert 'capabilities=frozenset({"offline"})' in provider_source
    assert '"provider"' in provider_source
    assert '"model"' in provider_source
    assert '"api_version"' in provider_source
    assert '"sdk_version"' in provider_source
    assert f'{role}="{provider_name}"' in (template / "agent.py").read_text(encoding="utf-8")
    assert "ProviderContractSuite" in test_source
    assert "integration_live" in test_source
    assert "provider_custom" in test_source
    assert f"surface_{role}" in test_source
    assert "credential_env_var" in test_source
    pytest_config = pyproject["tool"]["pytest"]["ini_options"]
    assert pytest_config["addopts"] == '-m "not integration_live"'
    assert pytest_config["strict_markers"] is True
    assert any(marker.startswith("integration_live:") for marker in pytest_config["markers"])
    assert any(marker.startswith("provider_custom:") for marker in pytest_config["markers"])
    assert any(marker.startswith(f"surface_{role}:") for marker in pytest_config["markers"])
    assert f'uv run pytest -m "integration_live and provider_custom and surface_{role}"' in readme
    assert "LIVE TODO" in provider_source
    assert "LIVE TODO" in readme


def _template_dir(name: str) -> Path:
    return _templates_root() / name


def _render_env_example(name: str, cfg: InitConfig) -> str:
    mapping = _substitutions(cfg, project_name="demo")
    source = (_template_dir(name) / ".env.example").read_text(encoding="utf-8")
    rendered = _render_text(source, mapping)
    for placeholder in mapping:
        assert f"${placeholder}" not in rendered
    return rendered


def _env_names_in(rendered: str) -> set[str]:
    names: set[str] = set()
    for line in rendered.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        names.add(stripped.partition("=")[0])
    return names


def _template_python_filenames(name: str) -> list[str]:
    return sorted(path.name for path in _template_dir(name).glob("*.py"))


def _readme_command_hints(readme: str) -> set[str]:
    commands: set[str] = set()
    in_code_fence = False

    for line in readme.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence and stripped:
            commands.add(stripped)

    for span in _CODE_SPAN_RE.findall(readme):
        if span.startswith(("cp ", "uv ")):
            commands.add(span)

    return commands


def _literal_string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _env_var_literal_from_call(node: ast.Call) -> tuple[str, bool] | None:
    func = node.func
    if isinstance(func, ast.Name) and func.id == "require_env":
        required = True
    elif isinstance(func, ast.Attribute) and func.attr == "getenv":
        if not isinstance(func.value, ast.Name) or func.value.id != "os":
            return None
        required = False
    elif isinstance(func, ast.Attribute) and func.attr == "get":
        if not _is_os_environ(func.value):
            return None
        required = False
    else:
        return None

    if not node.args:
        return None
    name = _literal_string(node.args[0])
    if name is None:
        return None
    return name, required


def _template_code_env_vars(name: str) -> tuple[set[str], set[str]]:
    required: set[str] = set()
    referenced: set[str] = set()

    for filename in _template_python_filenames(name):
        path = _template_dir(name) / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                result = _env_var_literal_from_call(node)
                if result is None:
                    continue
                env_var, is_required = result
                referenced.add(env_var)
                if is_required:
                    required.add(env_var)
            elif isinstance(node, ast.Subscript) and _is_os_environ(node.value):
                name_literal = _literal_string(node.slice)
                if name_literal is not None:
                    referenced.add(name_literal)
                    required.add(name_literal)

    return required, referenced


def _builds_easyconfig_preset_and_runs_it(source: str, preset: str) -> bool:
    """The module builds ``EasyConfig.<preset>(...)`` and runs it under ``__main__``.

    Replaces the old ``_uses_run_easyconfig_preset`` (which required
    ``run(EasyConfig.<preset>(...))`` as one expression): the factory shape
    builds the config in ``make_config()`` and calls ``run(make_config())``
    under the ``__main__`` guard instead, so the preset call and the ``run``
    call are no longer the same expression.
    """
    tree = ast.parse(source)

    builds_preset = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == preset
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "EasyConfig"
        for node in ast.walk(tree)
    )
    if not builds_preset:
        return False

    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not _is_main_guard(node):
            continue
        assert isinstance(node, ast.If)
        for child in node.body:
            guarded.update(id(inner) for inner in ast.walk(child))

    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "run"
        and id(node) in guarded
        for node in ast.walk(tree)
    )


def _is_main_guard(node: ast.AST) -> bool:
    """True for ``if __name__ == "__main__":``."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _returns_call_to(func: ast.FunctionDef, callee: str) -> bool:
    return any(
        isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == callee
        for node in ast.walk(func)
    )


def _builds_voice_app_under_main_guard(source: str) -> bool:
    """``make_app()`` returns the ``VoiceApp``; only ``__main__`` runs it."""
    tree = ast.parse(source)

    builds = any(
        isinstance(node, ast.FunctionDef)
        and node.name == "make_app"
        and _returns_call_to(node, "VoiceApp")
        for node in ast.walk(tree)
    )
    if not builds:
        return False

    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not _is_main_guard(node):
            continue
        assert isinstance(node, ast.If)
        for child in node.body:
            guarded.update(id(inner) for inner in ast.walk(child))

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "run"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "local"
            and id(node) not in guarded
        ):
            return False

    return True


def _module_level_names(source: str) -> set[str]:
    """Top-level ``def``/``class``/assignment names defined by *source*."""
    names: set[str] = set()
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _names_imported_from(source: str, modules: set[str]) -> set[str]:
    """Names *source* takes from *modules*, by either import style.

    ``from tools import current_time`` and ``import tools`` +
    ``tools.current_time`` must both anchor the static mutation guard, or a
    test that patches at the module boundary would stop pinning the symbol.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    plain: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in modules:
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            # Only a plainly imported module counts, so a local variable that
            # happens to share a module's name is never mistaken for one.
            plain.update(
                alias.name
                for alias in node.names
                if alias.asname is None and alias.name in modules
            )

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in plain
        ):
            names.add(node.attr)
    return names


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


@pytest.mark.parametrize("name", sorted(_SUPPORT_FILE_BUDGETS))
def test_support_files_within_budget(name: str) -> None:
    """SDK-free support modules stay small enough for a beginner to read."""
    for filename, budget in _SUPPORT_FILE_BUDGETS[name].items():
        path = _template_dir(name) / filename
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) <= budget, (
            f"{name}/{filename} has {len(lines)} lines, budget is {budget}"
        )


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


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_readme_install_section_names_rendered_base_requirement(name: str) -> None:
    source = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    rendered = _render_text(source, _substitutions(InitConfig(template=name), "demo"))
    install_section = rendered.split("## Install", 1)[1].split("## Configure", 1)[0]

    assert "uv sync" in install_section
    assert _base_requirement(name) in install_section
    assert "pyproject.toml" in install_section
    assert "$EXTRAS" not in rendered


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_readme_has_local_lint_check(name: str) -> None:
    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    check_section = readme.split("## Check", 1)[1].split("## Next steps", 1)[0]
    python_filenames = " ".join(_template_python_filenames(name))
    expected_command = f"uv run ruff check {python_filenames}"
    expected_fix_command = f"uv run ruff check --fix {python_filenames}"

    assert expected_command in check_section
    assert expected_fix_command in check_section
    assert "uv run python -m py_compile" not in check_section


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_readme_has_doctor_preflight_when_template_needs_openai_key(name: str) -> None:
    env_example = (_template_dir(name) / ".env.example").read_text(encoding="utf-8")
    if "OPENAI_API_KEY" not in env_example:
        pytest.skip(f"{name} does not require an OpenAI key")

    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())
    assert "uv run easycat doctor --env-file .env" in readme
    assert "uv run easycat doctor --env-file .env --json" in readme
    assert "when a script or coding agent" not in normalized_readme
    assert "uv run --env-file .env easycat doctor" not in readme
    assert "\nuv run easycat doctor\n" not in readme
    assert "Run `easycat doctor`" not in readme


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_readme_documents_catalog_optional_env(name: str) -> None:
    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    optional_env = _TEMPLATE_SPECS[name].optional_env

    for env_var in optional_env:
        assert env_var in readme, f"{name}/README.md missing optional env {env_var}"


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_readme_run_command_loads_env_file(name: str) -> None:
    """Readers who just filled ``.env`` should run with that file loaded."""
    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    run_section = readme.split("## Run", 1)[1].split("## Check", 1)[0]
    primary_run = run_section.split("Or export", 1)[0]

    assert "uv run --env-file .env" in primary_run
    assert "uv run python agent.py" not in primary_run


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_readme_command_hints_match_scaffold_next_steps(name: str) -> None:
    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    commands = _readme_command_hints(readme)
    expected_commands = [
        command
        for command in _next_step_commands(Path("my-agent"), name)
        if not command.startswith("cd ")
    ]

    missing = [command for command in expected_commands if command not in commands]

    assert not missing, f"{name}/README.md missing scaffold command hints: {missing}"


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_readme_avoids_ad_hoc_env_export_recipes(name: str) -> None:
    """Generated READMEs should keep dotenv loading on the uv command path."""
    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")

    assert "export $(" not in readme
    assert "grep -v '^#' .env" not in readme
    assert "source .env" not in readme


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_template_readme_next_steps_point_to_docs_command(name: str) -> None:
    """Structural check only — exact narrative phrasing is not hard-locked here.

    Command validity is covered by same-file
    ``test_readme_command_hints_match_scaffold_next_steps`` and
    ``test_template_catalog_commands_are_copyable_and_resolve``.
    """
    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    next_steps = readme.split("## Next steps", 1)[1]
    normalized_next_steps = " ".join(next_steps.split())
    audience_docs, audience_docs_json = _next_step_audience_docs_commands(name)
    required_commands = (
        "uv run easycat docs",
        audience_docs,
        audience_docs_json,
        "uv run easycat docs --json",
        "uv run easycat init --list-templates",
        "uv run easycat init --list-templates --json",
        "uv run easycat explain json-schema",
    )

    for command in required_commands:
        assert command in next_steps
    spaced_audience = _TEMPLATE_SPECS[name].docs_audience.replace("-", " ")
    assert f'uv run easycat docs --audience "{spaced_audience}"' not in next_steps
    assert "when a script or coding agent" not in normalized_next_steps


def test_provider_templates_route_authors_to_provider_docs() -> None:
    for name in ("provider", "provider-stt", "provider-tts"):
        assert _TEMPLATE_SPECS[name].docs_audience == "provider-maintainers"
        next_steps = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
        assert "uv run easycat docs --audience provider-maintainers" in next_steps
        assert "uv run easycat docs --audience app-builders" not in next_steps

    for name in set(_TEMPLATE_SPECS) - {"provider", "provider-stt", "provider-tts"}:
        assert _TEMPLATE_SPECS[name].docs_audience == "app-builders"


@pytest.mark.parametrize("name", sorted(_VOICE_TEMPLATE_PRESETS))
def test_voice_templates_use_canonical_preset_shape(name: str) -> None:
    preset = _VOICE_TEMPLATE_PRESETS[name]
    agent = (_template_dir(name) / "agent.py").read_text(encoding="utf-8")
    assert _builds_easyconfig_preset_and_runs_it(agent, preset), (
        f"{name}/agent.py must build EasyConfig.{preset}(...) and run it under __main__"
    )

    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    assert f"EasyConfig.{preset}(" in readme or f"EasyConfig.{preset}(...)" in readme


def test_default_openai_agents_template_uses_voice_app_golden_path() -> None:
    agent = (_template_dir("openai-agents") / "agent.py").read_text(encoding="utf-8")
    assert _builds_voice_app_under_main_guard(agent)
    assert "EasyConfig" not in agent
    assert "def make_agent()" in agent
    assert "def make_app()" in agent
    assert 'if __name__ == "__main__":' in agent
    assert "from tools import current_time" in agent

    readme = (_template_dir("openai-agents") / "README.md").read_text(encoding="utf-8")
    assert "VoiceApp(...)" in readme


def test_openai_agents_debug_guidance_requires_record_to_for_bundles() -> None:
    readme = (_template_dir("openai-agents") / "README.md").read_text(encoding="utf-8")
    debug_section = readme.split("- **Debug a session:**", 1)[1].split(
        "- **Graduate to EasyConfig", 1
    )[0]

    assert "does not create a timestamped `RunBundle` by itself" in debug_section
    assert 'record_to=".easycat/runs"' in debug_section
    assert 'debug="full"' in debug_section


def test_text_chat_readme_points_voice_upgrade_to_mic_preset() -> None:
    readme = (_template_dir("text-chat") / "README.md").read_text(encoding="utf-8")
    assert "EasyConfig.mic(agent=make_agent())" in readme
    assert "EasyConfig(agent=make_agent())" not in readme
    assert "uv run easycat init my-voice-agent --template openai-agents" in readme
    assert "`easycat init my-voice-agent --template openai-agents`" not in readme


def test_text_chat_template_uses_public_session_lifecycle() -> None:
    agent = (_template_dir("text-chat") / "agent.py").read_text(encoding="utf-8")
    assert _uses_async_with_create_text_session(agent)


def test_text_chat_template_keeps_first_code_readable() -> None:
    agent = (_template_dir("text-chat") / "agent.py").read_text(encoding="utf-8")

    assert '"""Text-mode EasyCat agent for prompt iteration."""' in agent
    assert "import asyncio" in agent
    assert "def make_agent() -> Agent:" in agent
    assert "create_text_session(agent=make_agent())" in agent
    assert "create_text_session(agent=Agent(" not in agent
    assert "asyncio.run(chat())" in agent
    assert 'if __name__ == "__main__":' in agent


def test_pydantic_templates_keep_first_code_readable() -> None:
    single_agent = (_template_dir("pydantic-ai") / "agent.py").read_text(encoding="utf-8")
    workflow = (_template_dir("pydantic-ai-workflow") / "agent.py").read_text(encoding="utf-8")
    workflow_tools = (_template_dir("pydantic-ai-workflow") / "tools.py").read_text(
        encoding="utf-8"
    )

    assert "from tools import current_time" in single_agent
    assert "tools=[current_time]" in single_agent
    assert "\n\n@agent.tool_plain\n" not in single_agent

    assert "\n\nclass SupportWorkflow:\n" in workflow
    assert workflow.index("pick_specialist") < workflow.index("class SupportWorkflow")
    assert "key = pick_specialist(text)" in workflow
    # The router — the interesting, SDK-free logic — and the terms it keys
    # on live in tools.py, not in the module that imports pydantic_ai.
    assert "TECH_TERMS" not in workflow
    assert workflow_tools.index("TECH_TERMS") < workflow_tools.index("def pick_specialist")


_PYDANTIC_MODEL_FACTORIES: dict[str, tuple[str, ...]] = {
    "pydantic-ai": ("make_agent",),
    "pydantic-ai-workflow": ("make_specialists", "make_workflow"),
}


@pytest.mark.parametrize("name", sorted(_PYDANTIC_MODEL_FACTORIES))
def test_pydantic_templates_inject_the_model_so_offline_tests_need_no_key(name: str) -> None:
    """PydanticAI resolves the model *inside* ``Agent(...)``, not at run time.

    ``Agent("openai:gpt-4.1-mini", ...)`` raises ``openai.OpenAIError: Missing
    credentials`` whenever ``OPENAI_API_KEY`` is unset or empty, so a generated
    test that called the factory with no argument would fail a user's very
    first ``uv run pytest`` — the run this scaffold advertises as key-free.
    No lane in this repo installs ``pydantic_ai``, so the generated test's own
    ``importorskip`` hides that everywhere it could be observed: this static
    guard is the standing proof, until PR3's wheel lane executes it for real.
    """
    factories = _PYDANTIC_MODEL_FACTORIES[name]
    agent_py = (_template_dir(name) / "agent.py").read_text(encoding="utf-8")

    assert 'MODEL = "openai:' in agent_py, f"{name}/agent.py must name the model in a constant"
    assert 'Agent("openai:' not in agent_py, (
        f"{name}/agent.py builds an Agent from a model string that needs a key to resolve"
    )
    defined = {
        node.name: node for node in ast.parse(agent_py).body if isinstance(node, ast.FunctionDef)
    }
    for factory in factories:
        args = defined[factory].args
        assert [arg.arg for arg in args.args] == ["model"], (
            f"{name}/{factory}() must take an injectable model"
        )
        assert args.defaults and ast.unparse(args.defaults[0]) == "MODEL"

    test_py = (_template_dir(name) / "tests" / "test_agent.py").read_text(encoding="utf-8")
    assert "TestModel" in test_py
    for node in ast.walk(ast.parse(test_py)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in factories
        ):
            assert node.args or node.keywords, (
                f"{name}: the generated test calls {node.func.id}() with no model, so "
                "PydanticAI would need a real API key just to construct it"
            )


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_template_debug_guidance_points_to_public_inspect_cli(name: str) -> None:
    """Structural check only — exact narrative phrasing is not hard-locked here."""
    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    assert "~/.cache/easycat" not in readme
    assert "RunBundle journal" not in readme
    assert ".easycat/journals/" in readme
    assert "uv run easycat inspect .easycat/journals/<session_id>.sqlite" in readme
    assert 'record_to=".easycat/runs"' in readme


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_pyproject_pins_easycat_with_extras(name: str) -> None:
    """Every template's pyproject.toml declares an easycat extras dep."""
    pyproject = (_template_dir(name) / "pyproject.toml").read_text(encoding="utf-8")
    rendered = _render_text(pyproject, _substitutions(InitConfig(template=name), "demo"))
    parsed = tomllib.loads(rendered)

    assert "easycat[" in pyproject, f"{name}/pyproject.toml must pin easycat[...]"
    assert f"easycat[{','.join(_TEMPLATE_SPECS[name].base_extras)}]" in rendered
    assert "$EASYCAT_VERSION_FLOOR" in pyproject
    assert "$EASYCAT_VERSION_FLOOR" not in rendered
    assert _base_requirement(name) in rendered
    assert parsed["dependency-groups"]["dev"] == _TEMPLATE_DEV_GROUPS.get(
        name, ["pytest>=8", "ruff>=0.9"]
    )
    scaffold_metadata = parsed["tool"]["easycat"]["scaffold"]
    assert scaffold_metadata["template"] == name
    assert tuple(scaffold_metadata["required_env"]) == _TEMPLATE_SPECS[name].required_env
    assert tuple(scaffold_metadata["optional_env"]) == _TEMPLATE_SPECS[name].optional_env
    # The generated pyproject uses a normalized metadata name; README files keep
    # the display project name.
    assert "$PYPROJECT_NAME" in pyproject
    assert "$PROJECT_NAME" not in pyproject


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_env_example_mentions_openai(name: str) -> None:
    """Every template today needs at least ``OPENAI_API_KEY`` by default."""
    env_example = (_template_dir(name) / ".env.example").read_text(encoding="utf-8")
    assert "OPENAI_API_KEY" in env_example


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_env_example_renders_for_doctor_env_file(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generated env examples must stay parseable by ``easycat doctor --env-file``."""
    rendered = _render_env_example(name, InitConfig(template=name))
    env_file = tmp_path / f"{name}.env"
    env_file.write_text(rendered, encoding="utf-8")

    # Exported provider variables win over file defaults, so scrub them to
    # keep this direct parser probe deterministic.
    for env_name in _env_names_in(rendered):
        monkeypatch.delenv(env_name, raising=False)

    parsed = _parse_env_file(env_file, allowed_names=_env_names_in(rendered))

    assert parsed["OPENAI_API_KEY"] == "sk-your-key-here"


@pytest.mark.parametrize("name", sorted(_VOICE_TEMPLATE_PRESETS))
def test_voice_env_example_renders_selected_provider_keys(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = InitConfig(
        template=name,
        stt="deepgram/flux",
        tts="elevenlabs/eleven_flash_v2_5",
    )
    rendered = _render_env_example(name, cfg)
    env_file = tmp_path / f"{name}.env"
    env_file.write_text(rendered, encoding="utf-8")

    for env_name in _env_names_in(rendered):
        monkeypatch.delenv(env_name, raising=False)

    parsed = _parse_env_file(env_file, allowed_names=_env_names_in(rendered))

    assert parsed["OPENAI_API_KEY"] == "sk-your-key-here"
    assert parsed["DEEPGRAM_API_KEY"] == ""
    assert parsed["ELEVENLABS_API_KEY"] == ""


def test_scaffold_metadata_includes_selected_provider_requirements() -> None:
    cfg = InitConfig(
        template="openai-agents",
        stt="deepgram/flux",
        tts="elevenlabs/eleven_flash_v2_5",
    )
    source = (_template_dir("openai-agents") / "pyproject.toml").read_text(encoding="utf-8")
    rendered = _render_text(source, _substitutions(cfg, "demo"))
    parsed = tomllib.loads(rendered)

    assert parsed["tool"]["easycat"]["scaffold"] == {
        "template": "openai-agents",
        "required_env": ["OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"],
        "optional_env": [],
    }


def test_scaffold_metadata_serializes_optional_env_requirements() -> None:
    source = (_template_dir("webrtc-browser") / "pyproject.toml").read_text(encoding="utf-8")
    rendered = _render_text(
        source,
        _substitutions(InitConfig(template="webrtc-browser"), "demo"),
    )
    metadata = tomllib.loads(rendered)["tool"]["easycat"]["scaffold"]

    assert metadata["required_env"] == ["OPENAI_API_KEY"]
    assert metadata["optional_env"] == [
        "TURN_SERVER_URL",
        "TURN_USERNAME",
        "TURN_CREDENTIAL",
    ]


def test_pydantic_ai_readme_points_to_workflow_template() -> None:
    readme = (_template_dir("pydantic-ai") / "README.md").read_text(encoding="utf-8")
    assert "pydantic-ai-workflow" in readme
    assert "uv run easycat init my-workflow --template pydantic-ai-workflow" in readme


def test_pydantic_ai_template_v2_requirement_matches_project_extra() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = pyproject["project"]["optional-dependencies"]["pydantic-ai-v2"]
    specs = [dep for dep in deps if dep.startswith("pydantic-ai>=")]
    assert len(specs) == 1

    constraint = specs[0].removeprefix("pydantic-ai")
    readme = (_template_dir("pydantic-ai") / "README.md").read_text(encoding="utf-8")
    assert f"pydantic-ai[groq]{constraint}" in readme


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

    for pattern in GENERATED_PROJECT_GITIGNORE_PATTERNS:
        assert pattern in patterns, f"{name}/.gitignore missing {pattern!r}"


def test_template_copy_filter_omits_local_artifact_directories() -> None:
    """The scaffold copier should enforce the same local-artifact policy."""
    assert SCAFFOLD_COPY_IGNORED_DIRECTORIES <= _COPY_IGNORE


def test_template_copy_filter_omits_coverage_report_files() -> None:
    assert SCAFFOLD_COPY_IGNORED_FILES <= _COPY_FILE_IGNORE
    assert set(SCAFFOLD_COPY_IGNORED_FILE_PREFIXES) <= set(_COPY_FILE_PREFIX_IGNORE)


def test_template_copy_filter_omits_package_metadata_directories() -> None:
    assert set(SCAFFOLD_COPY_IGNORED_PART_SUFFIXES) <= set(_COPY_PART_SUFFIX_IGNORE)


def test_template_copy_filter_omits_compiled_bytecode_suffixes() -> None:
    expected_suffixes = set(SCAFFOLD_COPY_IGNORED_SUFFIXES)
    assert {".pyc", ".pyo"} <= expected_suffixes <= _COPY_SUFFIX_IGNORE


def test_template_copy_filter_omits_local_secret_suffixes() -> None:
    expected_suffixes = set(SCAFFOLD_COPY_IGNORED_SUFFIXES)
    assert {".pem", ".key"} <= expected_suffixes <= _COPY_SUFFIX_IGNORE


def test_available_templates_omits_top_level_artifact_directories(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    templates_root = tmp_path / "templates"
    templates_root.mkdir()
    (templates_root / "demo").mkdir()
    for name in TEMPLATE_ARTIFACT_DIRECTORY_NAMES:
        (templates_root / name).mkdir()

    fake_schema_file = tmp_path / "_schema.py"
    fake_schema_file.write_text("", encoding="utf-8")
    monkeypatch.setattr("easycat.cli.scaffold._schema.__file__", str(fake_schema_file))

    assert available_templates() == ["demo"]


def test_template_sources_skip_generated_artifacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    templates_root = tmp_path / "templates"
    template = templates_root / "demo"
    template.mkdir(parents=True)
    kept = template / "agent.py"
    kept.write_text("print('ok')\n", encoding="utf-8")
    for rel in (
        "__pycache__/agent.cpython-312.pyc",
        "demo.egg-info/PKG-INFO",
        ".git/config",
        ".github/workflows/ci.yml",
        ".pytest_cache/state",
        ".venv/pyvenv.cfg",
        "build/generated.py",
        "dist/package.whl",
        "htmlcov/index.html",
        "site/index.html",
        "mutants/state.json",
        ".mutmut-cache/cache",
        ".coverage",
        ".coverage.worker",
        "coverage.xml",
        "cert.pem",
        "private.key",
        "optimized.pyo",
    ):
        path = template / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated\n", encoding="utf-8")
    monkeypatch.setattr("easycat.cli.scaffold.init._templates_root", lambda: templates_root)

    assert _template_sources("demo") == [kept]


def test_template_sources_skip_ignored_top_level_template(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    templates_root = tmp_path / "templates"
    template = templates_root / ".claude"
    template.mkdir(parents=True)
    leaked = template / "settings.local.json"
    leaked.write_text('{"secret":"sentinel"}\n', encoding="utf-8")
    monkeypatch.setattr("easycat.cli.scaffold.init._templates_root", lambda: templates_root)

    assert _template_sources(".claude") == []


# ── pre-launch local-source wiring ───────────────────────────────────


@pytest.mark.parametrize("name", available_templates())
def test_pyproject_carries_sources_block_placeholder(name: str) -> None:
    """Every template pyproject must carry the `$EASYCAT_SOURCES_BLOCK` hook.

    Pre-launch, `easycat` is not on PyPI; without this placeholder a
    scaffold generated from a repo/editable install could never `uv sync`.
    """
    pyproject = (_template_dir(name) / "pyproject.toml").read_text(encoding="utf-8")
    assert "$EASYCAT_SOURCES_BLOCK" in pyproject


@pytest.mark.parametrize("name", available_templates())
def test_pyproject_renders_uv_sources_for_local_checkout(name: str) -> None:
    template_text = (_template_dir(name) / "pyproject.toml").read_text(encoding="utf-8")

    published = _render_text(template_text, _substitutions(InitConfig(template=name), "demo"))
    assert "$EASYCAT_SOURCES_BLOCK" not in published
    assert "[tool.uv.sources]" not in published
    tomllib.loads(published)

    dev = _render_text(
        template_text,
        _substitutions(InitConfig(template=name), "demo", easycat_source=REPO_ROOT),
    )
    parsed = tomllib.loads(dev)
    assert parsed["tool"]["uv"]["sources"]["easycat"] == {
        "path": str(REPO_ROOT),
        "editable": True,
    }


@pytest.mark.parametrize("name", available_templates())
def test_pyproject_renders_portable_pinned_git_source(name: str) -> None:
    template_text = (_template_dir(name) / "pyproject.toml").read_text(encoding="utf-8")
    git_url = "https://github.com/yisding/easycat.git"
    git_rev = "0123456789abcdef0123456789abcdef01234567"

    rendered = _render_text(
        template_text,
        _substitutions(
            InitConfig(template=name),
            "demo",
            easycat_git=git_url,
            easycat_git_rev=git_rev,
        ),
    )
    parsed = tomllib.loads(rendered)

    assert parsed["tool"]["uv"]["sources"]["easycat"] == {
        "git": git_url,
        "rev": git_rev,
    }
    assert str(REPO_ROOT) not in rendered


@pytest.mark.parametrize("name", available_templates())
def test_template_readme_explains_local_source_block(name: str) -> None:
    readme = (_template_dir(name) / "README.md").read_text(encoding="utf-8")
    assert "[tool.uv.sources]" in readme
    assert "not on PyPI yet" in readme
    assert "--easycat-source" in readme
    assert "--easycat-git" in readme
    assert "--easycat-git-rev" in readme
    assert "portable" in readme.lower()


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_template_ships_offline_agent_tests(name: str) -> None:
    """Every scaffold ships a key-free test exercising the real app and pipeline.

    Placeholders, not dollar signs, are what must never survive into a
    generated project: the test rejects a constant shaped like an unrendered
    ``$PLACEHOLDER``, so a literal ``$`` in a user's own agent name or
    instructions stays legitimate.
    """
    source = (_template_dir(name) / "tests" / "test_agent.py").read_text(encoding="utf-8")

    assert "from easycat.debug.testing import" in source
    assert "run_text_turn" in source
    assert "assert_turn_completed" in source
    assert "assert_no_error" in source
    assert "assert_latency" in source
    # Key-free by construction: a scripted stand-in, never a live LLM client.
    assert "OPENAI_API_KEY" not in source
    # The generated test exercises the real app, not a private stub of it.
    assert "StubAgent" not in source
    assert "from agent import" in source
    assert "importorskip" in source
    if name in _TOOLS_MODULE_TEMPLATES:
        assert "import tools" in source
    # The rendered constants are checked against the *shape* of an
    # unrendered placeholder, never against a bare ``"$"``: a dollar sign
    # in a user's own agent name or instructions is legitimate text and
    # must not fail their very first ``uv run pytest``.
    assert r"\$[A-Z_]+" in source
    assert '"$" not in' not in source
    # No placeholders — the same file works in every rendered project.
    for placeholder in ("$AGENT_NAME", "$AGENT_INSTRUCTIONS", "$PROJECT_NAME"):
        assert placeholder not in source


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_template_ships_agents_md_guide(name: str) -> None:
    """Every scaffold ships an AGENTS.md for coding agents in the new project."""
    catalog = {entry["name"]: entry for entry in _available_template_catalog()}
    agents_md = (_template_dir(name) / "AGENTS.md").read_text(encoding="utf-8")

    assert "$PROJECT_NAME" in agents_md
    assert "uv run pytest" in agents_md
    assert "uv run easycat doctor --env-file .env" in agents_md
    assert "uv run easycat doctor --env-file .env --json" in agents_md
    assert "uv run easycat docs" in agents_md
    assert "easycat.debug.testing" in agents_md
    assert "run_text_turn" in agents_md
    assert "assert_llm_judge" in agents_md
    # The run/check hints must match the catalog's post-create commands.
    entry = catalog[name]
    assert entry["run_command"].removeprefix("uv run --env-file .env ").split()[0] in agents_md
    assert entry["run_command"] in agents_md
    assert entry["check_command"] in agents_md
    assert entry["fix_command"] in agents_md


def test_twilio_phone_template_authenticates_public_entrypoints() -> None:
    server = (_template_dir("twilio-phone") / "server.py").read_text(encoding="utf-8")
    env_example = (_template_dir("twilio-phone") / ".env.example").read_text(encoding="utf-8")
    readme = (_template_dir("twilio-phone") / "README.md").read_text(encoding="utf-8")

    assert 'require_env("TWILIO_AUTH_TOKEN")' in server
    assert "validate_twilio_webhook_signature" in server
    assert "twilio_websocket_signature_process_request" in server
    assert "process_request=process_request" in server
    assert "TwilioStreamTokenStore" in server
    assert "stream_token_validator=stream_tokens.consume_start" in server
    assert "if not await transport.wait_for_start(timeout_s=start_timeout_s):" in server
    assert "TWILIO_AUTH_TOKEN" in env_example
    assert "TWILIO_MAX_SESSIONS" in env_example
    assert "TWILIO_START_TIMEOUT_S" in env_example
    assert "TWILIO_DRAIN_TIMEOUT_S" in env_example
    assert "TWILIO_FORCE_SHUTDOWN_TIMEOUT_S" in env_example
    assert "TWILIO_PUBLIC_TWIML_URL" in env_example
    assert "TRUST_PROXY_HEADERS" in env_example
    assert "one-time stream token" in readme


def test_telnyx_phone_template_authenticates_public_entrypoints() -> None:
    server = (_template_dir("telnyx-phone") / "server.py").read_text(encoding="utf-8")
    env_example = (_template_dir("telnyx-phone") / ".env.example").read_text(encoding="utf-8")
    readme = (_template_dir("telnyx-phone") / "README.md").read_text(encoding="utf-8")

    assert 'require_env("TELNYX_API_KEY")' in server
    assert 'require_env("TELNYX_PUBLIC_KEY")' in server
    assert "verify_telnyx_webhook_signature" in server
    assert "TELNYX_WEBHOOK_SIGNATURE_HEADER" in server
    assert "TelnyxStreamTokenValidator" not in server
    assert "stream_token_validator=stream_tokens.consume_start" in server
    assert "if not await transport.wait_for_start(timeout_s=start_timeout_s):" in server
    assert "TELNYX_API_KEY" in env_example
    assert "TELNYX_MAX_SESSIONS" in env_example
    assert "TELNYX_START_TIMEOUT_S" in env_example
    assert "TELNYX_DRAIN_TIMEOUT_S" in env_example
    assert "TELNYX_FORCE_SHUTDOWN_TIMEOUT_S" in env_example
    # Telnyx does not sign the media handshake; the README must say how the
    # one-time stream token compensates.
    assert "one-time stream token" in readme
    assert "NOT signed" in readme


# ── Importable app factories (DX3) ───────────────────────────────

_STUB_BOUNDARY_PHRASE = "says nothing about live model quality"

# The three provider* templates keep a module-level ``register()`` call in
# ``agent.py`` (§3.3): it only mutates a provider registry, opens nothing, and
# the package's entry point (``custom_*:register``) must keep targeting a
# module-level callable. This is the one named exception to "no call at
# module scope" the import-safety guard below allows, and only for that one
# call, in those three templates.
_ALLOWED_MODULE_SCOPE_CALL: dict[str, str] = {
    "provider": "register",
    "provider-stt": "register",
    "provider-tts": "register",
}


def _is_allowed_module_scope_call(node: ast.AST, name: str) -> bool:
    allowed = _ALLOWED_MODULE_SCOPE_CALL.get(name)
    return (
        allowed is not None
        and isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == allowed
        and not node.value.args
        and not node.value.keywords
    )


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_template_entry_points_are_import_safe(name: str) -> None:
    """Importing a generated entry point must start no audio and no server."""
    cfg = InitConfig(
        template=name,
        agent_name="Support",
        agent_instructions="Help the user with billing.",
    )
    mapping = _substitutions(cfg, project_name="demo")

    for filename in ("agent.py", "server.py"):
        path = _template_dir(name) / filename
        if not path.is_file():
            continue
        tree = ast.parse(_render_text(path.read_text(encoding="utf-8"), mapping))
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                continue  # module docstring
            if isinstance(node, ast.Import | ast.ImportFrom):
                continue
            if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if isinstance(node, ast.Assign | ast.AnnAssign):
                assert not any(isinstance(sub, ast.Call) for sub in ast.walk(node)), (
                    f"{name}/{filename} calls something at module scope"
                )
                continue
            if _is_allowed_module_scope_call(node, name):
                continue
            assert _is_main_guard(node), (
                f"{name}/{filename} runs {ast.dump(node)[:60]}… at import time"
            )


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_template_tests_import_only_symbols_the_app_defines(name: str) -> None:
    """Renaming or deleting a tested app symbol must fail here, SDK or not."""
    template = _template_dir(name)
    defined: set[str] = set()
    for filename in ("agent.py", "tools.py"):
        path = template / filename
        if path.is_file():
            defined |= _module_level_names(path.read_text(encoding="utf-8"))

    test_source = (template / "tests" / "test_agent.py").read_text(encoding="utf-8")
    imported = _names_imported_from(test_source, {"agent", "tools"})

    assert imported <= defined, (
        f"{name} test imports undefined names: {sorted(imported - defined)}"
    )
    assert {_PRIMARY_FACTORY[name]} <= imported


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_template_offline_tests_cover_text_and_audio(name: str) -> None:
    """A text-only pass may never stand in for audio-pipeline coverage."""
    source = (_template_dir(name) / "tests" / "test_agent.py").read_text(encoding="utf-8")

    assert "run_text_turns(" in source
    assert "run_scripted_audio_turn(" in source


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_template_offline_tests_name_their_stub_boundary(name: str) -> None:
    """The stub boundary is stated in both the generated test and AGENTS.md."""
    template = _template_dir(name)
    test_source = (template / "tests" / "test_agent.py").read_text(encoding="utf-8")
    docstring = ast.get_docstring(ast.parse(test_source)) or ""
    agents_md = (template / "AGENTS.md").read_text(encoding="utf-8")

    assert "stands in" in docstring
    assert _STUB_BOUNDARY_PHRASE in docstring
    assert "class ScriptedReasoning" in test_source
    assert _STUB_BOUNDARY_PHRASE in agents_md
    assert "ScriptedReasoning" in agents_md


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
def test_template_pytest_config_puts_the_project_on_sys_path(name: str) -> None:
    """``uv run pytest`` uses the console script, which never prepends the cwd."""
    cfg = InitConfig(
        template=name,
        agent_name="Support",
        agent_instructions="Help the user with billing.",
    )
    mapping = _substitutions(cfg, project_name="demo")
    rendered = _render_text(
        (_template_dir(name) / "pyproject.toml").read_text(encoding="utf-8"), mapping
    )

    parsed = tomllib.loads(rendered)
    assert parsed["tool"]["pytest"]["ini_options"]["pythonpath"] == ["."]


def test_offline_scaffold_coverage_is_not_text_only() -> None:
    """The provider on-ramps keep real audio-shaped contract coverage."""
    provider_templates = {
        "provider": ("test_custom_vad.py", "custom_vad.py", "VADProviderContractSuite"),
        "provider-stt": ("test_custom_stt.py", "custom_stt.py", "STTProviderContractSuite"),
        "provider-tts": ("test_custom_tts.py", "custom_tts.py", "TTSProviderContractSuite"),
    }
    assert set(provider_templates) <= set(available_templates())

    for name, (test_file, module_file, suite) in provider_templates.items():
        test_source = (_template_dir(name) / test_file).read_text(encoding="utf-8")
        module_source = (_template_dir(name) / module_file).read_text(encoding="utf-8")
        assert suite in test_source, f"{name}/{test_file} no longer subclasses {suite}"
        assert "AudioChunk" in module_source, f"{name}/{module_file} no longer handles audio"


@pytest.mark.parametrize(
    ("template_source", "extra_kwargs", "expected"),
    [
        ("f(agent=agent, **__EASYCAT_CONFIG_EXTRA__)", "", "f(agent=agent)"),
        ("f(agent=make_agent(), **__EASYCAT_CONFIG_EXTRA__)", "", "f(agent=make_agent())"),
        (
            "f(agent=make_agent(), **__EASYCAT_CONFIG_EXTRA__)",
            'stt="deepgram/flux"',
            'f(agent=make_agent(), stt="deepgram/flux")',
        ),
        (
            "f(agent=SupportWorkflow(), **__EASYCAT_CONFIG_EXTRA__)",
            "",
            "f(agent=SupportWorkflow())",
        ),
        ("f(**__EASYCAT_CONFIG_EXTRA__)", "", "f()"),
        ("f(**__EASYCAT_CONFIG_EXTRA__)", 'tts="openai"', 'f(tts="openai")'),
        ("            **__EASYCAT_CONFIG_EXTRA__,  # noqa: F821\n", "", ""),
        (
            "            **__EASYCAT_CONFIG_EXTRA__,  # noqa: F821\n",
            'stt="x"',
            '            stt="x",\n',
        ),
        # Any nesting depth, not just the two the templates happen to use
        # today: a fixed-width match would leave four orphaned spaces here.
        ("                **__EASYCAT_CONFIG_EXTRA__,  # noqa: F821\n", "", ""),
        (
            "                **__EASYCAT_CONFIG_EXTRA__,  # noqa: F821\n",
            'stt="x"',
            '                stt="x",\n',
        ),
        (
            "f(\n                **__EASYCAT_CONFIG_EXTRA__,  # noqa: F821\n            )\n",
            "",
            "f(\n            )\n",
        ),
    ],
)
def test_render_drops_the_config_sentinel_with_its_separator(
    template_source: str, extra_kwargs: str, expected: str
) -> None:
    """The sentinel leaves with the separator that introduced it."""
    rendered = _render_text(template_source, {"EASYCAT_CONFIG_EXTRA": extra_kwargs})

    assert rendered == expected
    assert ", )" not in rendered


@pytest.mark.parametrize("name", sorted(_LINE_BUDGETS))
@pytest.mark.parametrize("extra", ["", 'stt="deepgram/flux"', 'tts="openai"'])
def test_rendered_templates_never_keep_a_dangling_separator(name: str, extra: str) -> None:
    cfg = InitConfig(
        template=name,
        agent_name="Support",
        agent_instructions="Help the user with billing.",
        stt="deepgram/flux" if extra.startswith("stt") else None,
        tts="openai" if extra.startswith("tts") else None,
    )
    mapping = _substitutions(cfg, project_name="demo")
    for py_file in _template_dir(name).glob("*.py"):
        rendered = _render_text(py_file.read_text(encoding="utf-8"), mapping)
        assert ", )" not in rendered
        ast.parse(rendered, filename=str(py_file))


# T11 only proves something for templates whose CLI-supplied stt=/tts=/
# mcp_servers= flags actually reach a rendered VoiceApp(...)/EasyConfig(...)
# call: text-chat has no audio config at all, and the three provider*
# templates hard-code a *custom* provider string (``vad="energy"`` etc.) that
# ``EasyConfig.mic`` cannot resolve without that package's own ``register()``
# — that value-level guarantee is what their own generated test proves
# instead (``test_register_makes_the_custom_*_selectable``).
_CONFIG_BUILDER_FILE: dict[str, str] = {
    "twilio-phone": "server.py",
    "telnyx-phone": "server.py",
}
_T11_TEMPLATES: frozenset[str] = frozenset(_LINE_BUDGETS) - {
    "text-chat",
    "provider",
    "provider-stt",
    "provider-tts",
}
# Keywords that build a provider/config object rather than naming a provider
# string, so ``ast.literal_eval`` must never be asked to evaluate them.
# ``provider`` stays in: it names the telephony vendor as a plain string and
# ``EasyConfig.phone`` — the preset the telnyx template really calls — needs it.
_CONFIG_BUILDER_EXCLUDED_KWARGS: frozenset[str] = frozenset({"agent", "transport", "telephony"})


def _config_builder_call(tree: ast.Module) -> tuple[ast.Call, str] | None:
    """Find the config-building call and the ``EasyConfig`` preset it uses.

    Returns the call node plus the preset name so the caller can rebuild the
    config through the *same* preset the template really calls
    (``EasyConfig.browser`` for webrtc-browser, ``EasyConfig.phone`` for
    telnyx-phone).  A bare ``VoiceApp(...)``/``EasyConfig(...)`` has no preset
    attribute; ``mic`` is the equivalent local-audio default those two build.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in {"VoiceApp", "EasyConfig"}:
            return node, "mic"
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "EasyConfig"
        ):
            return node, node.func.attr
    return None


def _t11_cases() -> list[tuple[str, dict[str, object]]]:
    cases: list[tuple[str, dict[str, object]]] = [(name, {}) for name in sorted(_T11_TEMPLATES)]
    for name in sorted(_T11_TEMPLATES):
        if _TEMPLATE_SPECS[name].supports_audio_config:
            cases.append((name, {"stt": "deepgram/flux"}))
            cases.append((name, {"tts": "openai"}))
            cases.append((name, {"mcp_servers": ["http://localhost:8931/mcp"]}))
    return cases


@pytest.mark.parametrize(("name", "overrides"), _t11_cases())
def test_rendered_app_kwargs_resolve_to_real_providers(
    name: str, overrides: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kwargs the renderer emits must name providers that really resolve.

    ``VoiceApp(...)``/``EasyConfig(...)`` validate field *names* only, so this
    is the value-level half: it parses (never imports) the rendered app and
    feeds the literal kwargs to the template's own ``EasyConfig`` preset plus
    the provider planner.
    """
    from easycat.config import EasyConfig
    from easycat.planning.provider_plan import build_provider_plan
    from easycat.stubs import NoopAgent

    for env_name in (
        "OPENAI_API_KEY",
        "DEEPGRAM_API_KEY",
        "ELEVENLABS_API_KEY",
        "CARTESIA_API_KEY",
    ):
        monkeypatch.setenv(env_name, "test-not-used")

    cfg = InitConfig(
        template=name,
        agent_name="Support",
        agent_instructions="Help the user with billing.",
        **overrides,  # type: ignore[arg-type]
    )
    mapping = _substitutions(cfg, project_name="demo")
    filename = _CONFIG_BUILDER_FILE.get(name, "agent.py")
    rendered = _render_text((_template_dir(name) / filename).read_text(encoding="utf-8"), mapping)

    found = _config_builder_call(ast.parse(rendered))
    assert found is not None, f"{name}/{filename} builds no VoiceApp/EasyConfig call"
    call, preset = found
    kwargs = {
        kw.arg: ast.literal_eval(kw.value)
        for kw in call.keywords
        if kw.arg is not None and kw.arg not in _CONFIG_BUILDER_EXCLUDED_KWARGS
    }

    # The template's own preset, not a hard-coded ``mic``: this is what proves
    # ``EasyConfig.browser(...)`` builds with the ``webrtc`` extra absent (the
    # config object opens no transport) and that ``EasyConfig.phone(...)``
    # accepts the telnyx server's rendered kwargs.
    assert hasattr(EasyConfig, preset), (
        f"{name}/{filename} calls an unknown preset EasyConfig.{preset}(...)"
    )
    config = getattr(EasyConfig, preset)(agent=NoopAgent(), **kwargs)
    plan = build_provider_plan(config)
    if "stt" in kwargs:
        assert plan.selected["stt"].provider == str(kwargs["stt"]).split("/")[0]
    if "tts" in kwargs:
        assert plan.selected["tts"].provider == str(kwargs["tts"]).split("/")[0]
    if "mcp_servers" in kwargs:
        assert list(config.mcp_servers or []) == list(kwargs["mcp_servers"])  # type: ignore[arg-type]
