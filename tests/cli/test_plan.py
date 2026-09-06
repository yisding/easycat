"""Focused CLI ergonomics tests for ``easycat plan``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.errors import EASYCAT_E501
from easycat.planning import selection


def test_plan_help_renders_profile_table_literally(cli: CliRunner) -> None:
    result = cli.invoke(app, ["plan", "--help"])
    help_text = " ".join(result.stdout.split())

    assert result.exit_code == 0
    assert "Voice profile table to plan" in help_text
    assert "voice.default" in help_text
    assert "The to plan" not in help_text


def _write_manifest(tmp_path: Path, body: str) -> Path:
    manifest = tmp_path / "easycat.toml"
    manifest.write_text(
        '[project]\nname = "plan-cli"\n\n[voice.default]\ntransport = "webrtc"\n' + body,
        encoding="utf-8",
    )
    return manifest


def test_plan_uses_the_shared_profile_resolution(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-1: ``plan`` no longer owns a private exception -> code mapping."""
    manifest = _write_manifest(tmp_path, "")

    def sentinel(*_args: object, **_kwargs: object) -> None:
        raise EASYCAT_E501(code="dx2-sentinel")

    monkeypatch.setattr(selection, "plan_selected_profile", sentinel)

    result = cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"])

    assert result.exit_code != 0
    assert json.loads(result.stdout)["code"] == "EASYCAT_E501"


def test_plan_unresolvable_backend_reports_e602(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-2: redaction must not eat the useful part of the planner message."""
    manifest = _write_manifest(tmp_path, 'vad = "silro"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")

    result = cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"])

    payload = json.loads(result.stdout)
    assert payload["code"] == "EASYCAT_E602"
    assert payload["context"]["path"] == "[voice.default]"
    assert "silro" in payload["message"]


def test_plan_unknown_provider_still_exits_two_with_e104(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-3: a coded planner error keeps its own code and exit."""
    manifest = _write_manifest(tmp_path, 'stt = "opnai"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")

    result = cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"])

    assert result.exit_code == 2
    assert json.loads(result.stdout)["code"] == "EASYCAT_E104"
    assert "Traceback" not in result.stdout


def test_plan_error_redacts_a_secret_shaped_manifest_value(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P-4: a secret inside an exception cause must not reach stdout."""
    secret = "sk-live-abcdef1234567890abcdef"
    manifest = _write_manifest(tmp_path, f'vad = "{secret}"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")

    result = cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"])

    payload = json.loads(result.stdout)
    assert payload["code"] == "EASYCAT_E602"
    assert secret not in result.stdout
    assert "[REDACTED_SECRET]" in payload["message"]


# ── DX2 PR2: the coded ``issues`` array on every plan surface ──────────


def _present(monkeypatch: pytest.MonkeyPatch, *modules: str) -> None:
    """Make ``provider_plan._module_available`` report *modules* present.

    Extras availability is environment-dependent, so a test about coded issues
    must pin it at the single private seam every extra check flows through.
    """
    from easycat.planning import provider_plan

    forced = set(modules)
    real = provider_plan._module_available
    monkeypatch.setattr(
        provider_plan,
        "_module_available",
        lambda module: True if module in forced else real(module),
    )


def _absent(monkeypatch: pytest.MonkeyPatch, *modules: str) -> None:
    from easycat.planning import provider_plan

    missing = set(modules)
    real = provider_plan._module_available
    monkeypatch.setattr(
        provider_plan,
        "_module_available",
        lambda module: False if module in missing else real(module),
    )


def test_plan_json_adds_issues_without_changing_existing_keys(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PP-1: the seven existing keys keep their shape; ``issues`` is additive."""
    manifest = _write_manifest(tmp_path, 'stt = "deepgram"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    _present(monkeypatch, "aiortc", "livekit", "onnxruntime")

    payload = json.loads(cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"]).stdout)

    assert {
        "profile",
        "selected",
        "missing_env",
        "missing_extras",
        "warnings",
        "blocking_errors",
        "has_blocking_errors",
        "issues",
    } <= set(payload)
    assert payload["blocking_errors"] == ["missing_env:DEEPGRAM_API_KEY"]
    assert payload["has_blocking_errors"] is True
    assert payload["selected"]["stt"]["required_env"] == "DEEPGRAM_API_KEY"


def test_plan_issues_name_role_field_and_code(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PP-2: a missing credential and a missing extra each carry role + code."""
    manifest = _write_manifest(tmp_path, 'stt = "deepgram"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    _absent(monkeypatch, "aiortc")

    payload = json.loads(cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"]).stdout)
    by_field = {issue["field"]: issue for issue in payload["issues"]}

    credential = by_field["DEEPGRAM_API_KEY"]
    assert credential["role"] == "stt"
    assert credential["code"] == "EASYCAT_E203"
    assert credential["reason"] == "missing_env"
    assert credential["severity"] == "blocking"
    assert credential["fix"]

    extra = by_field["webrtc"]
    assert extra["role"] == "transport"
    assert extra["code"] == "EASYCAT_E202"
    assert extra["reason"] == "missing_extra"


def test_plan_human_output_names_code_and_fix(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PP-3: the terminal surface prints the code and the fix, not just a token."""
    manifest = _write_manifest(tmp_path, 'stt = "deepgram"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    _present(monkeypatch, "aiortc", "livekit", "onnxruntime")

    result = cli.invoke(app, ["plan", "--manifest", str(manifest)])
    output = " ".join(result.stdout.split())

    assert "missing env: DEEPGRAM_API_KEY" in output
    assert "status: blocked" in output
    assert "EASYCAT_E203 DEEPGRAM_API_KEY (stt):" in output
    assert "Fix:" in output


def test_plan_reports_an_incomplete_phone_profile(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PP-4: a phone profile with no token is blocked, not silently ready."""
    manifest = tmp_path / "easycat.toml"
    manifest.write_text(
        '[project]\nname = "plan-cli"\n\n[voice.default]\ntransport = "twilio"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    # Fake the phone extras present so the redness proves the missing TOKEN,
    # not a missing install extra.
    _present(monkeypatch, "twilio", "onnxruntime")

    payload = json.loads(cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"]).stdout)

    assert payload["has_blocking_errors"] is True
    assert "incomplete_selection:[voice.default]" in payload["blocking_errors"]
    assert not [err for err in payload["blocking_errors"] if err.startswith("missing_extra:")]
    incomplete = [i for i in payload["issues"] if i["reason"] == "incomplete_selection"]
    assert [issue["code"] for issue in incomplete] == ["EASYCAT_E602"]
    assert "TWILIO_STREAM_TOKEN_SECRET" in incomplete[0]["detail"]


def test_plan_exit_code_is_zero_with_blocking_errors(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PP-5: pins today's verified behavior so a future change is deliberate."""
    manifest = _write_manifest(tmp_path, 'stt = "deepgram"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)

    result = cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["has_blocking_errors"] is True


def test_plan_and_doctor_report_the_same_cause(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PP-6: the headline acceptance case — one manifest, one code, two surfaces."""
    manifest = _write_manifest(tmp_path, 'stt = "deepgram"\n')
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    _present(monkeypatch, "aiortc", "livekit", "onnxruntime")

    planned = json.loads(cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"]).stdout)
    diagnosed = json.loads(
        cli.invoke(
            app, ["doctor", "--manifest", str(manifest), "--json", "--environment", "production"]
        ).stdout
    )

    issue = next(i for i in planned["issues"] if i["field"] == "DEEPGRAM_API_KEY")
    row = next(r for r in diagnosed["checks"] if r["name"] == "env_deepgram")

    assert issue["code"] == row["code"] == "EASYCAT_E203"
    assert issue["role"] == row["role"] == "stt"
    assert row["field"] == "DEEPGRAM_API_KEY"
