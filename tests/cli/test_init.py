"""``easycat init`` — scaffolding flows, error paths, and templates."""

from __future__ import annotations

import json
import re
import shlex
import tomllib
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from easycat._provider_registry import stt_tts_catalogs
from easycat.cli._app import app
from easycat.cli.scaffold import init as init_module
from easycat.cli.scaffold._schema import available_templates
from easycat.stt.factory import available_stt_providers
from easycat.tts.factory import available_tts_providers
from tests._release_artifacts import release_artifact_offenders

REPO_ROOT = Path(__file__).resolve().parents[2]


def _render_rich_markup(markup: str) -> str:
    stream = StringIO()
    Console(file=stream, force_terminal=False, no_color=True, width=120).print(markup)
    return stream.getvalue()


# ── --list-templates and basic flows ─────────────────────────────────


def test_init_help_describes_template_catalog_commands(cli: CliRunner) -> None:
    result = cli.invoke(app, ["init", "--help"])
    help_text = " ".join(result.stdout.split())
    help_words = re.sub(r"\W+", " ", result.stdout)

    assert result.exit_code == 0
    assert "--list-templates" in result.stdout
    assert "template guidance" in help_text
    assert "base package requirements" in help_words
    assert "extras" in help_text
    assert "env vars" in help_text
    assert "files" in help_text
    assert "preflight check fix docs json schema run commands" in help_words
    assert "explain" in help_text
    assert "init-schema" in help_text
    assert "--easycat-source" in result.stdout
    assert "--easycat-git" in result.stdout
    assert "--easycat-git-rev" in result.stdout
    assert "mutually exclusive" in help_text


def test_list_templates(cli: CliRunner) -> None:
    result = cli.invoke(app, ["init", "--list-templates"])
    assert result.exit_code == 0
    template_names = set(available_templates())
    names = [
        line.split()[0]
        for line in result.stdout.splitlines()
        if line and not line[0].isspace() and line.split()[0] in template_names
    ]
    assert set(names) == template_names
    for label in (
        "Best for:",
        "Required env:",
        "Optional env:",
        "Base extras:",
        "Base package:",
        "Files:",
        "Command note:",
        "Machine-readable template catalog: easycat init --list-templates --json",
    ):
        assert label in result.stdout
    for template in available_templates():
        assert f"Create: easycat init my-agent --template {template}" in result.stdout
        assert f"Repo create: uv run easycat init my-agent --template {template}" in result.stdout


def test_easycat_version_floor_strips_local_version_for_ordered_specifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_module.importlib.metadata, "version", lambda name: "0.1.0+local")

    assert init_module._easycat_version_floor() == "0.1.0"
    assert init_module._base_requirement("openai-agents") == "easycat[openai-agents,local]>=0.1.0"


def test_template_catalog_renders_bracketed_text_literally() -> None:
    base_requirement = f"easycat[sdk[beta]]>={init_module._easycat_version_floor()}"
    catalog = [
        {
            "name": "demo[beta]",
            "description": "Uses optional extra easycat[openai-agents].",
            "mode": "voice",
            "transport": "local[dev]",
            "framework": "OpenAI Agents",
            "base_extras": ("sdk[beta]",),
            "base_requirement": base_requirement,
            "files": ("agent[beta].py", ".env.example"),
            "best_for": "Teams using SDK[beta].",
            "required_env": ("OPENAI_API_KEY", "SDK[KEY]"),
            "optional_env": ("SDK[OPTIONAL]",),
            "create_command": "easycat init demo --template demo[beta]",
            "repo_create_command": "uv run easycat init demo --template demo[beta]",
            "check_command": "uv add 'easycat[openai-agents]'",
            "fix_command": "uv run ruff check --fix agent[beta].py",
            "run_command": "uv run --env-file .env python agent.py",
        }
    ]

    rendered = _render_rich_markup(init_module._format_template_catalog(catalog))

    assert "demo[beta]" in rendered
    assert "easycat[openai-agents]" in rendered
    assert "local[dev]" in rendered
    assert "sdk[beta]" in rendered
    assert base_requirement in rendered
    assert "agent[beta].py" in rendered
    assert "Teams using SDK[beta]." in rendered
    assert "SDK[KEY]" in rendered
    assert "SDK[OPTIONAL]" in rendered
    assert "easycat init demo --template demo[beta]" in rendered
    assert "uv run easycat doctor --env-file .env" in rendered
    assert "uv run ruff check --fix agent[beta].py" in rendered
    assert "uv run easycat docs" in rendered
    assert "uv run easycat docs --audience app-builders" in rendered
    assert "uv add 'easycat[openai-agents]'" in rendered


def test_list_templates_json(cli: CliRunner) -> None:
    result = cli.invoke(app, ["init", "--list-templates", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert set(payload["templates"]) == set(available_templates())
    assert "installed CLI form" in payload["command_note"]
    assert "repo_create_command runs from this repository root" in payload["command_note"]
    catalog = {entry["name"]: entry for entry in payload["catalog"]}
    assert set(catalog) == set(available_templates())
    required_fields = {
        "base_extras",
        "base_requirement",
        "best_for",
        "check_command",
        "create_command",
        "description",
        "files",
        "fix_command",
        "framework",
        "mode",
        "next_step_commands",
        "optional_env",
        "repo_create_command",
        "required_env",
        "run_command",
        "transport",
    }
    for name, entry in catalog.items():
        assert required_fields <= set(entry)
        assert entry["create_command"] == f"easycat init my-agent --template {name}"
        assert entry["repo_create_command"] == f"uv run easycat init my-agent --template {name}"


def test_missing_name_without_list_templates(cli: CliRunner) -> None:
    """`easycat init` with no NAME and no --list-templates exits 2."""
    result = cli.invoke(app, ["init"])
    assert result.exit_code == 2
    assert "Missing argument 'NAME'" in result.stderr


def test_missing_name_json_uses_standard_error_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["init", "--json"])

    assert result.exit_code == 2
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "init"
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert "Missing argument 'NAME'" in payload["message"]
    assert "easycat init --list-templates" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


# ── Scaffolding success paths ────────────────────────────────────────


def test_init_text_chat_non_interactive(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "text-chat",
            "agent_name": "Support",
            "agent_instructions": "Help the user with billing.",
        }
    )
    result = cli.invoke(
        app,
        ["init", "demo", "--config", config, "--no-git"],
    )
    assert result.exit_code == 0, result.stderr
    project = tmp_path / "demo"
    assert (project / "agent.py").exists()
    assert (project / "pyproject.toml").exists()
    assert (project / "README.md").exists()
    assert (project / ".env.example").exists()
    assert (project / ".gitignore").exists()
    # Substitution landed.
    agent_py = (project / "agent.py").read_text()
    assert 'name="Support"' in agent_py
    assert "Help the user with billing." in agent_py
    assert "$AGENT_NAME" not in agent_py
    pyproject = (project / "pyproject.toml").read_text()
    assert 'name = "demo"' in pyproject


def test_init_normalizes_pyproject_name_without_changing_readme_title(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(app, ["init", "EasyCat Demo Project", "--config", config, "--no-git"])

    assert result.exit_code == 0, result.stderr
    project = tmp_path / "EasyCat Demo Project"
    assert 'name = "easycat-demo-project"' in (project / "pyproject.toml").read_text()
    assert "# EasyCat Demo Project" in (project / "README.md").read_text()


def test_init_json_envelope(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = cli.invoke(
        app,
        [
            "init",
            "demo",
            "--config",
            json.dumps({"schema_version": 1, "template": "text-chat"}),
            "--no-git",
            "--json",
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["command"] == "init"
    assert payload["status"] == "ok"
    assert payload["template"] == "text-chat"
    assert {".env.example", "agent.py", "README.md"} <= set(payload["files"])
    assert payload["git"] is False


def test_init_force_overwrites_existing(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "demo"
    target.mkdir()
    (target / "leftover.txt").write_text("preexisting")
    config = json.dumps({"schema_version": 1, "template": "text-chat", "agent_name": "Forced"})
    result = cli.invoke(
        app,
        ["init", "demo", "--config", config, "--no-git", "--force"],
    )
    assert result.exit_code == 0, result.stderr
    assert 'name="Forced"' in (target / "agent.py").read_text()
    # leftover.txt is not removed — init writes into the dir; it does not
    # wipe it.  That's intentional and matches the plan.
    assert (target / "leftover.txt").exists()


def test_init_escapes_agent_literals(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "openai-agents",
            "agent_name": 'Sales "A\\B"',
            "agent_instructions": 'Line one\\path\nLine two says "hi"',
        }
    )
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 0, result.stderr
    agent_py = tmp_path / "demo" / "agent.py"
    compile(agent_py.read_text(encoding="utf-8"), str(agent_py), "exec")


def test_init_renders_non_ascii_instructions_intact(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Em-dashes and other non-ASCII survive into agent.py, not \\uXXXX escapes."""
    monkeypatch.chdir(tmp_path)
    instructions = "Keep answers short — you're speaking aloud, naïvité ☕."
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "openai-agents",
            "agent_instructions": instructions,
        }
    )
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 0, result.stderr
    agent_py = tmp_path / "demo" / "agent.py"
    text = agent_py.read_text(encoding="utf-8")
    assert instructions in text
    assert "\\u" not in text
    compile(text, str(agent_py), "exec")


def test_init_escapes_json_surrogates_in_agent_literals(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON accepts lone surrogates, but generated source must stay UTF-8 encodable."""
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "openai-agents",
            "agent_name": "\ud800",
            "agent_instructions": "Pair? \udfff",
        }
    )
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 0, result.stderr
    agent_py = tmp_path / "demo" / "agent.py"
    text = agent_py.read_text(encoding="utf-8")
    assert "\\ud800" in text
    assert "\\udfff" in text
    compile(text, str(agent_py), "exec")


def test_init_webrtc_browser_template(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "webrtc-browser",
            "transport": "browser",
            "agent_name": "BrowserBot",
            "agent_instructions": "Help people test browser audio.",
        }
    )

    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])

    assert result.exit_code == 0, result.stderr
    project = tmp_path / "demo"
    agent_py = (project / "agent.py").read_text()
    assert "EasyConfig.browser(" in agent_py
    assert 'name="BrowserBot"' in agent_py
    assert "Help people test browser audio." in agent_py
    pyproject = (project / "pyproject.toml").read_text()
    assert "openai-agents,webrtc" in pyproject
    readme = (project / "README.md").read_text()
    assert "http://localhost:8080" in readme


def test_init_pydantic_ai_workflow_template(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "pydantic-ai-workflow",
            "agent_instructions": "Keep handoffs crisp.",
        }
    )

    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])

    assert result.exit_code == 0, result.stderr
    project = tmp_path / "demo"
    agent_py = (project / "agent.py").read_text()
    assert "class SupportWorkflow" in agent_py
    assert "async def on_user_turn" in agent_py
    assert "Keep handoffs crisp." in agent_py
    assert "EasyConfig.mic(" in agent_py
    pyproject = (project / "pyproject.toml").read_text()
    assert "pydantic-ai,local" in pyproject


def test_init_twilio_phone_template(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "twilio-phone",
            "transport": "phone",
            "agent_name": "PhoneBot",
            "agent_instructions": "Take concise phone messages.",
        }
    )

    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])

    assert result.exit_code == 0, result.stderr
    project = tmp_path / "demo"
    assert (project / "server.py").exists()
    agent_py = (project / "agent.py").read_text()
    assert "def make_agent" in agent_py
    assert 'name="PhoneBot"' in agent_py
    assert "Take concise phone messages." in agent_py
    server_py = (project / "server.py").read_text()
    assert "TwilioConnectionTransport" in server_py
    assert "twiml_connect_stream" in server_py
    pyproject = (project / "pyproject.toml").read_text()
    assert "openai-agents,telephony,telephony-fastapi" in pyproject
    env_example = (project / ".env.example").read_text()
    assert "TWILIO_STREAM_URL" in env_example


def test_init_twilio_phone_honors_provider_shortcuts(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "twilio-phone",
            "stt": "deepgram/flux",
        }
    )

    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])

    assert result.exit_code == 0, result.stderr
    project = tmp_path / "demo"
    assert 'stt="deepgram/flux"' in (project / "server.py").read_text()
    assert "deepgram" in (project / "pyproject.toml").read_text()
    assert "DEEPGRAM_API_KEY" in (project / ".env.example").read_text()


def test_init_telnyx_phone_template(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "telnyx-phone",
            "transport": "telnyx",
            "agent_name": "PhoneBot",
            "agent_instructions": "Take concise phone messages.",
        }
    )

    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])

    assert result.exit_code == 0, result.stderr
    project = tmp_path / "demo"
    assert (project / "server.py").exists()
    agent_py = (project / "agent.py").read_text()
    assert "def make_agent" in agent_py
    assert 'name="PhoneBot"' in agent_py
    assert "Take concise phone messages." in agent_py
    server_py = (project / "server.py").read_text()
    assert "TelnyxConnectionTransport" in server_py
    assert 'provider="telnyx"' in server_py
    pyproject = (project / "pyproject.toml").read_text()
    assert "openai-agents,telnyx,telephony-fastapi" in pyproject
    env_example = (project / ".env.example").read_text()
    assert "TELNYX_STREAM_URL" in env_example


def test_init_omits_cache_artifacts(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No generated path may contain a cache dir or compiled bytecode."""
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "openai-agents"})
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git", "--json"])
    assert result.exit_code == 0, result.stderr
    project = tmp_path / "demo"

    generated_paths = [
        path.relative_to(project).as_posix() for path in project.rglob("*") if path != project
    ]
    offenders = release_artifact_offenders(generated_paths, scaffold_project=True)
    assert not offenders, "generated project shipped local/generated artifacts: " + ", ".join(
        offenders
    )

    # The reported file manifest is equally clean.
    payload = json.loads(result.stdout)
    manifest_offenders = release_artifact_offenders(payload["files"], scaffold_project=True)
    assert not manifest_offenders, "reported generated-project manifest is not clean: " + (
        ", ".join(manifest_offenders)
    )

    # The legitimate top-level .gitignore is still shipped.
    assert (project / ".gitignore").exists()


# ── Error paths ──────────────────────────────────────────────────────


def test_init_target_exists_without_force(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "demo"
    target.mkdir()
    (target / "leftover.txt").write_text("x")
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 101
    assert "EASYCAT_E101" in result.stderr
    # Rich may wrap the long path across lines; normalize before checking.
    normalized = " ".join(result.stderr.split())
    assert "already exists" in normalized


def test_init_target_is_existing_file(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NAME already existing as a file must surface E101, not FileExistsError."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "demo").write_text("I'm a file.")
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    # --force must not silently overwrite a file-typed collision.
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git", "--force"])
    assert result.exit_code == 101
    assert "EASYCAT_E101" in result.stderr


def test_init_bad_json(cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = cli.invoke(app, ["init", "demo", "--config", "not json", "--no-git"])
    assert result.exit_code == 4
    assert "EASYCAT_E102" in result.stderr
    assert "not valid JSON" in result.stderr


def test_init_unknown_key_fuzzy_suggest(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat", "templat": "typo"})
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 4
    assert "EASYCAT_E102" in result.stderr
    assert "Did you mean" in result.stderr
    assert "'template'" in result.stderr


def test_init_unknown_template(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "openai_agents"})
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 2
    assert "EASYCAT_E103" in result.stderr
    assert "Fix:" in result.stderr
    assert "easycat init --list-templates" in result.stderr


def test_init_unknown_template_json_includes_fix(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "openai_agents"})
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git", "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["code"] == "EASYCAT_E103"
    assert "easycat init --list-templates" in payload["fix"]


def test_init_missing_schema_version(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"template": "text-chat"})
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 4
    assert "schema_version" in result.stderr


# ── Optional-field honoring (stt / tts / mcp_servers) ──────────────────


def test_init_honors_stt_string(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stt="deepgram/flux"` lands in agent.py, .env.example, and pyproject."""
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "openai-agents",
            "stt": "deepgram/flux",
        }
    )
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 0, result.stderr
    project = tmp_path / "demo"
    agent_py = (project / "agent.py").read_text()
    assert 'stt="deepgram/flux"' in agent_py
    pyproject = (project / "pyproject.toml").read_text()
    assert "deepgram" in pyproject
    env_example = (project / ".env.example").read_text()
    assert "DEEPGRAM_API_KEY" in env_example


def test_init_honors_cartesia_provider(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cartesia STT/TTS must wire CARTESIA_API_KEY into .env.example."""
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "openai-agents",
            "stt": "cartesia",
            "tts": "cartesia",
        }
    )
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 0, result.stderr
    env_example = (tmp_path / "demo" / ".env.example").read_text()
    assert "CARTESIA_API_KEY" in env_example


def test_init_honors_tts_and_mcp_servers(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "openai-agents",
            "tts": "elevenlabs/eleven_flash_v2_5",
            "mcp_servers": ["stdio:///bin/echo"],
        }
    )
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 0, result.stderr
    project = tmp_path / "demo"
    agent_py = (project / "agent.py").read_text()
    assert 'tts="elevenlabs/eleven_flash_v2_5"' in agent_py
    assert "mcp_servers=" in agent_py
    pyproject = (project / "pyproject.toml").read_text()
    assert "elevenlabs" in pyproject
    env_example = (project / ".env.example").read_text()
    assert "ELEVENLABS_API_KEY" in env_example


def test_scaffold_provider_shortcuts_have_install_and_env_mappings() -> None:
    """Every provider accepted by `easycat init` must scaffold install/env hints."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    known_extras = set(pyproject["project"]["optional-dependencies"])
    provider_names = set(available_stt_providers()) | set(available_tts_providers())
    credentialed_provider_names = {
        name
        for catalog in stt_tts_catalogs()
        for name, env_var in catalog.env_vars.items()
        if env_var is not None
    }

    missing_extras = sorted(provider_names - set(init_module._provider_to_extra()))
    missing_env_vars = sorted(
        credentialed_provider_names - set(init_module._provider_to_env_var())
    )
    unknown_extras = sorted(set(init_module._provider_to_extra().values()) - known_extras)

    assert not missing_extras, "Scaffold missing provider extra mappings: " + ", ".join(
        missing_extras
    )
    assert not missing_env_vars, "Scaffold missing provider env-var mappings: " + ", ".join(
        missing_env_vars
    )
    assert not unknown_extras, "Scaffold provider maps reference unknown extras: " + ", ".join(
        unknown_extras
    )


def test_init_default_omits_extra_kwargs(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No stt/tts requested → no extra kwargs (no $-leak in scaffolded files)."""
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "openai-agents"})
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 0, result.stderr
    project = tmp_path / "demo"
    for fname in ("agent.py", "pyproject.toml", ".env.example"):
        assert "$" not in (project / fname).read_text(), f"{fname} leaked a placeholder"


# ── Not-yet-wired fields are rejected loudly ──────────────────────────


@pytest.mark.parametrize(
    "field,value",
    [
        ("llm", "openai/gpt-4o"),
        ("transport", "webrtc"),
    ],
)
def test_init_rejects_not_yet_wired_string_fields(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "openai-agents", field: value})
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 4
    assert "EASYCAT_E102" in result.stderr


def test_init_rejects_local_transport_for_webrtc_template(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "webrtc-browser",
            "transport": "local",
        }
    )

    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])

    assert result.exit_code == 4
    assert "EASYCAT_E102" in result.stderr
    assert "webrtc" in result.stderr


def test_init_rejects_webrtc_transport_for_twilio_template(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "twilio-phone",
            "transport": "webrtc",
        }
    )

    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])

    assert result.exit_code == 4
    assert "EASYCAT_E102" in result.stderr
    assert "twilio" in result.stderr


def test_init_rejects_tools_field(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "openai-agents",
            "tools": ["weather"],
        }
    )
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 4
    assert "EASYCAT_E102" in result.stderr


def test_init_rejects_voice_fields_for_text_chat(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "text-chat",
            "stt": "deepgram/flux",
        }
    )
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 4
    assert "EASYCAT_E102" in result.stderr


# ── Provider/MCP validation runs before scaffold writes ────────────────


def test_init_rejects_unknown_stt_provider(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typo in `stt` shortcut fails at scaffold time, not first run."""
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "openai-agents",
            "stt": "deepgrm/flux",
        }
    )
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 2
    assert "EASYCAT_E104" in result.stderr
    assert "Did you mean 'deepgram'" in result.stderr
    # No project files written.
    assert not (tmp_path / "demo").exists()


def test_init_rejects_unknown_tts_provider(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "openai-agents",
            "tts": "elevenlbs/eleven_flash_v2_5",
        }
    )
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 2
    assert "EASYCAT_E104" in result.stderr
    assert "Did you mean 'elevenlabs'" in result.stderr
    assert not (tmp_path / "demo").exists()


def test_init_rejects_non_uri_mcp_server(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plain names like `'filesystem'` are not yet wired — reject at scaffold."""
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "openai-agents",
            "mcp_servers": ["filesystem"],
        }
    )
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 4
    assert "EASYCAT_E102" in result.stderr
    assert "MCP server URI" in result.stderr
    assert not (tmp_path / "demo").exists()


# ── Doctor next-step uses the project env, not uvx ────────────────────


def _template_readme_run_command(template: str) -> str:
    readme = (init_module._templates_root() / template / "README.md").read_text(encoding="utf-8")
    run_section = readme.split("## Run", 1)[1].split("## Check", 1)[0]
    command_block = run_section.split("```bash", 1)[1].split("```", 1)[0]
    commands = [line.strip() for line in command_block.splitlines() if line.strip()]

    assert len(commands) == 1, f"{template}/README.md should have one primary run command"
    return commands[0]


def _template_readme_check_command(template: str) -> str:
    readme = (init_module._templates_root() / template / "README.md").read_text(encoding="utf-8")
    check_section = readme.split("## Check", 1)[1].split("## Next steps", 1)[0]
    command_block = check_section.split("```bash", 1)[1].split("```", 1)[0]
    commands = [line.strip() for line in command_block.splitlines() if line.strip()]

    assert len(commands) == 1, f"{template}/README.md should have one primary check command"
    return commands[0]


def _template_readme_fix_command(template: str) -> str:
    readme = (init_module._templates_root() / template / "README.md").read_text(encoding="utf-8")
    check_section = readme.split("## Check", 1)[1].split("## Next steps", 1)[0]
    commands = re.findall(r"`(uv run ruff check --fix [^`]+)`", check_section)

    assert len(commands) == 1, f"{template}/README.md should have one fix command"
    return commands[0]


def test_init_next_steps_load_env_for_doctor(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    normalized_stderr = " ".join(result.stderr.split())
    assert result.exit_code == 0, result.stderr
    assert "cd demo" in result.stderr
    assert "cp .env.example .env" in result.stderr
    assert "fill in your API keys" in normalized_stderr
    assert "uv sync && uv run easycat doctor --env-file .env" in normalized_stderr
    assert "uvx easycat doctor" not in result.stderr
    assert "uv run python -m py_compile" not in result.stderr


def test_init_next_steps_human_output_is_trimmed(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The human footer stays short; the full catalog lives in the JSON envelope."""
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    normalized_stderr = " ".join(result.stderr.split())
    assert result.exit_code == 0, result.stderr

    next_steps = result.stderr.split("Next steps:", 1)[1]
    command_lines = [
        line for line in next_steps.splitlines() if line.startswith("  ") and line.strip()
    ]
    assert len(command_lines) <= 5

    assert "uv run easycat doctor --env-file .env --json" not in result.stderr
    assert "uv run ruff check agent.py" not in result.stderr
    assert "uv run ruff check --fix agent.py" not in result.stderr
    assert "uv run easycat docs --audience app-builders" not in result.stderr
    assert "uv run easycat explain json-schema" not in result.stderr
    assert "easycat init --list-templates --json" in normalized_stderr
    assert "next_step_commands" in normalized_stderr


def test_init_next_steps_quote_project_name_for_shell(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(app, ["init", "demo project", "--config", config, "--no-git"])

    assert result.exit_code == 0, result.stderr
    assert "cd 'demo project'" in result.stderr
    assert "cd demo project" not in result.stderr


def test_init_escapes_project_name_markup_in_status_output(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(app, ["init", "demo[red]", "--config", config, "--no-git"])

    assert result.exit_code == 0, result.stderr
    assert "Creating demo[red]/" in result.stderr
    assert "cd 'demo[red]'" in result.stderr
    assert (tmp_path / "demo[red]").exists()


@pytest.mark.parametrize("template", sorted(available_templates()))
def test_init_next_steps_match_template_readme_run_command(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    template: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    name = f"demo-{template}"
    config = json.dumps({"schema_version": 1, "template": template})
    result = cli.invoke(app, ["init", name, "--config", config, "--no-git"])

    assert result.exit_code == 0, result.stderr
    assert _template_readme_run_command(template) in " ".join(result.stderr.split())
    if template == "twilio-phone":
        assert "uv run --env-file .env python agent.py" not in result.stderr


@pytest.mark.parametrize("template", sorted(available_templates()))
def test_init_next_steps_omit_check_and_fix_commands(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    template: str,
) -> None:
    """Check/fix commands stay out of the trimmed footer but in the JSON catalog."""
    monkeypatch.chdir(tmp_path)
    name = f"demo-{template}"
    config = json.dumps({"schema_version": 1, "template": template})
    result = cli.invoke(app, ["init", name, "--config", config, "--no-git"])

    assert result.exit_code == 0, result.stderr
    assert _template_readme_check_command(template) not in " ".join(result.stderr.split())
    assert _template_readme_fix_command(template) not in " ".join(result.stderr.split())
    assert _template_readme_check_command(template) in init_module._next_step_commands(
        Path(name), template
    )
    assert _template_readme_fix_command(template) in init_module._next_step_commands(
        Path(name), template
    )


@pytest.mark.parametrize("template", sorted(available_templates()))
def test_init_json_next_step_commands_match_template_readme(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    template: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    name = f"demo-{template}"
    config = json.dumps({"schema_version": 1, "template": template})
    result = cli.invoke(app, ["init", name, "--config", config, "--no-git", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["pyproject_name"] == init_module._pyproject_name(name)
    audience_docs, audience_docs_json = init_module._next_step_audience_docs_commands(template)
    expected_commands = [
        f"cd {shlex.quote(str(tmp_path / name))}",
        "cp .env.example .env",
        "uv sync",
        "uv run easycat doctor --env-file .env",
        "uv run easycat doctor --env-file .env --json",
        _template_readme_check_command(template),
        _template_readme_fix_command(template),
        "uv run easycat docs",
        audience_docs,
        audience_docs_json,
        "uv run easycat docs --json",
        "uv run easycat explain json-schema",
        _template_readme_run_command(template),
    ]
    assert payload["run_command"] == _template_readme_run_command(template)
    assert payload["check_command"] == _template_readme_check_command(template)
    assert payload["fix_command"] == _template_readme_fix_command(template)
    assert payload["next_step_commands"] == expected_commands
    assert "after cd into the scaffolded project" in payload["command_note"]


def test_init_json_next_step_commands_quote_project_path(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(app, ["init", "demo project", "--config", config, "--no-git", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["pyproject_name"] == "demo-project"
    assert payload["next_step_commands"][0] == f"cd {shlex.quote(str(tmp_path / 'demo project'))}"


def test_init_json_pyproject_name_falls_back_for_symbol_only_name(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(app, ["init", "!!!", "--config", config, "--no-git", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["pyproject_name"] == "easycat-agent"
    assert 'name = "easycat-agent"' in (tmp_path / "!!!" / "pyproject.toml").read_text()


def test_init_list_templates_json_catalog_includes_next_step_commands(cli: CliRunner) -> None:
    result = cli.invoke(app, ["init", "--list-templates", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    catalog = {entry["name"]: entry for entry in payload["catalog"]}

    for template in available_templates():
        assert catalog[template]["next_step_commands"] == init_module._next_step_commands(
            Path("my-agent"), template
        )
        assert catalog[template]["run_command"] == _template_readme_run_command(template)
        assert catalog[template]["check_command"] == _template_readme_check_command(template)
        assert catalog[template]["fix_command"] == _template_readme_fix_command(template)


# ── easycat source wiring (pre-launch local checkout) ────────────────


def _fake_checkout(tmp_path: Path) -> Path:
    checkout = tmp_path / "easycat-checkout"
    checkout.mkdir()
    (checkout / "pyproject.toml").write_text('[project]\nname = "easycat"\n', encoding="utf-8")
    return checkout


def test_init_easycat_source_flag_writes_uv_sources_block(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(
        app,
        ["init", "demo", "--config", config, "--no-git", "--easycat-source", str(REPO_ROOT)],
    )

    assert result.exit_code == 0, result.stderr
    pyproject = tomllib.loads((tmp_path / "demo" / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["uv"]["sources"]["easycat"] == {
        "path": str(REPO_ROOT),
        "editable": True,
    }
    assert "local checkout" in result.stderr


def test_init_config_easycat_source_writes_uv_sources_block(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    checkout = _fake_checkout(tmp_path)
    config = json.dumps(
        {"schema_version": 1, "template": "text-chat", "easycat_source": str(checkout)}
    )
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])

    assert result.exit_code == 0, result.stderr
    pyproject = tomllib.loads((tmp_path / "demo" / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["uv"]["sources"]["easycat"] == {
        "path": str(checkout),
        "editable": True,
    }


def test_init_easycat_source_flag_overrides_config_field(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    checkout = _fake_checkout(tmp_path)
    config = json.dumps(
        {"schema_version": 1, "template": "text-chat", "easycat_source": str(checkout)}
    )
    result = cli.invoke(
        app,
        ["init", "demo", "--config", config, "--no-git", "--easycat-source", str(REPO_ROOT)],
    )

    assert result.exit_code == 0, result.stderr
    pyproject = (tmp_path / "demo" / "pyproject.toml").read_text(encoding="utf-8")
    assert str(REPO_ROOT) in pyproject
    assert str(checkout) not in pyproject


def test_init_auto_detects_editable_install_source(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    checkout = _fake_checkout(tmp_path)
    monkeypatch.setattr(init_module, "_editable_easycat_source", lambda: checkout)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])

    assert result.exit_code == 0, result.stderr
    pyproject = tomllib.loads((tmp_path / "demo" / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["uv"]["sources"]["easycat"] == {
        "path": str(checkout),
        "editable": True,
    }


def test_init_published_install_renders_no_sources_block(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(init_module, "_editable_easycat_source", lambda: None)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])

    assert result.exit_code == 0, result.stderr
    pyproject = (tmp_path / "demo" / "pyproject.toml").read_text(encoding="utf-8")
    assert "$EASYCAT_SOURCES_BLOCK" not in pyproject
    assert "[tool.uv.sources]" not in pyproject
    assert pyproject.endswith(
        '[tool.easycat.scaffold]\ntemplate = "text-chat"\n'
        'required_env = ["OPENAI_API_KEY"]\noptional_env = []\n'
    )


def test_init_easycat_source_rejects_path_without_pyproject(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    empty = tmp_path / "not-a-checkout"
    empty.mkdir()
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(
        app,
        ["init", "demo", "--config", config, "--no-git", "--easycat-source", str(empty)],
    )

    assert result.exit_code == 4
    assert "EASYCAT_E102" in result.stderr
    assert "pyproject.toml" in result.stderr
    assert not (tmp_path / "demo").exists()


def test_init_json_reports_easycat_source(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(
        app,
        [
            "init",
            "demo",
            "--config",
            config,
            "--no-git",
            "--json",
            "--easycat-source",
            str(REPO_ROOT),
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["easycat_source"] == str(REPO_ROOT)


def test_init_json_easycat_source_is_null_for_published_install(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(init_module, "_editable_easycat_source", lambda: None)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git", "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["easycat_source"] is None


# ── portable Git source wiring ──────────────────────────────────────


_EASYCAT_GIT_URL = "https://github.com/yisding/easycat.git"
_EASYCAT_GIT_REV = "0123456789abcdef0123456789abcdef01234567"


def test_init_easycat_git_flag_writes_portable_uv_source(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(
        app,
        [
            "init",
            "demo",
            "--config",
            config,
            "--no-git",
            "--easycat-git",
            _EASYCAT_GIT_URL,
            "--easycat-git-rev",
            _EASYCAT_GIT_REV,
        ],
    )

    assert result.exit_code == 0, result.stderr
    pyproject_text = (tmp_path / "demo" / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = tomllib.loads(pyproject_text)
    assert pyproject["tool"]["uv"]["sources"]["easycat"] == {
        "git": _EASYCAT_GIT_URL,
        "rev": _EASYCAT_GIT_REV,
    }
    assert str(REPO_ROOT) not in pyproject_text
    assert "portable Git source" in result.stderr


def test_init_config_easycat_git_writes_portable_uv_source(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "text-chat",
            "easycat_git": "git@github.com:yisding/easycat.git",
            "easycat_git_rev": "feature/portable-scaffold",
        }
    )
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])

    assert result.exit_code == 0, result.stderr
    pyproject = tomllib.loads((tmp_path / "demo" / "pyproject.toml").read_text())
    assert pyproject["tool"]["uv"]["sources"]["easycat"] == {
        "git": "git@github.com:yisding/easycat.git",
        "rev": "feature/portable-scaffold",
    }


def test_init_easycat_git_flag_overrides_config_git_source(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps(
        {
            "schema_version": 1,
            "template": "text-chat",
            "easycat_git": "https://example.com/old/easycat.git",
            "easycat_git_rev": "old-revision",
        }
    )
    result = cli.invoke(
        app,
        [
            "init",
            "demo",
            "--config",
            config,
            "--no-git",
            "--easycat-git",
            _EASYCAT_GIT_URL,
        ],
    )

    assert result.exit_code == 0, result.stderr
    pyproject = tomllib.loads((tmp_path / "demo" / "pyproject.toml").read_text())
    assert pyproject["tool"]["uv"]["sources"]["easycat"] == {"git": _EASYCAT_GIT_URL}


@pytest.mark.parametrize(
    ("config_source", "extra_args"),
    [
        (
            {"easycat_source": str(REPO_ROOT)},
            ["--easycat-git", _EASYCAT_GIT_URL],
        ),
        (
            {"easycat_git": _EASYCAT_GIT_URL},
            ["--easycat-source", str(REPO_ROOT)],
        ),
        (
            {"easycat_source": str(REPO_ROOT), "easycat_git": _EASYCAT_GIT_URL},
            [],
        ),
    ],
)
def test_init_rejects_mixed_local_and_git_sources(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_source: dict[str, str],
    extra_args: list[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat", **config_source})
    result = cli.invoke(
        app,
        ["init", "demo", "--config", config, "--no-git", *extra_args],
    )

    assert result.exit_code == 4
    assert "mutually exclusive" in result.stderr
    assert not (tmp_path / "demo").exists()


def test_init_rejects_git_revision_without_url(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(
        app,
        [
            "init",
            "demo",
            "--config",
            config,
            "--no-git",
            "--easycat-git-rev",
            "main",
        ],
    )

    assert result.exit_code == 4
    assert "revision requires --easycat-git" in " ".join(result.stderr.split())
    assert not (tmp_path / "demo").exists()


@pytest.mark.parametrize(
    "git_url",
    [
        "",
        "../easycat",
        "file:///home/developer/easycat",
        "https://token@example.com/yisding/easycat.git",
        "https://github.com/yisding/easycat.git?access_token=secret",
        "https://github.com/yisding/easycat.git#access_token=secret",
        "git@github.com:yisding/easycat.git?access_token=secret",
        "git@github.com:yisding/easycat.git#access_token=secret",
        "https://github.com/yisding/easycat repo.git",
    ],
)
def test_init_rejects_nonportable_or_credential_bearing_git_urls(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_url: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(
        app,
        [
            "init",
            "demo",
            "--config",
            config,
            "--no-git",
            "--easycat-git",
            git_url,
        ],
    )

    assert result.exit_code == 4
    assert "invalid easycat Git source" in result.stderr
    assert not (tmp_path / "demo").exists()


def test_init_json_reports_portable_git_source(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(
        app,
        [
            "init",
            "demo",
            "--config",
            config,
            "--no-git",
            "--json",
            "--easycat-git",
            _EASYCAT_GIT_URL,
            "--easycat-git-rev",
            _EASYCAT_GIT_REV,
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["easycat_source"] is None
    assert payload["easycat_git"] == _EASYCAT_GIT_URL
    assert payload["easycat_git_rev"] == _EASYCAT_GIT_REV
