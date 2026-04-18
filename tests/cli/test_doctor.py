"""``easycat doctor`` — checks 1-5 and --json envelope."""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.cli.diagnose import doctor as doctor_module


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stub ``httpx.head`` so tests don't hit real provider endpoints."""

    def fake_head(url, *, timeout=0.0, follow_redirects=False, **kw):  # noqa: ANN001
        class _R:
            status_code = 200

        return _R()

    monkeypatch.setattr("httpx.head", fake_head)
    yield


def test_doctor_all_skips_when_no_keys(cli: CliRunner, empty_env: None, no_network: None) -> None:
    result = cli.invoke(app, ["doctor"])
    # Exit 1 because env_any fails when no keys are set.
    assert result.exit_code == 1
    assert "EasyCat doctor" in result.stderr
    assert "EASYCAT_E203" in result.stderr


def test_doctor_passes_with_one_key(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    no_network: None,
) -> None:
    for var in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("NO_COLOR", "1")
    result = cli.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.stderr
    assert "openai reachable" in result.stderr


def test_doctor_json_envelope(cli: CliRunner, empty_env: None, no_network: None) -> None:
    result = cli.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "doctor"
    assert payload["status"] == "error"
    assert payload["environment"] == "dev"
    # Every check has name/status/detail keys.
    for check in payload["checks"]:
        assert "name" in check and "status" in check and "detail" in check


def test_doctor_unknown_environment(cli: CliRunner, empty_env: None) -> None:
    result = cli.invoke(app, ["doctor", "--environment", "bogus"])
    assert result.exit_code == 2
    assert "Unknown --environment" in result.stderr


def test_doctor_only_provider_filters_reachability(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    no_network: None,
) -> None:
    for var in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
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
    for var in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-stub")
    monkeypatch.setenv("NO_COLOR", "1")
    result = cli.invoke(app, ["doctor", "--provider", "openai"])
    assert result.exit_code == 1
    assert "EASYCAT_E203" in result.stderr
    assert "OPENAI_API_KEY" in result.stderr


def test_doctor_reports_httpx_failure(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ConnectError on the probe should surface as E204."""
    import httpx

    for var in ("DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("NO_COLOR", "1")

    def raising_head(url, **kw):  # noqa: ANN001
        raise httpx.ConnectError("no route")

    monkeypatch.setattr("httpx.head", raising_head)
    result = cli.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "EASYCAT_E204" in result.stderr


def test_doctor_check_functions_are_pure() -> None:
    """Each individual check returns a CheckResult; no side effects."""
    py_check = doctor_module.check_python_version()
    assert py_check.status == "ok"
    assert "Python" in py_check.detail
