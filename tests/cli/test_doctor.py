"""``easycat doctor`` — environment checks and --json envelope."""

from __future__ import annotations

import json
import os
from collections import namedtuple
from collections.abc import Iterator
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.cli.diagnose import doctor as doctor_module
from easycat.errors import REGISTRY

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
    assert result.exit_code == 1
    assert "EASYCAT_E203" in result.stderr
    assert "OPENAI_API_KEY" in result.stderr
    assert "--env-file" in result.stderr
    assert ".env" in result.stderr
    assert "easycat doctor" in result.stderr


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
