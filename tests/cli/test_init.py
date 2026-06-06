"""``easycat init`` — scaffolding flows, error paths, and templates."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.cli.scaffold import init as init_module
from easycat.cli.scaffold._schema import available_templates
from easycat.stt.factory import available_providers as available_stt_providers
from easycat.tts.factory import available_providers as available_tts_providers

REPO_ROOT = Path(__file__).resolve().parents[2]

# ── --list-templates and basic flows ─────────────────────────────────


def test_list_templates(cli: CliRunner) -> None:
    result = cli.invoke(app, ["init", "--list-templates"])
    assert result.exit_code == 0
    names = [
        line.split()[0] for line in result.stdout.splitlines() if line and not line[0].isspace()
    ]
    assert "openai-agents" in names
    assert "pydantic-ai" in names
    assert "pydantic-ai-workflow" in names
    assert "text-chat" in names
    assert "twilio-phone" in names
    assert "webrtc-browser" in names
    assert "best first voice scaffold" in result.stdout
    assert "Text-only REPL" in result.stdout
    assert "WebRTC audio" in result.stdout


def test_list_templates_json(cli: CliRunner) -> None:
    result = cli.invoke(app, ["init", "--list-templates", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert set(payload["templates"]) == set(available_templates())
    catalog = {entry["name"]: entry for entry in payload["catalog"]}
    assert set(catalog) == set(available_templates())
    assert catalog["openai-agents"]["transport"] == "local mic"
    assert catalog["text-chat"]["mode"] == "text"
    assert "description" in catalog["webrtc-browser"]


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
    assert "openai-agents,telephony" in pyproject
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


def test_init_omits_cache_artifacts(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No generated path may contain a cache dir or a compiled .pyc."""
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "openai-agents"})
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git", "--json"])
    assert result.exit_code == 0, result.stderr
    project = tmp_path / "demo"

    forbidden = {"__pycache__", ".ruff_cache", ".pytest_cache", ".mypy_cache"}
    for path in project.rglob("*"):
        parts = set(path.relative_to(project).parts)
        assert not (parts & forbidden), f"shipped a cache artifact: {path}"
        assert path.suffix != ".pyc", f"shipped a compiled artifact: {path}"

    # The reported file manifest is equally clean.
    payload = json.loads(result.stdout)
    for rel in payload["files"]:
        assert not (set(Path(rel).parts) & forbidden), rel
        assert not rel.endswith(".pyc"), rel

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

    missing_extras = sorted(provider_names - set(init_module._PROVIDER_TO_EXTRA))
    missing_env_vars = sorted(provider_names - set(init_module._PROVIDER_TO_ENV_VAR))
    unknown_extras = sorted(set(init_module._PROVIDER_TO_EXTRA.values()) - known_extras)

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


def test_init_next_steps_load_env_for_doctor(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    config = json.dumps({"schema_version": 1, "template": "text-chat"})
    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git"])
    assert result.exit_code == 0, result.stderr
    assert "uv run easycat doctor --env-file .env" in result.stderr
    assert "uv run easycat docs" in result.stderr
    assert "uvx easycat doctor" not in result.stderr


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
