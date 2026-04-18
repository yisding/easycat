"""``easycat init`` — scaffolding flows, error paths, and templates."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.cli.scaffold._schema import available_templates

# ── --list-templates and basic flows ─────────────────────────────────


def test_list_templates(cli: CliRunner) -> None:
    result = cli.invoke(app, ["init", "--list-templates"])
    assert result.exit_code == 0
    names = result.stdout.strip().splitlines()
    assert "openai-agents" in names
    assert "pydantic-ai" in names
    assert "text-chat" in names


def test_list_templates_json(cli: CliRunner) -> None:
    result = cli.invoke(app, ["init", "--list-templates", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert set(payload["templates"]) == set(available_templates())


def test_missing_name_without_list_templates(cli: CliRunner) -> None:
    """`easycat init` with no NAME and no --list-templates exits 2."""
    result = cli.invoke(app, ["init"])
    assert result.exit_code == 2
    assert "Missing argument 'NAME'" in result.stderr


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
