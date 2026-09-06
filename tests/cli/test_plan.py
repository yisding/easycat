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


#: The three keys ``json_envelope`` stamps on every command payload; the plan
#: body keys are everything else.
_ENVELOPE_KEYS = frozenset({"schema_version", "command", "status"})


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

    # EQUALITY, not containment: a renamed, dropped, or accidentally-added
    # top-level key is exactly the regression this test is named for, and the
    # server-side half of the same contract
    # (``test_plan_payload_shares_the_cli_body_keys``) is pinned this strongly.
    assert set(payload) - _ENVELOPE_KEYS == {
        "profile",
        "selected",
        "missing_env",
        "missing_extras",
        "warnings",
        "blocking_errors",
        "has_blocking_errors",
        "issues",
    }
    assert _ENVELOPE_KEYS <= set(payload)
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


def test_plan_human_output_survives_bracketed_fields_and_fixes(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PP-3b: Rich must not eat ``[voice.default]`` or ``easycat[webrtc]``.

    Both DX2 headline shapes carry square brackets, which Rich reads as style
    tags unless every interpolation is escaped. Unescaped, the E202 row prints
    the copy-pasteable command ``uv add 'easycat'`` — which installs EasyCat
    WITHOUT the extra the row is about — and the E602 row loses its field
    entirely.
    """
    manifest = tmp_path / "easycat.toml"
    manifest.write_text(
        '[project]\nname = "plan-cli"\n\n[voice.default]\ntransport = "twilio"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("NO_COLOR", "1")
    _absent(monkeypatch, "twilio")
    _present(monkeypatch, "onnxruntime")

    output = " ".join(cli.invoke(app, ["plan", "--manifest", str(manifest)]).stdout.split())

    # The missing-extra row's fix must stay installable verbatim.
    assert "EASYCAT_E202 telephony (transport):" in output
    assert "uv add 'easycat[telephony]'" in output
    # The incomplete-selection row must keep its manifest-table field.
    assert "EASYCAT_E602 [voice.default] (-):" in output
    assert "`[voice.default]`" in output


def test_plan_human_output_does_not_crash_on_a_bracket_shaped_detail(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PP-3c: a closing-tag-shaped substring must not raise ``MarkupError``."""
    from easycat.errors import SetupIssue

    manifest = _write_manifest(tmp_path, "")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("NO_COLOR", "1")
    _present(monkeypatch, "aiortc", "livekit", "onnxruntime")
    monkeypatch.setattr(
        selection,
        "plan_issues",
        lambda _plan: [
            SetupIssue(code="EASYCAT_E602", reason="incomplete_selection", field="[/]", detail="x")
        ],
    )

    result = cli.invoke(app, ["plan", "--manifest", str(manifest)])

    assert result.exit_code == 0
    assert "EASYCAT_E602 [/] (-): x" in " ".join(result.stdout.split())


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
    # The fix must address THIS defect, not E602's code-wide "pick a known
    # transport" advice — the transport is already valid.
    assert "token = 'bearer-env:TWILIO_STREAM_TOKEN_SECRET'" in incomplete[0]["fix"]
    assert "needs a known `transport`" not in incomplete[0]["fix"]


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
