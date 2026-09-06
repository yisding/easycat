"""``easycat doctor`` — environment checks and --json envelope."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import namedtuple
from collections.abc import Iterator
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from easycat.cli._app import _COMMAND_TEXT, app
from easycat.cli.diagnose import _requirements as requirements_module
from easycat.cli.diagnose import doctor as doctor_module
from easycat.cli.diagnose._requirements import (
    RoleRequirement,
    SelectedApp,
    dependency_source,
    install_fix,
    selected_app_from_manifest,
)
from easycat.errors import EASYCAT_E202, REGISTRY, SetupIssue
from easycat.planning import provider_plan
from easycat.planning.selection import build_manifest_plan
from easycat.project import load_manifest

_DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])

_TELNYX_REQUIRED_ENV = (
    'required_env = ["OPENAI_API_KEY", "TELNYX_STREAM_URL", "TELNYX_API_KEY", "TELNYX_PUBLIC_KEY"]'
)


def _write_telnyx_scaffold(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f"""
[tool.easycat.scaffold]
template = "telnyx-phone"
{_TELNYX_REQUIRED_ENV}
""".strip(),
        encoding="utf-8",
    )


def _plain_console() -> tuple[StringIO, Console]:
    stream = StringIO()
    return stream, Console(file=stream, force_terminal=False, no_color=True, width=120)


def _stub_sufficient_disk_space(monkeypatch: pytest.MonkeyPatch) -> None:
    capacity = 2 * 1024**3
    monkeypatch.setattr(
        "shutil.disk_usage",
        lambda _path: _DiskUsage(total=capacity, used=capacity // 2, free=capacity // 2),
    )


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stub ``httpx.head`` so tests don't hit real provider endpoints."""

    def fake_head(url, *, timeout=0.0, follow_redirects=False, **kw):
        class _R:
            status_code = 200

        return _R()

    monkeypatch.setattr("httpx.head", fake_head)
    yield


def test_doctor_all_skips_when_no_keys(cli: CliRunner, empty_env: None, no_network: None) -> None:
    result = cli.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stderr
    assert "EasyCat doctor" in result.stderr
    assert "valid for" in result.stderr
    assert "keyless/local/custom setups" in result.stderr
    assert "EASYCAT_E203" not in result.stderr


def test_doctor_help_names_first_run_checks(cli: CliRunner) -> None:
    result = cli.invoke(app, ["doctor", "--help"])
    help_text = " ".join(result.stdout.split())

    assert result.exit_code == 0
    assert "configured credentials" in help_text
    assert "network liveness" in help_text
    assert "--env-file" in result.stdout
    assert "for example, .env" in help_text
    assert "--provider" in result.stdout


def test_doctor_passes_with_one_key(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    no_network: None,
) -> None:
    for var in (
        "OPENAI_API_KEY",
        "DEEPGRAM_API_KEY",
        "ELEVENLABS_API_KEY",
        "CARTESIA_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("NO_COLOR", "1")
    result = cli.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stderr
    assert "openai network reachable" in result.stderr


def test_doctor_treats_whitespace_credentials_as_missing(
    monkeypatch: pytest.MonkeyPatch,
    empty_env: None,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", " \t ")

    scoped = doctor_module.check_env_vars("openai")
    assert [(check.name, check.status) for check in scoped] == [("env_openai", "fail")]

    unscoped = {check.name: check for check in doctor_module.check_env_vars()}
    assert unscoped["env_openai"].status == "skip"
    assert unscoped["env_any"].status == "skip"
    assert doctor_module.check_provider_reachability("openai") == []


def test_doctor_passes_with_cartesia_only(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    no_network: None,
) -> None:
    """Cartesia-only setups must not trip env_any/E203 or `--provider cartesia`."""
    for var in (
        "OPENAI_API_KEY",
        "DEEPGRAM_API_KEY",
        "ELEVENLABS_API_KEY",
        "CARTESIA_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CARTESIA_API_KEY", "ck-stub")
    monkeypatch.setenv("NO_COLOR", "1")
    result = cli.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stderr
    assert "cartesia network reachable" in result.stderr
    scoped = cli.invoke(app, ["doctor", "--provider", "cartesia"])
    assert scoped.exit_code == 0, scoped.stderr


def test_doctor_json_envelope(cli: CliRunner, empty_env: None, no_network: None) -> None:
    result = cli.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "doctor"
    assert payload["status"] == "ok"
    assert payload["environment"] == "dev"
    # Every check exposes both outcome and configuration relevance.
    for check in payload["checks"]:
        assert {"name", "status", "detail", "requirement"} <= set(check)
    assert {check["requirement"] for check in payload["checks"]} == {
        "required",
        "optional",
        "unused",
        "not_applicable",
    }
    env_any = next(check for check in payload["checks"] if check["name"] == "env_any")
    assert env_any["status"] == "skip"
    assert env_any["requirement"] == "not_applicable"
    assert "fix" not in env_any


def test_doctor_env_file_loads_keys_and_restores_env(
    cli: CliRunner,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# local scaffold env\nOPENAI_API_KEY='sk-from-file'\nDEEPGRAM_API_KEY=\"dg-from-file\"\n",
        encoding="utf-8",
    )

    result = cli.invoke(app, ["doctor", "--env-file", str(env_file), "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["env_openai"]["status"] == "ok"
    assert checks["env_deepgram"]["status"] == "ok"
    assert os.getenv("OPENAI_API_KEY") is None
    assert os.getenv("DEEPGRAM_API_KEY") is None


def test_doctor_env_file_exported_variables_win_over_file_defaults(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    """A real exported credential must not be replaced by a `.env` placeholder."""
    import sys

    # Skip the optional local-mic probe so the run's exit code only
    # reflects credential state on machines without PortAudio.
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=your-key\nDEEPGRAM_API_KEY=dg-from-file\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-exported")

    result = cli.invoke(app, ["doctor", "--env-file", str(env_file), "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["env_openai"]["detail"] == "OPENAI_API_KEY set"
    assert checks["env_openai"]["status"] == "ok"
    # File-only keys still load.
    assert checks["env_deepgram"]["status"] == "ok"
    # The exported variable is left untouched; only file-loaded keys are
    # restored.
    assert os.getenv("OPENAI_API_KEY") == "sk-real-exported"
    assert os.getenv("DEEPGRAM_API_KEY") is None


def test_doctor_env_file_ignores_non_provider_variables(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
) -> None:
    env_file = tmp_path / ".env"
    attacker_path = str(tmp_path / "bin")
    attacker_proxy = "http://127.0.0.1:8765"
    attacker_cache = str(tmp_path / "cache")
    env_file.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=sk-from-file",
                f"PATH={attacker_path}",
                f"HTTPS_PROXY={attacker_proxy}",
                f"XDG_CACHE_HOME={attacker_cache}",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)

    def fake_head(url, *, timeout=0.0, follow_redirects=False, **kw):
        assert os.environ["OPENAI_API_KEY"] == "sk-from-file"
        assert os.environ.get("HTTPS_PROXY") != attacker_proxy
        assert os.environ.get("XDG_CACHE_HOME") != attacker_cache
        assert os.environ.get("PATH") != attacker_path

        class _R:
            status_code = 200

        return _R()

    monkeypatch.setattr("httpx.head", fake_head)

    result = cli.invoke(app, ["doctor", "--env-file", str(env_file), "--json"])

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["env_openai"]["status"] == "ok"
    assert os.getenv("OPENAI_API_KEY") is None
    assert os.getenv("HTTPS_PROXY") is None
    assert os.getenv("XDG_CACHE_HOME") is None


def test_doctor_env_file_rejects_invalid_lines(
    cli: CliRunner,
    tmp_path: Path,
    empty_env: None,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY sk-from-file\n", encoding="utf-8")

    result = cli.invoke(app, ["doctor", "--env-file", str(env_file)])

    assert result.exit_code == 2
    assert "Invalid --env-file" in result.stderr
    assert "expected" in result.stderr
    assert "KEY=VALUE" in result.stderr


def test_doctor_env_file_reports_extra_tokens_once(
    cli: CliRunner,
    tmp_path: Path,
    empty_env: None,
) -> None:
    """The location prefix must appear once, not doubled by a re-wrap."""
    env_file = tmp_path / ".env"
    env_file.write_text('OPENAI_API_KEY="a" "b"\n', encoding="utf-8")

    result = cli.invoke(app, ["doctor", "--env-file", str(env_file)])

    assert result.exit_code == 2
    assert "extra tokens after quoted value" in result.stderr
    assert result.stderr.count("invalid .env syntax") == 1


def test_doctor_env_file_rejects_invalid_lines_json_envelope(
    cli: CliRunner,
    tmp_path: Path,
    empty_env: None,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY sk-from-file\n", encoding="utf-8")

    result = cli.invoke(app, ["doctor", "--env-file", str(env_file), "--json"])

    assert result.exit_code == 2
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "doctor"
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert "Invalid --env-file" in payload["message"]
    assert "expected" in payload["message"]
    assert "KEY=VALUE" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_doctor_unknown_environment(cli: CliRunner, empty_env: None) -> None:
    result = cli.invoke(app, ["doctor", "--environment", "bogus"])
    assert result.exit_code == 2
    assert "Unknown --environment" in result.stderr


def test_doctor_unknown_environment_json_envelope(cli: CliRunner, empty_env: None) -> None:
    result = cli.invoke(app, ["doctor", "--environment", "bogus", "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "doctor"
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert "Unknown --environment" in payload["message"]


def test_doctor_production_drops_microphone_check(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    no_network: None,
) -> None:
    """The production profile is server-oriented and skips the local mic
    probe; the dev profile still includes it."""
    for var in (
        "OPENAI_API_KEY",
        "DEEPGRAM_API_KEY",
        "ELEVENLABS_API_KEY",
        "CARTESIA_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("NO_COLOR", "1")

    dev = cli.invoke(app, ["doctor", "--json"])
    dev_names = {c["name"] for c in json.loads(dev.stdout)["checks"]}
    assert "microphone" in dev_names

    prod = cli.invoke(app, ["doctor", "--environment", "production", "--json"])
    prod_names = {c["name"] for c in json.loads(prod.stdout)["checks"]}
    assert "microphone" not in prod_names


def test_doctor_only_provider_filters_reachability(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    no_network: None,
) -> None:
    for var in (
        "OPENAI_API_KEY",
        "DEEPGRAM_API_KEY",
        "ELEVENLABS_API_KEY",
        "CARTESIA_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-stub")
    monkeypatch.setenv("NO_COLOR", "1")
    result = cli.invoke(app, ["doctor", "--provider", "openai"])
    assert result.exit_code == 0
    assert "reach_openai" in result.stderr
    assert "reach_deepgram" not in result.stderr


def test_doctor_only_provider_fails_when_its_key_missing(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    no_network: None,
) -> None:
    """--provider X must fail (not false-green) when X's key is unset,
    even if a *different* provider's key happens to be set."""
    for var in (
        "OPENAI_API_KEY",
        "DEEPGRAM_API_KEY",
        "ELEVENLABS_API_KEY",
        "CARTESIA_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-stub")
    monkeypatch.setenv("NO_COLOR", "1")
    result = cli.invoke(app, ["doctor", "--provider", "openai"])
    # Whitespace-normalized: the fix now comes from EASYCAT_E203's registry
    # entry, which is long enough that Rich soft-wraps it inside the report
    # table. The guidance an operator reads is unchanged; only the column the
    # line break lands in is.
    stderr = " ".join(result.stderr.split())
    assert result.exit_code == 1
    assert "EASYCAT_E203" in stderr
    assert "OPENAI_API_KEY" in stderr
    assert "--env-file" in stderr
    assert ".env" in stderr
    assert "easycat doctor --env-file .env" in stderr


def test_doctor_unknown_provider_is_usage_error(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    no_network: None,
) -> None:
    """A typo or mis-cased --provider exits 2, not 0 (false-green guard)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("NO_COLOR", "1")
    result = cli.invoke(app, ["doctor", "--provider", "OpenAI"])
    assert result.exit_code == 2
    assert "Unknown --provider" in result.stderr


def test_doctor_unknown_provider_json_envelope(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    no_network: None,
) -> None:
    """Scoped provider typos still emit parseable stdout in JSON mode."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("NO_COLOR", "1")
    result = cli.invoke(app, ["doctor", "--provider", "OpenAI", "--json"])
    assert result.exit_code == 2
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "doctor"
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert "Unknown --provider" in payload["message"]
    assert "openai" in payload["message"]


def test_doctor_reports_httpx_failure(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ConnectError on the probe should surface as E204."""
    import httpx

    for var in ("DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY", "CARTESIA_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("NO_COLOR", "1")

    def raising_head(url, **kw):
        raise httpx.ConnectError("no route")

    monkeypatch.setattr("httpx.head", raising_head)
    result = cli.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "EASYCAT_E204" in result.stderr


def test_doctor_rejects_obvious_placeholder_credential(
    cli: CliRunner,
    empty_env: None,
    no_network: None,
) -> None:
    result = cli.invoke(
        app,
        ["doctor", "--provider", "openai"],
        env={"OPENAI_API_KEY": "sk-your-key-here"},
    )

    assert result.exit_code == 1
    assert "looks like a placeholder" in result.stderr
    assert "EASYCAT_E203" in result.stderr
    assert "reach_openai" not in result.stderr


def test_doctor_network_probe_does_not_claim_credential_validity(
    monkeypatch: pytest.MonkeyPatch,
    empty_env: None,
) -> None:
    class Unauthorized:
        status_code = 401

    monkeypatch.setenv("OPENAI_API_KEY", "sk-configured")
    monkeypatch.setattr("httpx.head", lambda *args, **kwargs: Unauthorized())

    result = doctor_module.check_provider_reachability("openai")

    assert len(result) == 1
    assert result[0].status == "ok"
    assert "network reachable (HTTP 401)" in result[0].detail
    assert "credential validity not checked" in result[0].detail


def test_doctor_uses_scaffold_metadata_to_validate_all_twilio_requirements(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.easycat.scaffold]
template = "twilio-phone"
required_env = ["OPENAI_API_KEY", "TWILIO_STREAM_URL", "TWILIO_AUTH_TOKEN"]
""".strip(),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
OPENAI_API_KEY=sk-real-value
TWILIO_STREAM_URL=wss://your-public-host:8766
TWILIO_AUTH_TOKEN=your-twilio-auth-token
""".strip(),
        encoding="utf-8",
    )

    result = cli.invoke(app, ["doctor", "--env-file", str(env_file), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["scaffold"]["status"] == "ok"
    assert checks["env_openai"]["status"] == "ok"
    assert checks["env_twilio_stream_url"]["status"] == "fail"
    assert checks["env_twilio_auth_token"]["status"] == "fail"
    assert checks["env_twilio_stream_url"]["code"] == "EASYCAT_E210"


def test_doctor_scaffold_requirements_pass_with_real_values(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.easycat.scaffold]
template = "twilio-phone"
required_env = ["OPENAI_API_KEY", "TWILIO_STREAM_URL", "TWILIO_AUTH_TOKEN"]
""".strip(),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
OPENAI_API_KEY=sk-real-value
TWILIO_STREAM_URL=wss://voice.example.net/media
TWILIO_AUTH_TOKEN=0123456789abcdef0123456789abcdef
""".strip(),
        encoding="utf-8",
    )

    result = cli.invoke(app, ["doctor", "--env-file", str(env_file), "--json"])

    assert result.exit_code == 0, result.stderr
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["env_twilio_stream_url"]["status"] == "ok"
    assert checks["env_twilio_auth_token"]["status"] == "ok"


def test_doctor_rejects_invalid_numeric_twilio_scaffold_values(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.easycat.scaffold]
template = "twilio-phone"
required_env = ["OPENAI_API_KEY"]
optional_env = [
    "TWILIO_WS_PORT",
    "TWILIO_MAX_SESSIONS",
    "TWILIO_START_TIMEOUT_S",
    "TWILIO_DRAIN_TIMEOUT_S",
    "TWILIO_FORCE_SHUTDOWN_TIMEOUT_S",
]
""".strip(),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
OPENAI_API_KEY=sk-real-value
TWILIO_WS_PORT=abc
TWILIO_MAX_SESSIONS=0
TWILIO_START_TIMEOUT_S=nan
TWILIO_DRAIN_TIMEOUT_S=-1
TWILIO_FORCE_SHUTDOWN_TIMEOUT_S=inf
""".strip(),
        encoding="utf-8",
    )

    result = cli.invoke(app, ["doctor", "--env-file", str(env_file), "--json"])

    assert result.exit_code == 1
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    for name in (
        "twilio_ws_port",
        "twilio_max_sessions",
        "twilio_start_timeout_s",
        "twilio_drain_timeout_s",
        "twilio_force_shutdown_timeout_s",
    ):
        check = checks[f"env_{name}"]
        assert check["status"] == "fail"
        assert check["requirement"] == "optional"
        assert check["code"] == "EASYCAT_E210"


def test_doctor_uses_scaffold_metadata_to_validate_all_telnyx_requirements(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    """TELNYX_* scaffold values mirror TWILIO_*: placeholders and non-wss:// URLs
    fail with E210."""
    monkeypatch.chdir(tmp_path)
    _write_telnyx_scaffold(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
OPENAI_API_KEY=sk-real-value
TELNYX_STREAM_URL=https://not-a-ws-host:8766
TELNYX_API_KEY=your-telnyx-api-key
TELNYX_PUBLIC_KEY=your_telnyx_public_key
""".strip(),
        encoding="utf-8",
    )

    result = cli.invoke(app, ["doctor", "--env-file", str(env_file), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["scaffold"]["status"] == "ok"
    assert checks["env_openai"]["status"] == "ok"
    assert checks["env_telnyx_stream_url"]["status"] == "fail"
    assert "wss://" in checks["env_telnyx_stream_url"]["detail"]
    for name in ("env_telnyx_api_key", "env_telnyx_public_key"):
        assert checks[name]["status"] == "fail"
        assert "placeholder" in checks[name]["detail"]
    for name in ("env_telnyx_stream_url", "env_telnyx_api_key", "env_telnyx_public_key"):
        assert checks[name]["code"] == "EASYCAT_E210"


def test_doctor_telnyx_scaffold_requirements_pass_with_real_values(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    monkeypatch.chdir(tmp_path)
    _write_telnyx_scaffold(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
OPENAI_API_KEY=sk-real-value
TELNYX_STREAM_URL=wss://voice.example.net/media
TELNYX_API_KEY=telnyx-real-key-value
TELNYX_PUBLIC_KEY=cHVibGljLWtleS1iYXNlNjQtc3RyaW5nLXZhbHVl
""".strip(),
        encoding="utf-8",
    )

    result = cli.invoke(app, ["doctor", "--env-file", str(env_file), "--json"])

    assert result.exit_code == 0, result.stderr
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["env_telnyx_stream_url"]["status"] == "ok"
    assert checks["env_telnyx_api_key"]["status"] == "ok"
    assert checks["env_telnyx_public_key"]["status"] == "ok"


def test_doctor_rejects_invalid_numeric_telnyx_scaffold_values(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.easycat.scaffold]
template = "telnyx-phone"
required_env = ["OPENAI_API_KEY"]
optional_env = [
    "TELNYX_WS_PORT",
    "TELNYX_MAX_SESSIONS",
    "TELNYX_START_TIMEOUT_S",
    "TELNYX_DRAIN_TIMEOUT_S",
    "TELNYX_FORCE_SHUTDOWN_TIMEOUT_S",
]
""".strip(),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        """
OPENAI_API_KEY=sk-real-value
TELNYX_WS_PORT=abc
TELNYX_MAX_SESSIONS=0
TELNYX_START_TIMEOUT_S=nan
TELNYX_DRAIN_TIMEOUT_S=-1
TELNYX_FORCE_SHUTDOWN_TIMEOUT_S=inf
""".strip(),
        encoding="utf-8",
    )

    result = cli.invoke(app, ["doctor", "--env-file", str(env_file), "--json"])

    assert result.exit_code == 1
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    for name in (
        "telnyx_ws_port",
        "telnyx_max_sessions",
        "telnyx_start_timeout_s",
        "telnyx_drain_timeout_s",
        "telnyx_force_shutdown_timeout_s",
    ):
        check = checks[f"env_{name}"]
        assert check["status"] == "fail"
        assert check["requirement"] == "optional"
        assert check["code"] == "EASYCAT_E210"


def test_doctor_scaffold_optional_env_is_allowed_and_non_blocking(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.easycat.scaffold]
template = "webrtc-browser"
required_env = ["OPENAI_API_KEY"]
optional_env = ["TURN_SERVER_URL", "TURN_USERNAME", "TURN_CREDENTIAL", "DEEPGRAM_API_KEY"]
""".strip(),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY=sk-real-value\n", encoding="utf-8")

    result = cli.invoke(app, ["doctor", "--env-file", str(env_file), "--json"])

    assert result.exit_code == 0, result.stdout
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["scaffold"]["detail"].endswith("1 required, 4 optional env vars)")
    for name in (
        "env_turn_server_url",
        "env_turn_username",
        "env_turn_credential",
        "env_deepgram",
    ):
        assert checks[name]["status"] == "skip"
        assert checks[name]["requirement"] == "optional"


def test_doctor_rejects_placeholder_when_optional_env_is_configured(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.easycat.scaffold]
template = "webrtc-browser"
required_env = ["OPENAI_API_KEY"]
optional_env = ["TURN_SERVER_URL"]
""".strip(),
        encoding="utf-8",
    )
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=sk-real-value\nTURN_SERVER_URL=your-turn-url\n",
        encoding="utf-8",
    )

    result = cli.invoke(app, ["doctor", "--env-file", str(env_file), "--json"])

    assert result.exit_code == 1
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["env_turn_server_url"]["status"] == "fail"
    assert checks["env_turn_server_url"]["requirement"] == "optional"


def test_doctor_rejects_overlapping_scaffold_env_metadata(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.easycat.scaffold]
template = "broken"
required_env = ["OPENAI_API_KEY"]
optional_env = ["OPENAI_API_KEY"]
""".strip(),
        encoding="utf-8",
    )

    result = cli.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 2
    assert "cannot be both required and optional" in result.stdout


def test_doctor_scaffold_does_not_probe_unrelated_ambient_provider(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.easycat.scaffold]
template = "openai-agents"
required_env = ["OPENAI_API_KEY"]
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-value")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-unrelated")
    probed: list[str] = []

    def fake_head(url: str, **_kwargs: object) -> object:
        probed.append(url)
        if "deepgram" in url:
            raise AssertionError("unused provider must not be probed")
        return type("Response", (), {"status_code": 200})()

    monkeypatch.setattr("httpx.head", fake_head)

    result = cli.invoke(app, ["doctor", "--json"])

    assert result.exit_code == 0, result.stderr
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["env_openai"]["requirement"] == "required"
    assert checks["env_deepgram"]["status"] == "skip"
    assert checks["env_deepgram"]["requirement"] == "unused"
    assert probed == [doctor_module._PROVIDER_PROBE_URL["openai"]]


def test_doctor_check_functions_are_pure() -> None:
    """Each individual check returns a CheckResult; no side effects."""
    py_check = doctor_module.check_python_version()
    assert py_check.status == "ok"
    assert "Python" in py_check.detail


@pytest.mark.parametrize(
    ("backend", "status", "detail"),
    [
        ("soxr", "ok", "soxr high-quality backend"),
        ("scipy", "ok", "scipy high-quality backend"),
        ("linear", "skip", "filtered linear fallback"),
    ],
)
def test_doctor_reports_resampling_backend(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    status: str,
    detail: str,
) -> None:
    monkeypatch.setattr(doctor_module, "resample_backend", lambda: backend)

    result = doctor_module.check_resampling_backend()

    assert result.name == "audio_resampling"
    assert result.status == status
    assert detail in result.detail


def test_doctor_version_detail_is_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor_module.importlib.metadata, "version", lambda package: "0.1.0")

    def fake_import_module(module: str) -> object:
        if module == "agents":
            return object()
        raise ImportError(module)

    monkeypatch.setattr(doctor_module.importlib, "import_module", fake_import_module)

    result = doctor_module.check_easycat_version()

    assert result.status == "ok"
    assert result.detail == "easycat 0.1.0 (extras: openai-agents)"
    assert "[dim]" not in result.detail


def test_doctor_report_renders_detail_and_fix_text_literally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream, console = _plain_console()
    monkeypatch.setattr(doctor_module, "stderr_console", console)

    doctor_module._render_report(
        [
            doctor_module.CheckResult(
                name="journal[dev]",
                status="fail",
                detail="/tmp/easycat[dev] is not writable",
                code="EASYCAT_E207",
                fix="mkdir -p '/tmp/easycat[dev]'",
            )
        ],
        profile="dev",
    )

    text = stream.getvalue()
    assert "journal[dev]" in text
    assert "/tmp/easycat[dev] is not writable" in text
    assert "mkdir -p '/tmp/easycat[dev]'" in text


def test_doctor_python_version_failure_includes_repo_setup_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    version_info = namedtuple(
        "version_info", ["major", "minor", "micro", "releaselevel", "serial"]
    )
    monkeypatch.setattr(doctor_module.sys, "version_info", version_info(3, 10, 13, "final", 0))

    result = doctor_module.check_python_version()

    assert result.status == "fail"
    assert result.code == "EASYCAT_E201"
    assert "uv python install 3.12" in result.fix
    assert "uv sync --python 3.12 --group dev" in result.fix
    assert "uv sync --python 3.12`" not in result.fix


# ── Checks 6–8 (microphone / journal writable / disk space) ──────────


def test_check_microphone_skips_when_sounddevice_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skip — not fail — when ``sounddevice`` isn't installed."""
    import sys

    monkeypatch.setitem(sys.modules, "sounddevice", None)
    result = doctor_module.check_microphone()
    assert result.status == "skip"
    assert "sounddevice" in result.detail


def test_check_microphone_fails_with_portaudio_fix_when_native_library_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_portaudio_error(_name: str) -> None:
        raise OSError("PortAudio library not found")

    monkeypatch.setattr(doctor_module.importlib, "import_module", raise_portaudio_error)

    result = doctor_module.check_microphone()

    assert result.status == "fail"
    assert result.code == "EASYCAT_E209"
    assert "PortAudio library not found" in result.detail
    assert "sudo apt-get install libportaudio2" in result.fix
    assert "brew install portaudio" in result.fix


def test_check_journal_writable_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path, empty_env: None
) -> None:
    """Pointing EASYCAT_DATA_DIR at a writable tmp dir yields ok."""
    monkeypatch.setenv("EASYCAT_DATA_DIR", str(tmp_path))
    (tmp_path / "journals").mkdir()
    result = doctor_module.check_journal_writable()
    assert result.status == "ok", result.detail
    assert str(tmp_path) in result.detail


def test_check_journal_writable_does_not_touch_existing_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, empty_env: None
) -> None:
    data_dir = tmp_path / "data"
    journal_dir = data_dir / "journals"
    journal_dir.mkdir(parents=True)
    sentinel = journal_dir / "existing.jsonl"
    sentinel.write_text("preserve me\n", encoding="utf-8")
    monkeypatch.setenv("EASYCAT_DATA_DIR", str(data_dir))
    before_names = sorted(path.name for path in journal_dir.iterdir())
    before_mtime = journal_dir.stat().st_mtime_ns

    result = doctor_module.check_journal_writable()

    assert result.status == "ok", result.detail
    assert sorted(path.name for path in journal_dir.iterdir()) == before_names
    assert journal_dir.stat().st_mtime_ns == before_mtime
    assert sentinel.read_text(encoding="utf-8") == "preserve me\n"


def test_check_journal_writable_does_not_follow_predictable_probe_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, empty_env: None
) -> None:
    """A malicious CWD probe symlink must not clobber its target."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EASYCAT_DATA_DIR", raising=False)
    journal_dir = tmp_path / ".easycat" / "journals"
    journal_dir.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me\n", encoding="utf-8")
    probe = journal_dir / ".doctor-write-probe"
    probe.symlink_to(victim)

    result = doctor_module.check_journal_writable()

    assert result.status == "ok", result.detail
    assert victim.read_text(encoding="utf-8") == "keep me\n"
    assert probe.is_symlink()


def test_journal_check_is_read_only_and_matches_runtime_data_dir_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path, empty_env: None
) -> None:
    """Doctor must probe the same journal root that SqliteJournal uses."""
    data_dir = tmp_path / "runtime-data"
    xdg_cache = tmp_path / "xdg-cache"
    monkeypatch.setenv("EASYCAT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_cache))

    result = doctor_module.check_journal_writable()

    assert result.status == "skip", result.detail
    assert result.code == "EASYCAT_E207"
    assert str(data_dir / "journals") in result.detail
    assert not (data_dir / "journals").exists()
    assert not (xdg_cache / "easycat" / "journals").exists()


def test_check_journal_writable_fails_on_readonly(
    monkeypatch: pytest.MonkeyPatch, tmp_path, empty_env: None
) -> None:
    """If the journal dir cannot be created safely, surface E207 read-only."""
    # Point at a path that collides with a regular file. Doctor must not replace it.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setenv("EASYCAT_DATA_DIR", str(blocker))
    result = doctor_module.check_journal_writable()
    assert result.status == "fail"
    assert result.code == "EASYCAT_E207"
    assert blocker.read_text(encoding="utf-8") == "not a dir"


def test_check_journal_writable_does_not_follow_fixed_probe_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path, empty_env: None
) -> None:
    """A pre-existing fixed-name probe symlink must not be overwritten."""
    data_dir = tmp_path / "data"
    journal_dir = data_dir / "journals"
    journal_dir.mkdir(parents=True)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"keep me")
    fixed_probe = journal_dir / ".doctor-write-probe"
    try:
        fixed_probe.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    monkeypatch.setenv("EASYCAT_DATA_DIR", str(data_dir))

    result = doctor_module.check_journal_writable()

    assert result.status == "ok", result.detail
    assert victim.read_bytes() == b"keep me"
    assert fixed_probe.is_symlink()


def test_check_disk_space_reports_free_megabytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path, empty_env: None
) -> None:
    monkeypatch.setenv("EASYCAT_DATA_DIR", str(tmp_path))
    result = doctor_module.check_disk_space()
    assert result.status in {"ok", "fail"}
    assert "MB free" in result.detail


def test_check_disk_space_fails_under_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path, empty_env: None
) -> None:
    """Force the threshold higher than any realistic free space."""
    monkeypatch.setenv("EASYCAT_DATA_DIR", str(tmp_path))
    result = doctor_module.check_disk_space(min_free_mb=10**12)
    assert result.status == "fail"
    assert result.code == "EASYCAT_E208"
    assert "EASYCAT_DATA_DIR" in result.fix


def test_journal_error_guidance_matches_runtime_data_dir_contract() -> None:
    text = (
        f"{REGISTRY['EASYCAT_E207'].cause}\n"
        f"{REGISTRY['EASYCAT_E207'].fix}\n"
        f"{REGISTRY['EASYCAT_E208'].fix}"
    )

    assert ".easycat/journals" in text
    assert "EASYCAT_DATA_DIR" in text
    assert "~/.cache/easycat" not in text
    assert "XDG_CACHE_HOME" not in text


def test_doctor_fix_creates_journal_dir(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    no_network: None,
    empty_env: None,
) -> None:
    """``--fix`` mkdirs the journal directory when E207 is reported."""
    # Point EASYCAT_DATA_DIR at a non-existent nested path. Default doctor only
    # reports it; --fix owns the creation.
    data_dir = tmp_path / "never-created"
    monkeypatch.setenv("EASYCAT_DATA_DIR", str(data_dir))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    _stub_sufficient_disk_space(monkeypatch)

    # Pre-condition: the dir doesn't exist yet.
    journal_dir = data_dir / "journals"
    assert not journal_dir.exists()

    # Running doctor once with --fix should create it.
    result = cli.invoke(app, ["doctor", "--fix"])
    assert journal_dir.exists(), "journal dir should have been auto-created by --fix"
    # Exit code should be 0 after remediation, since all other checks pass.
    assert result.exit_code == 0, result.stderr


def test_doctor_fix_json_reports_each_mutation_and_is_idempotent(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    no_network: None,
    empty_env: None,
) -> None:
    data_dir = tmp_path / "data"
    journal_dir = data_dir / "journals"
    monkeypatch.setenv("EASYCAT_DATA_DIR", str(data_dir))
    _stub_sufficient_disk_space(monkeypatch)

    first = cli.invoke(app, ["doctor", "--fix", "--json"])

    assert first.exit_code == 0, first.stderr
    first_payload = json.loads(first.stdout)
    assert first_payload["fixes"] == [
        {
            "action": "create_directory",
            "target": str(journal_dir),
            "status": "applied",
            "detail": "created journal directory",
        }
    ]
    assert journal_dir.is_dir()

    second = cli.invoke(app, ["doctor", "--fix", "--json"])

    assert second.exit_code == 0, second.stderr
    assert json.loads(second.stdout)["fixes"] == []


def test_doctor_failed_fix_sets_error_status_and_nonzero_exit(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    checks = [
        doctor_module.CheckResult(
            name="journal_writable",
            status="skip",
            detail="journal directory does not exist",
            code="EASYCAT_E207",
        )
    ]
    failed_fix = doctor_module.FixResult(
        action="create_directory",
        target=str(tmp_path / "journals"),
        status="failed",
        detail="permission denied",
    )
    monkeypatch.setattr(doctor_module, "_run_all_checks", lambda **_kwargs: checks)
    monkeypatch.setattr(doctor_module, "_apply_safe_fixes", lambda _results: [failed_fix])

    terminal_result = cli.invoke(app, ["doctor", "--fix"])

    assert terminal_result.exit_code == 1
    assert "--fix failed" in terminal_result.stderr
    assert "1 fix failed" in terminal_result.stderr

    json_result = cli.invoke(app, ["doctor", "--fix", "--json"])

    assert json_result.exit_code == 1
    payload = json.loads(json_result.stdout)
    assert payload["status"] == "error"
    assert payload["fixes"] == [failed_fix.as_dict()]


# ══════════════════════════════════════════════════════════════════════
# DX2 — doctor diagnoses the selected manifest profile
# ══════════════════════════════════════════════════════════════════════

_SECRET_SHAPED = "sk-live-secret-token-abcdef1234567890"


def _write_manifest(
    tmp_path: Path,
    profile_body: str,
    *,
    server: str = "",
    name: str = "dx2-app",
) -> Path:
    """Write an ``easycat.toml`` with ``[project]``/``[voice.default]``."""
    sections = [f'[project]\nname = "{name}"']
    if server:
        sections.append(f"[server]\n{server.strip()}")
    sections.append(f"[voice.default]\n{profile_body.strip()}")
    manifest = tmp_path / "easycat.toml"
    manifest.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
    return manifest


def _pin_extras(monkeypatch: pytest.MonkeyPatch, *absent: str) -> None:
    """Pin planner module availability: everything present except *absent*.

    Mirrors ``tests/planning/test_parity.py``'s ``_force_find_spec_none`` but
    patches the single private seam every extra check flows through
    (``provider_plan._module_available``), so no doctor test depends on which
    extras happen to be installed in the running environment.
    """
    missing = set(absent)
    monkeypatch.setattr(provider_plan, "_module_available", lambda module: module not in missing)


def _checks(payload: dict[str, object]) -> dict[str, dict[str, object]]:
    rows = payload["checks"]
    assert isinstance(rows, list)
    return {row["name"]: row for row in rows}


def _assert_rows(checks: dict[str, dict], expected: dict) -> None:
    for name, fields in expected.get("rows", {}).items():
        assert name in checks, sorted(checks)
        for key, value in fields.items():
            assert checks[name].get(key) == value, (name, key, checks[name])
    for name in expected.get("absent_rows", ()):
        assert name not in checks, sorted(checks)
    for name, fragment in expected.get("detail_contains", {}).items():
        assert fragment in checks[name]["detail"]
    for name, fragment in expected.get("fix_contains", {}).items():
        assert fragment in checks[name]["fix"]


def _assert_selected_app_case(result, expected: dict, probed: list[str]) -> None:
    payload = json.loads(result.stdout)
    if "exit_code" in expected:
        assert result.exit_code == expected["exit_code"], result.stdout
    _assert_rows(_checks(payload), expected)
    for code in expected.get("forbidden_codes", ()):
        assert code not in result.stdout
    for field_name in expected.get("forbidden_fields", ()):
        assert not [row for row in payload["checks"] if row.get("field") == field_name]
    for text in expected.get("forbidden_text", ()):
        assert text not in result.stdout
        assert text not in result.stderr
    if "probed" in expected:
        expected_urls = [doctor_module._PROVIDER_PROBE_URL[name] for name in expected["probed"]]
        assert probed == expected_urls


_SELECTED_APP_CASES: tuple[tuple[str, str, str, dict[str, str], tuple[str, ...], dict], ...] = (
    (
        "missing-credential",
        'transport = "webrtc"\nstt = "deepgram"\ntts = "openai"',
        "",
        {"OPENAI_API_KEY": "sk-stub"},
        (),
        {
            "exit_code": 1,
            "rows": {
                "env_deepgram": {
                    "status": "fail",
                    "code": "EASYCAT_E203",
                    "role": "stt",
                    "field": "DEEPGRAM_API_KEY",
                    "requirement": "required",
                },
                "env_openai": {"status": "ok", "role": "tts"},
            },
        },
    ),
    (
        "credential-present",
        'transport = "webrtc"\nstt = "deepgram"\ntts = "openai"',
        "",
        {"OPENAI_API_KEY": "sk-stub", "DEEPGRAM_API_KEY": "dg-stub"},
        (),
        {
            "exit_code": 0,
            "rows": {
                "env_deepgram": {"status": "ok", "requirement": "required", "role": "stt"},
            },
        },
    ),
    (
        "unused-provider",
        'transport = "webrtc"\nstt = "openai"\ntts = "openai"',
        "",
        {"OPENAI_API_KEY": "sk-stub", "ELEVENLABS_API_KEY": "el-stub"},
        (),
        {
            "exit_code": 0,
            "rows": {"env_elevenlabs": {"status": "skip", "requirement": "unused"}},
            "probed": ["openai"],
        },
    ),
    (
        "browser-no-mic",
        'transport = "webrtc"',
        "",
        {"OPENAI_API_KEY": "sk-stub"},
        (),
        {
            "exit_code": 0,
            "absent_rows": ["microphone"],
            "forbidden_codes": ["EASYCAT_E206", "EASYCAT_E209"],
        },
    ),
    (
        "local-needs-mic",
        'transport = "local"',
        "",
        {"OPENAI_API_KEY": "sk-stub"},
        (),
        {"rows": {"microphone": {"probe": "hardware"}}},
    ),
    (
        "missing-extra",
        'transport = "webrtc"',
        "",
        {"OPENAI_API_KEY": "sk-stub"},
        ("aiortc",),
        {
            "exit_code": 1,
            "rows": {
                "extra_webrtc": {
                    "status": "fail",
                    "code": "EASYCAT_E202",
                    "field": "webrtc",
                    "role": "transport",
                    "requirement": "required",
                }
            },
            "fix_contains": {"extra_webrtc": "easycat[webrtc]"},
        },
    ),
    (
        "degraded-extra",
        'transport = "webrtc"',
        "",
        {"OPENAI_API_KEY": "sk-stub"},
        ("livekit",),
        {
            "exit_code": 0,
            "rows": {
                "extra_aec": {
                    "status": "skip",
                    "requirement": "optional",
                    "role": "echo_canceller",
                }
            },
            "detail_contains": {"extra_aec": "passthrough"},
        },
    ),
    (
        "phone-without-token",
        'transport = "twilio"',
        "",
        {"OPENAI_API_KEY": "sk-stub"},
        (),
        {
            "exit_code": 1,
            "rows": {
                "selection_incomplete_selection": {
                    "status": "fail",
                    "code": "EASYCAT_E602",
                    "field": "[voice.default]",
                }
            },
            "detail_contains": {
                "selection_incomplete_selection": (
                    "token = 'bearer-env:TWILIO_STREAM_TOKEN_SECRET'"
                )
            },
        },
    ),
    (
        "phone-token-unset",
        'transport = "twilio"\ntoken = "bearer-env:TW_TOK"',
        "",
        {"OPENAI_API_KEY": "sk-stub"},
        (),
        {
            "exit_code": 1,
            "rows": {
                "env_tw_tok": {
                    "status": "fail",
                    "code": "EASYCAT_E604",
                    "field": "TW_TOK",
                    "requirement": "required",
                }
            },
        },
    ),
    (
        "phone-token-placeholder",
        'transport = "twilio"\ntoken = "bearer-env:TW_TOK"',
        "",
        {"OPENAI_API_KEY": "sk-stub", "TW_TOK": "changeme"},
        (),
        {
            "exit_code": 1,
            "rows": {
                "env_tw_tok": {
                    "status": "fail",
                    # E604 is what STARTUP raises for an UNSET reference var;
                    # ``changeme`` is set, so ``EnvReference.resolve`` accepts
                    # it and doctor must not name a code startup cannot produce.
                    "code": "EASYCAT_E210",
                    "field": "TW_TOK",
                    "requirement": "required",
                }
            },
            "detail_contains": {"env_tw_tok": "looks like a placeholder"},
            "forbidden_codes": ["EASYCAT_E604"],
        },
    ),
    (
        "phone-token-set",
        'transport = "twilio"\ntoken = "bearer-env:TW_TOK"',
        "",
        {"OPENAI_API_KEY": "sk-stub", "TW_TOK": _SECRET_SHAPED},
        (),
        {
            "exit_code": 0,
            "rows": {"env_tw_tok": {"status": "ok", "field": "TW_TOK"}},
            "forbidden_text": [_SECRET_SHAPED],
        },
    ),
    (
        "server-auth-unset",
        'transport = "webrtc"',
        'auth = "bearer-env:SRV_TOK"',
        {"OPENAI_API_KEY": "sk-stub"},
        (),
        {
            "exit_code": 1,
            "rows": {
                "env_srv_tok": {
                    "status": "fail",
                    "code": "EASYCAT_E604",
                    "field": "SRV_TOK",
                    "requirement": "required",
                }
            },
        },
    ),
    (
        "unused-extra",
        'transport = "webrtc"\nstt = "openai"\ntts = "openai"',
        "",
        {"OPENAI_API_KEY": "sk-stub"},
        ("sounddevice", "aiortc"),
        {
            "exit_code": 1,
            "rows": {"extra_webrtc": {"status": "fail", "code": "EASYCAT_E202"}},
            "absent_rows": ["extra_local"],
            "forbidden_fields": ["local"],
        },
    ),
)


@pytest.mark.parametrize(
    ("case_id", "profile_body", "server", "env", "absent", "expected"),
    _SELECTED_APP_CASES,
    ids=[case[0] for case in _SELECTED_APP_CASES],
)
def test_doctor_selected_app(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    case_id: str,
    profile_body: str,
    server: str,
    env: dict[str, str],
    absent: tuple[str, ...],
    expected: dict,
) -> None:
    monkeypatch.chdir(tmp_path)
    _stub_sufficient_disk_space(monkeypatch)
    _pin_extras(monkeypatch, *absent)
    probed: list[str] = []

    def fake_head(url, *, timeout=0.0, follow_redirects=False, **kw):
        probed.append(url)

        class _R:
            status_code = 200

        return _R()

    monkeypatch.setattr("httpx.head", fake_head)
    for var in ("TW_TOK", "SRV_TOK"):
        monkeypatch.delenv(var, raising=False)
    for var, value in env.items():
        monkeypatch.setenv(var, value)
    manifest = _write_manifest(tmp_path, profile_body, server=server)

    result = cli.invoke(app, ["doctor", "--manifest", str(manifest), "--json"])

    _assert_selected_app_case(result, expected, probed)


# ── Behavior preservation and boundaries ──────────────────────────────


def test_doctor_without_manifest_flags_ignores_an_ambient_easycat_toml(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    """D-1: manifest mode is strictly opt-in — a stray easycat.toml is inert."""
    monkeypatch.chdir(tmp_path)
    _stub_sufficient_disk_space(monkeypatch)
    _write_manifest(tmp_path, 'transport = "webrtc"\nstt = "deepgram"')
    (tmp_path / "pyproject.toml").write_text(
        '[tool.easycat.scaffold]\ntemplate = "twilio-phone"\nrequired_env = ["OPENAI_API_KEY"]\n',
        encoding="utf-8",
    )

    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("bare doctor must not load a manifest")

    monkeypatch.setattr("easycat.project.load_manifest", explode)

    result = cli.invoke(app, ["doctor", "--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 1
    assert payload["selection"]["source"] == "scaffold"
    assert payload["selection"]["profile"] is None
    assert payload["selection"]["roles"] == []


def test_doctor_without_manifest_does_not_import_the_planner(tmp_path: Path) -> None:
    """D-2: the no-heavy-import-regression proof, in a fresh interpreter."""
    code = (
        "import sys;"
        "from typer.testing import CliRunner;"
        "from easycat.cli._app import app, _register_commands;"
        "_register_commands();"
        "r = CliRunner().invoke(app, ['doctor', '--json']);"
        "print('easycat.planning' in sys.modules, 'easycat.project' in sys.modules)"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.endswith("_API_KEY") and key != "EASYCAT_MANIFEST"
    }
    env["EASYCAT_DATA_DIR"] = str(tmp_path / "data")
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        env=env,
    )

    assert result.stdout.strip() == "False False", result.stdout


def test_doctor_manifest_mode_never_imports_the_application(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    """D-3: resolving a profile must not import and execute its application."""
    monkeypatch.chdir(tmp_path)
    _stub_sufficient_disk_space(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    sentinel = tmp_path / "imported.marker"
    (tmp_path / "dx2_probe_app.py").write_text(
        f"from pathlib import Path\n"
        f"Path({str(sentinel)!r}).write_text('imported')\n"
        f"raise RuntimeError('the application must never be imported by doctor')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    manifest = _write_manifest(
        tmp_path, 'transport = "webrtc"\nagent = "python:dx2_probe_app:build"'
    )

    result = cli.invoke(app, ["doctor", "--manifest", str(manifest), "--json"])

    assert result.exit_code in {0, 1}, result.stdout
    assert not sentinel.exists()
    assert "dx2_probe_app" not in sys.modules


def test_doctor_unknown_profile_matches_plan_error(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, empty_env: None
) -> None:
    """D-4: one manifest typo, one coded answer across both commands."""
    monkeypatch.chdir(tmp_path)
    manifest = _write_manifest(tmp_path, 'transport = "webrtc"')

    doctor_result = cli.invoke(
        app, ["doctor", "--manifest", str(manifest), "--profile", "nope", "--json"]
    )
    plan_result = cli.invoke(
        app, ["plan", "--manifest", str(manifest), "--profile", "nope", "--json"]
    )

    doctor_payload = json.loads(doctor_result.stdout)
    plan_payload = json.loads(plan_result.stdout)
    assert doctor_payload["code"] == plan_payload["code"] == "EASYCAT_E602"
    assert doctor_result.exit_code == plan_result.exit_code
    assert "available: default" in doctor_payload["message"]
    assert "available: default" in plan_payload["message"]


def test_doctor_missing_manifest_reports_e601(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, empty_env: None
) -> None:
    """D-5."""
    monkeypatch.chdir(tmp_path)

    result = cli.invoke(app, ["doctor", "--manifest", str(tmp_path / "nope.toml"), "--json"])

    assert result.exit_code != 0
    assert json.loads(result.stdout)["code"] == "EASYCAT_E601"


def test_doctor_unknown_vad_matches_plan_error(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, empty_env: None
) -> None:
    """D-6."""
    monkeypatch.chdir(tmp_path)
    manifest = _write_manifest(tmp_path, 'transport = "webrtc"\nvad = "silro"')

    doctor_payload = json.loads(
        cli.invoke(app, ["doctor", "--manifest", str(manifest), "--json"]).stdout
    )
    plan_payload = json.loads(
        cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"]).stdout
    )

    assert doctor_payload["code"] == plan_payload["code"] == "EASYCAT_E602"
    assert doctor_payload["context"]["path"] == "[voice.default]"
    assert plan_payload["context"]["path"] == "[voice.default]"


def test_doctor_unknown_provider_reports_e104(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, empty_env: None
) -> None:
    """D-7."""
    monkeypatch.chdir(tmp_path)
    manifest = _write_manifest(tmp_path, 'transport = "webrtc"\nstt = "opnai"')

    doctor_result = cli.invoke(app, ["doctor", "--manifest", str(manifest), "--json"])
    plan_result = cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"])

    assert doctor_result.exit_code == 2
    assert json.loads(doctor_result.stdout)["code"] == "EASYCAT_E104"
    assert json.loads(plan_result.stdout)["code"] == "EASYCAT_E104"
    assert "Traceback" not in doctor_result.stdout


def test_doctor_provider_scope_conflicts_with_manifest_selection(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, empty_env: None
) -> None:
    """D-8: two incompatible scoping mechanisms fail loudly."""
    monkeypatch.chdir(tmp_path)
    manifest = _write_manifest(tmp_path, 'transport = "webrtc"')

    result = cli.invoke(
        app, ["doctor", "--provider", "openai", "--manifest", str(manifest), "--json"]
    )

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert "--provider" in payload["message"]
    assert "--manifest/--profile" in payload["message"]


def test_doctor_merges_scaffold_and_profile_requirements(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    """D-9: a scaffold's non-provider settings survive manifest selection."""
    monkeypatch.chdir(tmp_path)
    _stub_sufficient_disk_space(monkeypatch)
    _pin_extras(monkeypatch)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.easycat.scaffold]\ntemplate = "twilio-phone"\nrequired_env = ["TWILIO_WS_PORT"]\n',
        encoding="utf-8",
    )
    manifest = _write_manifest(
        tmp_path, 'transport = "twilio"\nstt = "deepgram"\ntoken = "bearer-env:TW_TOK"'
    )
    monkeypatch.setenv("TW_TOK", "tok")

    result = cli.invoke(app, ["doctor", "--manifest", str(manifest), "--json"])

    payload = json.loads(result.stdout)
    checks = _checks(payload)
    selection = payload["selection"]
    assert selection["source"] == "scaffold+manifest"
    assert checks["env_twilio_ws_port"]["code"] == "EASYCAT_E210"
    assert checks["env_deepgram"]["code"] == "EASYCAT_E203"
    assert checks["env_deepgram"]["role"] == "stt"

    # The ``selection`` object is a wire contract of its own, not just a
    # ``source`` tag: pin every field DX2 §1.3.7 specifies.
    assert selection["profile"] == "default"
    assert selection["manifest_path"] == str(manifest)
    assert selection["template"] == "twilio-phone"
    assert selection["required_env"] == [
        "DEEPGRAM_API_KEY",
        "OPENAI_API_KEY",
        "TW_TOK",
        "TWILIO_WS_PORT",
    ]
    assert selection["optional_env"] == []
    roles = {role["role"]: role for role in selection["roles"]}
    assert roles["stt"] == {
        "role": "stt",
        "provider": "deepgram",
        "required_env": "DEEPGRAM_API_KEY",
        "extra": "deepgram",
        "capabilities": [],
    }
    assert roles["transport"] == {
        "role": "transport",
        "provider": "twilio",
        "required_env": None,
        "extra": "telephony",
        "capabilities": ["8khz", "mulaw", "telephony"],
    }


def test_doctor_env_file_carries_manifest_reference_vars(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    """D-10: the allow-list widens, and the planner snapshot sees the file."""
    monkeypatch.chdir(tmp_path)
    _stub_sufficient_disk_space(monkeypatch)
    _pin_extras(monkeypatch)
    monkeypatch.delenv("SRV_TOK", raising=False)
    manifest = _write_manifest(
        tmp_path, 'transport = "webrtc"', server='auth = "bearer-env:SRV_TOK"'
    )
    env_file = tmp_path / ".env"
    env_file.write_text("SRV_TOK=from-file\nOPENAI_API_KEY=sk-from-file\n", encoding="utf-8")
    # Nothing doctor PRINTS today depends on which environment snapshot the
    # planner got (``check_env_vars`` reads the live environment inside
    # ``_temporary_env``), so record the kwarg directly: this is the only
    # assertion that fails if the snapshot moves back outside ``_temporary_env``.
    recorded: dict[str, str] = {}
    real_derivation = requirements_module.selected_app_from_manifest

    def _recording_derivation(*args: object, environ: dict[str, str], **kwargs: object):
        recorded.update(environ)
        return real_derivation(*args, environ=environ, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(requirements_module, "selected_app_from_manifest", _recording_derivation)

    result = cli.invoke(
        app,
        ["doctor", "--manifest", str(manifest), "--env-file", str(env_file), "--json"],
    )

    checks = _checks(json.loads(result.stdout))
    assert checks["env_srv_tok"]["status"] == "ok"
    assert checks["env_openai"]["status"] == "ok"
    assert result.exit_code == 0, result.stdout
    assert recorded["SRV_TOK"] == "from-file"
    assert recorded["OPENAI_API_KEY"] == "sk-from-file"


def test_doctor_profile_flag_alone_discovers_the_manifest(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    """``--profile`` without ``--manifest`` uses the standard discovery order."""
    monkeypatch.chdir(tmp_path)
    _stub_sufficient_disk_space(monkeypatch)
    _pin_extras(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    _write_manifest(tmp_path, 'transport = "webrtc"')

    result = cli.invoke(app, ["doctor", "--profile", "default", "--json"])

    payload = json.loads(result.stdout)
    assert payload["selection"]["source"] == "manifest"
    assert payload["selection"]["profile"] == "default"
    assert payload["selection"]["manifest_path"] == str(tmp_path / "easycat.toml")


def test_doctor_profile_flag_alone_reports_e601_when_nothing_is_found(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, empty_env: None
) -> None:
    """The other half of the activation rule: discovery failure is still coded."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EASYCAT_MANIFEST", raising=False)

    result = cli.invoke(app, ["doctor", "--profile", "default", "--json"])

    assert result.exit_code != 0
    assert json.loads(result.stdout)["code"] == "EASYCAT_E601"


_CLI_DOC = Path(__file__).resolve().parents[2] / "docs" / "cli.md"


def _documented_walkthrough_manifest() -> str:
    """The ``easycat.toml`` heredoc from docs/cli.md's first-run walkthrough."""
    text = _CLI_DOC.read_text(encoding="utf-8")
    start = text.index("cat > easycat.toml <<'TOML'")
    body = text[text.index("\n", start) + 1 :]
    return body[: body.index("\nTOML\n") + 1]


def test_documented_first_run_walkthrough_manifest_is_diagnosable(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    """docs/cli.md's walkthrough must actually reach the codes it advertises.

    ``easycat init`` scaffolds an application, not a manifest, so the doc has to
    write one; if that block is dropped or malformed the sequence aborts with
    ``EASYCAT_E601`` instead of the E203 -> E202 -> green path it promises.
    """
    monkeypatch.chdir(tmp_path)
    _stub_sufficient_disk_space(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    manifest = tmp_path / "easycat.toml"
    manifest.write_text(_documented_walkthrough_manifest(), encoding="utf-8")

    _pin_extras(monkeypatch, "aiortc")
    step_3 = json.loads(cli.invoke(app, ["doctor", "--manifest", "easycat.toml", "--json"]).stdout)
    assert step_3["checks"], step_3
    assert {row["code"] for row in step_3["checks"] if row["status"] == "fail"} == {
        "EASYCAT_E203",
        "EASYCAT_E202",
    }

    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    step_5 = json.loads(cli.invoke(app, ["doctor", "--manifest", "easycat.toml", "--json"]).stdout)
    step_5_codes = {row["code"] for row in step_5["checks"] if row["status"] == "fail"}
    assert step_5_codes == {"EASYCAT_E202"}

    _pin_extras(monkeypatch)
    step_7 = cli.invoke(app, ["doctor", "--manifest", "easycat.toml", "--json"])
    assert step_7.exit_code == 0, step_7.stdout


@pytest.mark.parametrize("run_from", ["manifest-dir", "elsewhere"])
def test_doctor_extra_fix_respects_the_manifest_project_dependency_pin(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
    run_from: str,
) -> None:
    """The install fix classifies the MANIFEST's project, not the process CWD."""
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "app"\ndependencies = ["easycat[openai]"]\n\n'
        '[tool.uv.sources]\neasycat = { git = "https://x.invalid/e.git", rev = "abc" }\n',
        encoding="utf-8",
    )
    manifest = _write_manifest(project, 'transport = "webrtc"')
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(project if run_from == "manifest-dir" else elsewhere)
    _stub_sufficient_disk_space(monkeypatch)
    _pin_extras(monkeypatch, "aiortc")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")

    result = cli.invoke(app, ["doctor", "--manifest", str(manifest), "--json"])

    row = _checks(json.loads(result.stdout))["extra_webrtc"]
    assert row["code"] == "EASYCAT_E202"
    assert "pyproject.toml" in row["fix"]
    assert "uv sync" in row["fix"]
    assert "uv add" not in row["fix"]


_EXTRA_ROW_APP = SelectedApp(
    source="manifest",
    roles=(
        RoleRequirement(
            role="transport",
            provider="webrtc",
            extra="webrtc",
            capabilities=frozenset({"browser"}),
        ),
    ),
)
_DEFECT_ROW_APP = SelectedApp(
    source="manifest",
    issues=(
        SetupIssue(
            reason="incomplete_selection",
            field="[voice.default]",
            code="EASYCAT_E602",
            detail="twilio needs a token",
            role="transport",
        ),
    ),
)


@pytest.mark.parametrize(
    ("factory", "expected"),
    [
        (lambda: doctor_module.check_python_version(), "static"),
        (lambda: doctor_module.check_easycat_version(), "import"),
        (lambda: doctor_module.check_onnxruntime(), "import"),
        (lambda: doctor_module.check_resampling_backend(), "import"),
        (lambda: doctor_module.check_microphone(), "hardware"),
        (lambda: doctor_module.check_journal_writable(), "filesystem"),
        (lambda: doctor_module.check_disk_space(), "filesystem"),
        (lambda: doctor_module.check_selected_extras(_EXTRA_ROW_APP)[0], "static"),
        (lambda: doctor_module.check_selection_defects(_DEFECT_ROW_APP)[0], "static"),
    ],
)
def test_doctor_rows_declare_their_probe_class(factory, expected: str) -> None:
    """D-11 (unit half): every row names how it learned what it reports."""
    assert factory().probe == expected


def test_doctor_env_and_network_rows_declare_their_probe_class(
    monkeypatch: pytest.MonkeyPatch, empty_env: None, no_network: None
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")

    assert doctor_module.check_env_vars("openai")[0].probe == "static"
    assert doctor_module.check_provider_reachability("openai")[0].probe == "network"


def test_doctor_json_rows_and_probe_summary_agree(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    """D-11 (envelope half)."""
    monkeypatch.chdir(tmp_path)
    _stub_sufficient_disk_space(monkeypatch)
    _pin_extras(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    manifest = _write_manifest(tmp_path, 'transport = "local"')

    payload = json.loads(cli.invoke(app, ["doctor", "--manifest", str(manifest), "--json"]).stdout)

    emitted = {row["probe"] for row in payload["checks"]}
    assert emitted <= set(doctor_module.PROBE_KINDS)
    assert set(payload["probes"]) == set(doctor_module.PROBE_KINDS)
    assert payload["probes"] == {kind: kind in emitted for kind in doctor_module.PROBE_KINDS}
    assert payload["probes"]["hardware"] is True
    assert payload["probes"]["network"] is True


def test_doctor_bare_run_still_reports_probe_classes(
    cli: CliRunner, empty_env: None, no_network: None
) -> None:
    payload = json.loads(cli.invoke(app, ["doctor", "--json"]).stdout)

    assert "selection" not in payload
    assert set(payload["probes"]) == set(doctor_module.PROBE_KINDS)
    assert payload["probes"]["network"] is False


def test_doctor_help_states_the_probe_boundary(cli: CliRunner) -> None:
    """D-12: the probe boundary and the new flags are visible in --help."""
    result = cli.invoke(app, ["doctor", "--help"])
    help_text = " ".join(result.stdout.split())

    assert result.exit_code == 0
    assert "--manifest" in help_text
    assert "--profile" in help_text
    assert "never imported or run" in help_text
    assert "bounded" in help_text
    assert "configured credentials" in help_text
    assert "network liveness" in help_text

    # A wide terminal so Rich does not ellipsize the Commands table before the
    # assertion can read the row.
    top = cli.invoke(app, ["--help"], env={"COLUMNS": "200"})
    top_text = " ".join(top.stdout.split())
    short_help = _COMMAND_TEXT["doctor"].short_help
    assert short_help is not None
    assert f"doctor {short_help}" in top_text
    assert "never imported or run" not in top_text


def test_doctor_network_probe_stays_bounded(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, empty_env: None
) -> None:
    """D-13: one bounded request per configured, in-scope provider."""
    monkeypatch.chdir(tmp_path)
    _stub_sufficient_disk_space(monkeypatch)
    _pin_extras(monkeypatch)
    calls: list[dict[str, object]] = []

    def fake_head(url, **kwargs):
        calls.append({"url": url, **kwargs})

        class _R:
            status_code = 200

        return _R()

    monkeypatch.setattr("httpx.head", fake_head)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-stub")
    manifest = _write_manifest(tmp_path, 'transport = "webrtc"\nstt = "deepgram"')

    cli.invoke(app, ["doctor", "--manifest", str(manifest), "--json"])

    assert calls, "the reachability probe should have run"
    assert all(call["timeout"] == 2.0 for call in calls)
    assert len(calls) <= 2


def test_doctor_manifest_output_contains_no_secret_values(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    """D-14: names and presence only, in human and JSON mode."""
    monkeypatch.chdir(tmp_path)
    _stub_sufficient_disk_space(monkeypatch)
    _pin_extras(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("DEEPGRAM_API_KEY", _SECRET_SHAPED)
    monkeypatch.setenv("TW_TOK", _SECRET_SHAPED)
    manifest = _write_manifest(
        tmp_path, 'transport = "twilio"\nstt = "deepgram"\ntoken = "bearer-env:TW_TOK"'
    )

    for argv in (
        ["doctor", "--manifest", str(manifest)],
        ["doctor", "--manifest", str(manifest), "--json"],
    ):
        result = cli.invoke(app, argv)
        assert _SECRET_SHAPED not in result.stdout
        assert _SECRET_SHAPED not in result.stderr


def test_doctor_manifest_human_report_names_role_and_fix(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    """D-15."""
    monkeypatch.chdir(tmp_path)
    _stub_sufficient_disk_space(monkeypatch)
    _pin_extras(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    manifest = _write_manifest(tmp_path, 'transport = "webrtc"\nstt = "deepgram"')

    result = cli.invoke(app, ["doctor", "--manifest", str(manifest)])
    report = " ".join(result.stderr.split())

    assert "env_deepgram (stt)" in report
    assert "EASYCAT_E203" in report
    assert "Fix:" in report
    assert "easycat explain E203" in report
    assert "[voice.default]" in report


def test_doctor_manifest_mode_builds_one_plan(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    """D-16: --fix's re-run must reuse the SelectedApp, never re-derive it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EASYCAT_DATA_DIR", str(tmp_path / "data"))
    _stub_sufficient_disk_space(monkeypatch)
    _pin_extras(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    manifest = _write_manifest(tmp_path, 'transport = "webrtc"')
    calls: list[str] = []
    real = build_manifest_plan

    def counting(*args, **kwargs):
        calls.append(kwargs.get("profile", "default"))
        return real(*args, **kwargs)

    monkeypatch.setattr("easycat.planning.selection.build_manifest_plan", counting)

    cli.invoke(app, ["doctor", "--manifest", str(manifest), "--fix", "--json"])

    assert calls == ["default"]


def test_doctor_extra_rows_are_deduped_per_extra(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    """D-17: one row per DISTINCT extra, not one per role."""
    monkeypatch.chdir(tmp_path)
    _stub_sufficient_disk_space(monkeypatch)
    _pin_extras(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    manifest = _write_manifest(
        tmp_path, 'transport = "webrtc"\nstt = "openai-realtime"\ntts = "openai"'
    )

    payload = json.loads(cli.invoke(app, ["doctor", "--manifest", str(manifest), "--json"]).stdout)

    names = [row["name"] for row in payload["checks"]]
    assert names.count("extra_openai") == 1


def test_doctor_manifest_error_precedes_env_file_usage_error(
    cli: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, empty_env: None
) -> None:
    """D-18: pins the documented ordering change, and its narrow scope."""
    monkeypatch.chdir(tmp_path)
    bad_env = tmp_path / "bad.env"
    bad_env.write_text("OPENAI_API_KEY sk-stub\n", encoding="utf-8")
    bad_manifest = tmp_path / "easycat.toml"
    bad_manifest.write_text("this is not = valid = toml\n", encoding="utf-8")

    with_manifest = cli.invoke(
        app,
        ["doctor", "--manifest", str(bad_manifest), "--env-file", str(bad_env), "--json"],
    )
    assert with_manifest.exit_code == 1
    assert json.loads(with_manifest.stdout)["code"] == "EASYCAT_E602"

    (tmp_path / "easycat.toml").unlink()
    bare = cli.invoke(app, ["doctor", "--env-file", str(bad_env), "--json"])
    assert bare.exit_code == 2
    bare_payload = json.loads(bare.stdout)
    assert "Invalid --env-file" in bare_payload["message"]
    assert "code" not in bare_payload


def test_doctor_ignores_extras_no_selected_role_needs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """D-19 (pure half): the unused-extras acceptance case, without the CLI."""
    _pin_extras(monkeypatch, "sounddevice", "aiortc")
    manifest_path = _write_manifest(
        tmp_path, 'transport = "webrtc"\nstt = "openai"\ntts = "openai"'
    )
    manifest = load_manifest(manifest_path)
    selected = selected_app_from_manifest(
        manifest,
        manifest.profile("default"),
        "default",
        environ={"OPENAI_API_KEY": "sk-stub"},
    )

    rows = doctor_module.check_selected_extras(selected)

    assert {row.field for row in rows} <= {"webrtc", "openai", "aec", "silero-vad"}
    assert "local" not in {row.field for row in rows}
    webrtc_row = next(row for row in rows if row.field == "webrtc")
    assert webrtc_row.status == "fail"
    assert webrtc_row.code == "EASYCAT_E202"


def test_doctor_reports_one_row_per_unset_reference_var(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
) -> None:
    """D-20: ``check_selection_defects`` must not duplicate ``check_env_vars``."""
    monkeypatch.chdir(tmp_path)
    _stub_sufficient_disk_space(monkeypatch)
    _pin_extras(monkeypatch)
    for var in ("SRV_TOK", "TW_TOK"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    manifest = _write_manifest(
        tmp_path,
        'transport = "twilio"\ntoken = "bearer-env:TW_TOK"',
        server='auth = "bearer-env:SRV_TOK"',
    )

    payload = json.loads(cli.invoke(app, ["doctor", "--manifest", str(manifest), "--json"]).stdout)

    for var in ("SRV_TOK", "TW_TOK"):
        assert len([row for row in payload["checks"] if row.get("field") == var]) == 1


@pytest.mark.parametrize(
    ("case_id", "profile_body", "absent"),
    [
        # Missing credential + missing extra, no plan defect.
        ("gaps", 'transport = "webrtc"\nstt = "deepgram"', ("aiortc",)),
        # A real ``ProviderPlan.defect`` (phone transport with no token), so the
        # third assertion below compares against a NON-EMPTY set of codes.
        ("defect", 'transport = "twilio"', ()),
    ],
)
def test_doctor_requirement_rows_match_the_manifest_plan(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    empty_env: None,
    no_network: None,
    case_id: str,
    profile_body: str,
    absent: tuple[str, ...],
) -> None:
    """The drift anchor: doctor's failing rows equal the plan's blocking gaps."""
    monkeypatch.chdir(tmp_path)
    _stub_sufficient_disk_space(monkeypatch)
    _pin_extras(monkeypatch, *absent)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    manifest_path = _write_manifest(tmp_path, profile_body)

    payload = json.loads(
        cli.invoke(app, ["doctor", "--manifest", str(manifest_path), "--json"]).stdout
    )
    plan = build_manifest_plan(load_manifest(manifest_path), environ={"OPENAI_API_KEY": "sk-stub"})
    checks = payload["checks"]

    assert {row["field"] for row in checks if row.get("code") == "EASYCAT_E203"} == set(
        plan.missing_env
    )
    assert {
        row["field"]
        for row in checks
        if row.get("code") == "EASYCAT_E202" and row["status"] == "fail"
    } == set(plan.missing_extras)
    defect_codes = {issue.code for issue in plan.defects}
    assert (defect_codes != set()) is (case_id == "defect")
    assert {row["code"] for row in checks if row["status"] == "fail"} >= defect_codes


# ── Pure units ─────────────────────────────────────────────────────────


_REGISTRY_FIX = EASYCAT_E202(extra="webrtc").rendered_fix()

_DEPENDENCY_SOURCE_CASES = (
    ("none-no-pyproject", None, "none"),
    (
        "none-no-easycat-dependency",
        '[project]\nname = "app"\ndependencies = ["httpx"]\n',
        "none",
    ),
    (
        "pypi",
        '[project]\nname = "app"\ndependencies = ["easycat[openai]"]\n',
        "pypi",
    ),
    (
        "git",
        (
            '[project]\nname = "app"\ndependencies = ["easycat[openai]"]\n'
            '[tool.uv.sources]\neasycat = { git = "https://x.invalid/e.git", rev = "abc" }\n'
        ),
        "git",
    ),
    (
        "path",
        (
            '[project]\nname = "app"\ndependencies = ["easycat"]\n'
            '[tool.uv.sources]\neasycat = { path = "../easycat", editable = true }\n'
        ),
        "path",
    ),
)


@pytest.mark.parametrize(
    ("case_id", "pyproject", "expected"),
    _DEPENDENCY_SOURCE_CASES,
    ids=[case[0] for case in _DEPENDENCY_SOURCE_CASES],
)
def test_install_fix_respects_dependency_source(
    tmp_path: Path, case_id: str, pyproject: str | None, expected: str
) -> None:
    """U-1: only a PINNED source overrides the registry's E202 fix."""
    root = tmp_path / case_id
    root.mkdir()
    if pyproject is not None:
        (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")

    assert dependency_source(root) == expected
    fix = install_fix("webrtc", project_root=root)

    if expected in {"git", "path"}:
        assert "pyproject.toml" in fix
        assert "uv sync" in fix
        assert "uv add" not in fix
    else:
        assert fix == _REGISTRY_FIX
        assert "uv add 'easycat[webrtc]'" in fix
        assert "uv sync --extra webrtc --group dev" in fix


@pytest.mark.parametrize(
    ("transport", "expected"),
    [
        ("local", True),
        ("webrtc", False),
        ("websocket", False),
        ("twilio", False),
        ("telnyx", False),
    ],
)
def test_selected_app_requires_microphone_follows_transport_capability(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, transport: str, expected: bool
) -> None:
    """U-2."""
    _pin_extras(monkeypatch)
    body = f'transport = "{transport}"'
    if transport in {"twilio", "telnyx"}:
        body += '\ntoken = "bearer-env:TOK"'
    root = tmp_path / transport
    root.mkdir()
    manifest_path = _write_manifest(root, body)
    manifest = load_manifest(manifest_path)
    selected = selected_app_from_manifest(
        manifest, manifest.profile("default"), "default", environ={"TOK": "t"}
    )

    assert selected.requires_microphone is expected


def test_scaffold_only_selected_app_still_requires_a_microphone() -> None:
    """U-2 (compat half): today's behavior when no profile was selected."""
    assert SelectedApp(source="scaffold").requires_microphone is True


def test_selected_app_role_for_env_names_the_needing_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """U-3."""
    _pin_extras(monkeypatch)
    manifest_path = _write_manifest(
        tmp_path, 'transport = "webrtc"\nstt = "deepgram"\ntts = "openai"'
    )
    manifest = load_manifest(manifest_path)
    selected = selected_app_from_manifest(
        manifest, manifest.profile("default"), "default", environ={}
    )

    assert selected.role_for_env("DEEPGRAM_API_KEY") == "stt"
    assert selected.role_for_env("OPENAI_API_KEY") == "tts"
    assert selected.role_for_env("TWILIO_WS_PORT") == ""
    assert selected.code_for_env("DEEPGRAM_API_KEY") == "EASYCAT_E210"


def test_selected_app_codes_a_manifest_reference_var_as_e604() -> None:
    selected = SelectedApp(source="manifest", reference_vars=frozenset({"SRV_TOK"}))

    assert selected.code_for_env("SRV_TOK") == "EASYCAT_E604"
    assert selected.code_for_env("OTHER") == "EASYCAT_E210"


def test_doctor_selected_app_check_functions_are_pure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """U-4: neither new check reads the environment nor imports a module.

    The one filesystem touch is ``install_fix``'s ``pyproject.toml`` read, so it
    is stubbed here AND pinned: it must be handed the selected app's project
    root, not the process working directory.
    """
    manifest_path = tmp_path / "elsewhere" / "easycat.toml"
    selected = SelectedApp(
        source="manifest",
        manifest_path=str(manifest_path),
        roles=(
            RoleRequirement(
                role="transport",
                provider="webrtc",
                extra="webrtc",
                extra_missing=True,
                capabilities=frozenset({"browser"}),
            ),
        ),
    )
    roots: list[Path] = []
    monkeypatch.setattr(
        doctor_module,
        "install_fix",
        lambda extra, *, project_root: roots.append(project_root) or "stub fix",
    )
    before_env = dict(os.environ)
    before_modules = sorted(sys.modules)

    extras = doctor_module.check_selected_extras(selected)
    doctor_module.check_selection_defects(selected)

    assert dict(os.environ) == before_env
    assert sorted(sys.modules) == before_modules
    assert roots == [manifest_path.parent]
    assert extras[0].fix == "stub fix"


def test_selected_app_checks_are_empty_without_a_selection() -> None:
    assert doctor_module.check_selected_extras(None) == []
    assert doctor_module.check_selection_defects(None) == []
    assert doctor_module.check_selected_extras(SelectedApp(source="scaffold")) == []
