"""``easycat console`` — keyless first-run demo with explicit live opt-in.

The loop is driven via piped stdin (CliRunner ``input=``), matching how a
user would pipe text into the command. Every mode must end by printing the
exported debug bundle path plus a ``easycat replay <bundle>`` hint — the
journal is the payoff of the first run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from easycat.cli import console as console_module
from easycat.cli._app import app
from easycat.cli.diagnose.doctor import CheckResult

REPLAY_HINT_RE = re.compile(r"Replay this session: easycat replay (?P<path>\S+\.zip)")


def _saved_bundle_path(output: str) -> Path:
    match = REPLAY_HINT_RE.search(output)
    assert match, f"No replay hint in output:\n{output}"
    return Path(match.group("path"))


# ── Explicit mode selection ────────────────────────────────────────


def test_select_mode_voice_demo_flag_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert console_module._select_mode(voice_demo=True, live=False) == "voice-demo"


def test_select_mode_keyless_defaults_to_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert console_module._select_mode(voice_demo=False, live=False) == "keyless-text"


def test_select_mode_ignores_ambient_key_without_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials alone must never opt the default console into provider traffic."""
    import easycat.cli.diagnose.doctor as doctor_module

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        doctor_module,
        "check_microphone",
        lambda: pytest.fail("default console should not probe the microphone"),
    )

    assert console_module._select_mode(voice_demo=False, live=False) == "keyless-text"


def test_select_mode_key_and_microphone_is_live_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import easycat.cli.diagnose.doctor as doctor_module

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        doctor_module,
        "check_microphone",
        lambda: CheckResult(name="microphone", status="ok", detail="default input: test"),
    )
    assert console_module._select_mode(voice_demo=False, live=True) == "live-voice"


@pytest.mark.parametrize("mic_status", ["skip", "fail"])
def test_select_mode_key_without_microphone_is_live_text(
    monkeypatch: pytest.MonkeyPatch,
    mic_status: str,
) -> None:
    import easycat.cli.diagnose.doctor as doctor_module

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        doctor_module,
        "check_microphone",
        lambda: CheckResult(name="microphone", status=mic_status, detail="no device"),
    )
    assert console_module._select_mode(voice_demo=False, live=True) == "live-text"


def test_select_mode_live_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    with pytest.raises(typer.BadParameter, match="OPENAI_API_KEY"):
        console_module._select_mode(voice_demo=False, live=True)


# ── Keyless text loop (piped stdin) ────────────────────────────────


def test_console_keyless_loop_echoes_and_prints_replay_hint(
    cli: CliRunner,
    empty_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = cli.invoke(app, ["console"], input="hello easycat\n")

    assert result.exit_code == 0, result.output
    assert "bot: hello easycat" in result.stdout
    assert "Saved debug bundle:" in result.stdout
    bundle = _saved_bundle_path(result.stdout)
    assert bundle.is_file()
    assert bundle.parent == Path(".easycat/recordings")


def test_console_keyless_loop_stops_on_exit_command(
    cli: CliRunner,
    empty_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = cli.invoke(app, ["console"], input="first turn\nexit\nnever sent\n")

    assert result.exit_code == 0, result.output
    assert "bot: first turn" in result.stdout
    assert "bot: never sent" not in result.stdout
    assert "Replay this session: easycat replay" in result.stdout


def test_console_keyless_loop_journals_every_turn(
    cli: CliRunner,
    empty_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = cli.invoke(app, ["console"], input="one\ntwo\n")

    assert result.exit_code == 0, result.output
    bundle = _saved_bundle_path(result.stdout)

    replay = cli.invoke(app, ["replay", str(bundle), "--json"])
    assert replay.exit_code == 0, replay.output
    payload = json.loads(replay.stdout)
    assert payload["status"] == "ok"


def test_console_honors_record_to_directory(
    cli: CliRunner,
    empty_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = cli.invoke(app, ["console", "--record-to", "bundles"], input="hi\n")

    assert result.exit_code == 0, result.output
    bundle = _saved_bundle_path(result.stdout)
    assert bundle.parent == Path("bundles")
    assert bundle.is_file()


# ── Scripted voice demo (full audio pipeline, no keys) ─────────────


def test_console_voice_demo_runs_one_scripted_turn(
    cli: CliRunner,
    empty_env: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = cli.invoke(app, ["console", "--voice-demo"])

    assert result.exit_code == 0, result.output
    assert "you (scripted): Hello, EasyCat!" in result.stdout
    assert "bot: You said: Hello, EasyCat!" in result.stdout
    bundle = _saved_bundle_path(result.stdout)
    assert bundle.is_file()

    replay = cli.invoke(app, ["replay", str(bundle), "--json"])
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.stdout)["status"] == "ok"


def test_console_voice_demo_ignores_api_key(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--voice-demo stays scripted/keyless even when a key is present."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    result = cli.invoke(app, ["console", "--voice-demo"])

    assert result.exit_code == 0, result.output
    assert "you (scripted): Hello, EasyCat!" in result.stdout
    assert "Replay this session: easycat replay" in result.stdout


def test_console_default_stays_keyless_with_ambient_key(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    result = cli.invoke(app, ["console"], input="offline turn\n")

    assert result.exit_code == 0, result.output
    assert "bot: offline turn" in result.stdout
    assert "Ambient API keys are not used" in result.stderr
    assert "billable" not in result.output.lower()


def test_console_live_requires_explicit_key(cli: CliRunner, empty_env: None) -> None:
    result = cli.invoke(app, ["console", "--live"])

    assert result.exit_code == 2
    assert "OPENAI_API_KEY" in result.output


def test_console_rejects_live_voice_demo_combination(cli: CliRunner) -> None:
    result = cli.invoke(app, ["console", "--live", "--voice-demo"])

    assert result.exit_code == 2
    assert "cannot be used together" in result.output


# ── Interactive-only contract ──────────────────────────────────────


def test_console_has_no_json_flag(cli: CliRunner) -> None:
    result = cli.invoke(app, ["console", "--help"])
    assert result.exit_code == 0
    assert "--voice-demo" in result.stdout
    assert "--live" in result.stdout
    assert "incur charges" in " ".join(result.stdout.split())
    assert "--record-to" in result.stdout
    assert "--json" not in result.stdout


def test_console_rejects_json_flag(cli: CliRunner, empty_env: None) -> None:
    result = cli.invoke(app, ["console", "--json"])
    assert result.exit_code == 2


def test_explain_json_schema_documents_console_exemption(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "json-schema"])
    stdout = re.sub(r"\s+", " ", result.stdout)
    assert result.exit_code == 0
    assert "`easycat console` is interactive-only" in stdout
    assert "never accepts `--json`" in stdout
    assert "easycat replay PATH --json" in stdout
