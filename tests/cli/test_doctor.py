"""``easycat doctor`` — environment checks and --json envelope."""

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
    assert "openai reachable" in result.stderr


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
    assert "cartesia reachable" in result.stderr
    scoped = cli.invoke(app, ["doctor", "--provider", "cartesia"])
    assert scoped.exit_code == 0, scoped.stderr


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


def test_doctor_reports_httpx_failure(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ConnectError on the probe should surface as E204."""
    import httpx

    for var in ("DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY", "CARTESIA_API_KEY"):
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


def test_check_journal_writable_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path, empty_env: None
) -> None:
    """Pointing XDG_CACHE_HOME at a writable tmp dir yields ok."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    result = doctor_module.check_journal_writable()
    assert result.status == "ok", result.detail
    assert str(tmp_path) in result.detail


def test_check_journal_writable_fails_on_readonly(
    monkeypatch: pytest.MonkeyPatch, tmp_path, empty_env: None
) -> None:
    """If the journal dir can't be created, surface E207."""
    # Point at a path that collides with a regular file, so mkdir() fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setenv("XDG_CACHE_HOME", str(blocker))
    result = doctor_module.check_journal_writable()
    assert result.status == "fail"
    assert result.code == "EASYCAT_E207"


def test_check_disk_space_reports_free_megabytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path, empty_env: None
) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    result = doctor_module.check_disk_space()
    assert result.status in {"ok", "fail"}
    assert "MB free" in result.detail


def test_check_disk_space_fails_under_threshold(
    monkeypatch: pytest.MonkeyPatch, tmp_path, empty_env: None
) -> None:
    """Force the threshold higher than any realistic free space."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    result = doctor_module.check_disk_space(min_free_mb=10**12)
    assert result.status == "fail"
    assert result.code == "EASYCAT_E208"


def test_doctor_fix_creates_journal_dir(
    cli: CliRunner,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    no_network: None,
    empty_env: None,
) -> None:
    """``--fix`` mkdirs the journal directory when E207 is reported."""
    # Stage 1: block the default mkdir by pointing XDG_CACHE_HOME at a
    # non-existent nested path, then remove any previously-created dir.
    cache = tmp_path / "never-created"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")

    # Pre-condition: the dir doesn't exist yet.
    journal_dir = cache / "easycat" / "journals"
    assert not journal_dir.exists()

    # Running doctor once with --fix should create it.
    result = cli.invoke(app, ["doctor", "--fix"])
    assert journal_dir.exists(), "journal dir should have been auto-created by --fix"
    # Exit code should be 0 after remediation, since all other checks pass.
    assert result.exit_code == 0, result.stderr
