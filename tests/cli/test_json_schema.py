"""Plan 11 — JSON envelope stability.

Every ``--json`` output shares a versioned envelope:

    {"schema_version": 1, "command": "...", "status": "ok"|"error", ...}

These tests walk the primary CLI JSON command families and high-risk aliases,
checking the envelope shape against a single schema. Command-specific suites
cover deeper payload behavior for individual subcommands. Drift here is a
breaking change for coding-agent consumers.

See ``TEST_PLANS.md`` §11.
"""

from __future__ import annotations

import json
import shlex
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import _DOCS_COMMAND_NOTE, app
from easycat.debug.bundle import FORMAT_VERSION
from easycat.validation.report import (
    GitMetadata,
    ValidationCheck,
    ValidationEnvironment,
    ValidationRun,
)
from easycat.validation.runner import ValidationRunResult


def _assert_envelope(payload: dict, command: str, status: str = "ok") -> None:
    assert payload.get("schema_version") == 1, payload
    assert payload.get("command") == command, payload
    assert payload.get("status") == status, payload


def _validation_run() -> ValidationRun:
    return ValidationRun(
        run_id="20260521T120000Z-quick-12345",
        command=["uv", "run", "pytest", "-q"],
        started_at=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 21, 12, 0, 3, tzinfo=UTC),
        duration_s=3.25,
        status="pass",
        exit_code=0,
        tool_exit_codes={"pytest": 0},
        git=GitMetadata(sha="abc123", branch="feature/validation", dirty=True),
        environment=ValidationEnvironment(
            python="3.12.13",
            platform="Linux",
            ci=False,
            env_vars={"OPENAI_API_KEY": True},
        ),
        checks=[
            ValidationCheck(
                name="pytest.quick",
                status="pass",
                duration_s=2.75,
                command=["uv", "run", "pytest", "-q"],
            )
        ],
    )


def _make_bundle(path: Path, records: list[dict]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format_version": FORMAT_VERSION,
                    "provider_versions": {"stt": "openai-realtime-1.0"},
                    "replay_entry_points": [{"sequence": 7, "stage": "stt", "unit_id": "u1"}],
                }
            ),
        )
        zf.writestr("journal.ndjson", "\n".join(json.dumps(record) for record in records))


def test_explain_single_code_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "E101", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "explain")
    assert payload["code"] == "EASYCAT_E101"
    # Required docs fields.
    for key in ("headline", "cause", "fix"):
        assert key in payload


def test_explain_list_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "--list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "explain")
    assert isinstance(payload["codes"], list)
    assert isinstance(payload["meta"], list)
    assert all("code" in c for c in payload["codes"])


def test_explain_meta_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "init-schema", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "explain")
    assert payload["slug"] == "init-schema"


def test_explain_unknown_code_error_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "E999", "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "explain", status="error")
    assert payload["code"] == "EASYCAT_E501"
    assert "Unknown error code" in payload["message"]
    assert "easycat explain --list" in payload["fix"]
    assert payload["context"] == {"code": "E999"}
    assert payload["query"] == "E999"
    assert payload["exit_code"] == 2


def test_explain_usage_error_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "explain", status="error")
    assert payload["exit_code"] == 2
    assert "Pass an error code" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_init_envelope(cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    _assert_envelope(payload, "init")
    assert payload["template"] == "text-chat"
    assert payload["pyproject_name"] == "demo"
    assert isinstance(payload["files"], list)
    assert isinstance(payload["agent_lines"], int)
    assert isinstance(payload["git"], bool)
    assert payload["run_command"] == "uv run --env-file .env python agent.py"
    assert payload["check_command"] == "uv run python -m py_compile agent.py"
    assert payload["next_step_commands"] == [
        f"cd {shlex.quote(str(tmp_path / 'demo'))}",
        "cp .env.example .env",
        "uv sync",
        "uv run easycat doctor --env-file .env",
        "uv run python -m py_compile agent.py",
        "uv run easycat docs",
        "uv run easycat docs --json",
        "uv run --env-file .env python agent.py",
    ]
    assert "after cd into the scaffolded project" in payload["command_note"]


def test_init_list_templates_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["init", "--list-templates", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "init")
    assert isinstance(payload["templates"], list)
    assert isinstance(payload["catalog"], list)
    assert "installed CLI form" in payload["command_note"]
    assert "repository root" in payload["command_note"]
    assert "after cd into the scaffolded project" in payload["command_note"]
    required_keys = {
        "name",
        "mode",
        "transport",
        "framework",
        "best_for",
        "base_extras",
        "required_env",
        "optional_env",
        "files",
        "description",
        "create_command",
        "repo_create_command",
        "run_command",
        "check_command",
    }
    for entry in payload["catalog"]:
        assert required_keys <= set(entry)
        assert entry["create_command"] == f"easycat init my-agent --template {entry['name']}"
        assert entry["repo_create_command"] == (
            f"uv run easycat init my-agent --template {entry['name']}"
        )
        assert entry["run_command"].startswith("uv run ")
        assert entry["check_command"].startswith("uv run python -m py_compile ")
        assert isinstance(entry["best_for"], str)
        assert entry["best_for"]
        assert isinstance(entry["base_extras"], list)
        assert entry["base_extras"]
        assert isinstance(entry["required_env"], list)
        assert entry["required_env"]
        assert isinstance(entry["optional_env"], list)
        assert isinstance(entry["files"], list)
        assert "README.md" in entry["files"]


def test_init_error_envelope(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = cli.invoke(app, ["init", "demo", "--config", "not json", "--json"])
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "init", status="error")
    assert payload["code"] == "EASYCAT_E102"
    assert payload["fix"]
    assert "easycat explain init-schema" in payload["fix"]
    assert payload["exit_code"] == 4


def test_init_usage_error_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["init", "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "init", status="error")
    assert payload["exit_code"] == 2
    assert "Missing argument 'NAME'" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_doctor_ok_envelope(cli: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OPENAI_API_KEY", "DEEPGRAM_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")
    monkeypatch.setenv("NO_COLOR", "1")

    def fake_head(url, **kw):  # noqa: ANN001
        class _R:
            status_code = 200

        return _R()

    monkeypatch.setattr("httpx.head", fake_head)
    result = cli.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "doctor")
    assert payload["environment"] == "dev"
    # Every check row has the required shape.
    for check in payload["checks"]:
        assert "name" in check and "status" in check and "detail" in check


def test_doctor_error_envelope(
    cli: CliRunner, empty_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_head(url, **kw):  # noqa: ANN001
        class _R:
            status_code = 200

        return _R()

    monkeypatch.setattr("httpx.head", fake_head)
    result = cli.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "doctor", status="error")


def test_doctor_usage_error_envelope(cli: CliRunner, tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OPENAI_API_KEY sk-stub\n", encoding="utf-8")

    result = cli.invoke(app, ["doctor", "--env-file", str(env_file), "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "doctor", status="error")
    assert payload["exit_code"] == 2
    assert "Invalid --env-file" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_docs_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["docs", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "docs")
    assert isinstance(payload["entries"], list)
    assert payload["command_note"] == _DOCS_COMMAND_NOTE
    entries = [{key: entry[key] for key in ("label", "path")} for entry in payload["entries"]]
    assert {"label": "CLI and scaffolds", "path": "README.md#cli"} in entries
    assert {"label": "Docs map", "path": "docs/README.md"} in entries
    assert {"label": "First lesson", "path": "docs/teaching/00-hello-audio/"} in entries
    assert {"label": "Examples", "path": "examples/README.md"} in entries
    assert {"label": "Architecture", "path": "CLAUDE.md"} in entries
    assert {"label": "Contributing", "path": "CONTRIBUTING.md"} in entries
    assert {"label": "Deployment", "path": "docs/deployment/docker.md"} in entries
    assert {"label": "Observability", "path": "docs/observability.md"} in entries
    assert {"label": "Validation reference", "path": "plan/validation/reference.md"} in entries
    assert all(isinstance(entry.get("description"), str) for entry in payload["entries"])
    assert all(isinstance(entry.get("audience"), str) for entry in payload["entries"])
    assert all(isinstance(entry.get("url"), str) for entry in payload["entries"])
    assert all(
        isinstance(command, str)
        for entry in payload["entries"]
        for command in entry.get("commands", [])
    )
    descriptions = {entry["path"]: entry["description"] for entry in payload["entries"]}
    audiences = {entry["path"]: entry["audience"] for entry in payload["entries"]}
    commands = {entry["path"]: entry.get("commands", []) for entry in payload["entries"]}
    assert "base extras" in descriptions["README.md#cli"]
    assert "env requirements" in descriptions["README.md#cli"]
    assert "optional env knobs" in descriptions["README.md#cli"]
    assert "generated files" in descriptions["README.md#cli"]
    assert "copyable create/check/run commands" in descriptions["README.md#cli"]
    assert audiences["README.md#install"] == "new users"
    assert audiences["README.md#cli"] == "app builders"
    assert audiences["CLAUDE.md"] == "maintainers"
    assert audiences["README.md#validation-workflow"] == "contributors"
    assert "easycat init --list-templates" in commands["README.md#cli"]
    assert "uv run pytest tests/test_install_guidance.py" in commands["CLAUDE.md"]
    assert "easycat validate quick" in commands["README.md#validation-workflow"]
    assert all(entry["url"].startswith(payload["source_url"]) for entry in payload["entries"])


def test_validate_quick_envelope(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run_validation_slice(slice_name: str, **kwargs: object) -> ValidationRunResult:
        assert slice_name == "quick"
        run = _validation_run()
        result_report = tmp_path / "run" / "report.json"
        result_report.parent.mkdir()
        result_report.write_text(run.to_json())
        return ValidationRunResult(
            run=run,
            run_dir=result_report.parent,
            report_path=result_report,
            exit_code=0,
        )

    monkeypatch.setattr("easycat.cli.validate.run_validation_slice", fake_run_validation_slice)

    result = cli.invoke(app, ["validate", "quick", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "validate quick")
    assert payload["exit_code"] == 0
    assert payload["validation"]["kind"] == "validation_run"


def test_validate_usage_error_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["validate", "latency", "--smoke", "--sweep", "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "validate latency", status="error")
    assert payload["exit_code"] == 2
    assert "choose only one of --smoke or --sweep" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_validate_report_envelope(cli: CliRunner, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text(_validation_run().to_json())

    result = cli.invoke(app, ["validate", "report", str(report_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "validate report")
    assert payload["report_path"] == str(report_path)
    assert payload["exit_code"] == 0
    assert payload["validation"]["kind"] == "validation_run"


def test_validate_report_command_error_envelope(cli: CliRunner, tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    report_path.write_text("{not-json")

    result = cli.invoke(app, ["validate", "report", str(report_path), "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "validate report", status="error")
    assert payload["report_path"] == str(report_path)
    assert payload["exit_code"] == 2
    assert "invalid validation report JSON" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_validate_report_missing_command_error_envelope(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "missing.json"

    result = cli.invoke(app, ["validate", "report", str(report_path), "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "validate report", status="error")
    assert payload["report_path"] == str(report_path)
    assert payload["exit_code"] == 2
    assert "validation report not found" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_bundles_list_envelope(cli: CliRunner, tmp_path: Path) -> None:
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    _make_bundle(recordings / "one.zip", [{"sequence": 1, "name": "TurnStarted"}])

    result = cli.invoke(app, ["bundles", "list", "--path", str(tmp_path), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "bundles_list")
    assert payload["scanned"] == str(tmp_path)
    assert len(payload["bundles"]) == 1


def test_bundles_show_and_inspect_envelopes(cli: CliRunner, tmp_path: Path) -> None:
    bundle = tmp_path / "demo.zip"
    _make_bundle(
        bundle,
        [
            {
                "sequence": 1,
                "name": "TurnStarted",
                "turn_id": "t1",
                "session_id": "sess-xyz",
            }
        ],
    )

    show = cli.invoke(app, ["bundles", "show", str(bundle), "--json"])
    inspect = cli.invoke(app, ["inspect", str(bundle), "--json"])

    assert show.exit_code == 0
    show_payload = json.loads(show.stdout)
    _assert_envelope(show_payload, "bundles_show")
    assert show_payload["path"] == str(bundle)
    assert show_payload["session_id"] == "sess-xyz"

    assert inspect.exit_code == 0
    inspect_payload = json.loads(inspect.stdout)
    _assert_envelope(inspect_payload, "bundles_show")
    assert inspect_payload == show_payload


def test_bundles_show_error_envelope(cli: CliRunner, tmp_path: Path) -> None:
    missing = tmp_path / "missing.zip"

    result = cli.invoke(app, ["bundles", "show", str(missing), "--json"])

    assert result.exit_code == 5
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "bundles_show", status="error")
    assert payload["path"] == str(missing)
    assert payload["exit_code"] == 5
    assert "Bundle not found" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_bundles_export_envelope(cli: CliRunner, tmp_path: Path) -> None:
    bundle = tmp_path / "demo.zip"
    output = tmp_path / "pack"
    _make_bundle(bundle, [{"sequence": 1, "name": "TurnStarted", "session_id": "sess-xyz"}])

    result = cli.invoke(app, ["bundles", "export", str(bundle), "--output", str(output), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "bundles_export")
    assert payload["output_path"] == str(output)
    assert payload["target"] == "claude-code"
    assert set(payload["files"]) == {"README.md", "summary.json", "timeline.md", "timeline.jsonl"}


def test_bundles_export_error_envelope(cli: CliRunner, tmp_path: Path) -> None:
    bundle = tmp_path / "demo.zip"
    output = tmp_path / "pack"
    output.mkdir()
    _make_bundle(bundle, [{"sequence": 1, "name": "TurnStarted", "session_id": "sess-xyz"}])

    result = cli.invoke(
        app,
        ["bundles", "export", str(bundle), "--output", str(output), "--json"],
    )

    assert result.exit_code == 101
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "bundles_export", status="error")
    assert payload["output_path"] == str(output)
    assert payload["exit_code"] == 101
    assert "already exists" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_replay_envelope(cli: CliRunner, tmp_path: Path) -> None:
    bundle = tmp_path / "demo.zip"
    _make_bundle(
        bundle,
        [
            {
                "sequence": 1,
                "kind": "event",
                "name": "stage_start",
                "turn_id": "t1",
                "session_id": "sess-xyz",
                "data": {"stage": "stt"},
            },
            {
                "sequence": 2,
                "kind": "event",
                "name": "stage_end",
                "turn_id": "t1",
                "session_id": "sess-xyz",
                "data": {"stage": "stt"},
            },
        ],
    )

    result = cli.invoke(app, ["replay", str(bundle), "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "replay")
    assert payload["fidelity_requested"] == "artifact"
    assert payload["frames"] == 2
    assert payload["stages"] == ["stt"]


def test_replay_error_envelope(cli: CliRunner, tmp_path: Path) -> None:
    bundle = tmp_path / "tool.zip"
    _make_bundle(
        bundle,
        [
            {
                "sequence": 1,
                "kind": "framework_transition",
                "name": "tool_call_started",
                "turn_id": "t1",
                "session_id": "sess-xyz",
                "data": {"phase": "tool_call", "tool_name": "get_weather", "call_id": "c1"},
            }
        ],
    )

    result = cli.invoke(app, ["replay", str(bundle), "--json"])

    assert result.exit_code == 6
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "replay", status="error")
    assert payload["path"] == str(bundle)
    assert payload["exit_code"] == 6
    assert "Replay blocked" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_stdout_is_parseable_json_even_with_stderr_noise(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The contract is ``stdout = json, stderr = logs``.  A consumer
    should be able to ``| jq`` the output without parsing errors even
    if stderr carries progress/logs.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    result = cli.invoke(app, ["explain", "E101", "--json"])
    # Pure JSON stdout — json.loads must succeed without stripping.
    json.loads(result.stdout)
