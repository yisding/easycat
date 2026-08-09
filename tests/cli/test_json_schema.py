"""Plan 12 — JSON envelope stability.

Every ``--json`` output shares a versioned envelope:

    {"schema_version": 1, "command": "...", "status": "ok"|"error", ...}

These tests walk the primary CLI JSON command families and high-risk aliases,
checking the envelope shape against a single schema. Command-specific suites
cover deeper payload behavior for individual subcommands. Drift here is a
breaking change for coding-agent consumers.

See ``TEST_PLANS.md`` §12.
"""

from __future__ import annotations

import ast
import json
import shlex
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import (
    _DOCS_AUDIENCE_ALIAS_NOTE,
    _DOCS_COMMAND_NOTE,
    _available_docs_audience_filters,
    app,
)
from easycat.cli.scaffold import init as init_module
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
    monkeypatch.setattr(init_module, "_editable_easycat_source", lambda: None)
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
    assert payload["easycat_source"] is None
    assert payload["easycat_git"] is None
    assert payload["easycat_git_rev"] is None
    assert payload["run_command"] == "uv run --env-file .env python agent.py"
    assert payload["check_command"] == "uv run ruff check agent.py"
    assert payload["fix_command"] == "uv run ruff check --fix agent.py"
    assert payload["next_step_commands"] == [
        f"cd {shlex.quote(str(tmp_path / 'demo'))}",
        "cp .env.example .env",
        "uv sync",
        "uv run easycat doctor --env-file .env",
        "uv run easycat doctor --env-file .env --json",
        "uv run ruff check agent.py",
        "uv run ruff check --fix agent.py",
        "uv run easycat docs",
        "uv run easycat docs --audience app-builders",
        "uv run easycat docs --audience app-builders --json",
        "uv run easycat docs --json",
        "uv run easycat explain json-schema",
        "uv run --env-file .env python agent.py",
    ]
    assert "after cd into the scaffolded project" in payload["command_note"]
    assert "fix_command run after cd into the scaffolded project" in payload["command_note"]


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
        "base_requirement",
        "required_env",
        "optional_env",
        "files",
        "description",
        "create_command",
        "repo_create_command",
        "next_step_commands",
        "run_command",
        "check_command",
        "fix_command",
    }
    for entry in payload["catalog"]:
        audience_docs, audience_docs_json = init_module._next_step_audience_docs_commands(
            entry["name"]
        )
        assert required_keys <= set(entry)
        assert entry["create_command"] == f"easycat init my-agent --template {entry['name']}"
        assert entry["repo_create_command"] == (
            f"uv run easycat init my-agent --template {entry['name']}"
        )
        assert entry["next_step_commands"] == [
            "cd my-agent",
            "cp .env.example .env",
            "uv sync",
            "uv run easycat doctor --env-file .env",
            "uv run easycat doctor --env-file .env --json",
            entry["check_command"],
            entry["fix_command"],
            "uv run easycat docs",
            audience_docs,
            audience_docs_json,
            "uv run easycat docs --json",
            "uv run easycat explain json-schema",
            entry["run_command"],
        ]
        assert entry["run_command"].startswith("uv run ")
        assert entry["check_command"].startswith("uv run ruff check ")
        assert entry["fix_command"].startswith("uv run ruff check --fix ")
        assert isinstance(entry["best_for"], str)
        assert entry["best_for"]
        assert isinstance(entry["base_extras"], list)
        assert entry["base_extras"]
        assert entry["base_requirement"].startswith("easycat[")
        assert entry["base_requirement"].endswith(f"]>={init_module._easycat_version_floor()}")
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


@pytest.mark.parametrize(
    ("config", "message_fragment"),
    [
        (
            '{"schema_version":' + ("9" * 5000) + ',"template":"text-chat"}',
            "not valid JSON",
        ),
        (
            (
                '{"schema_version":1,"template":"text-chat","tools":'
                + ("[" * 10_000)
                + '"lookup"'
                + ("]" * 10_000)
                + "}"
            ),
            None,
        ),
    ],
    ids=["oversized-integer", "excessive-nesting"],
)
def test_init_decoder_resource_errors_use_error_envelope(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config: str,
    message_fragment: str | None,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = cli.invoke(app, ["init", "demo", "--config", config, "--no-git", "--json"])

    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "init", status="error")
    assert payload["code"] == "EASYCAT_E102"
    if message_fragment is not None:
        assert message_fragment in payload["message"]
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

    def fake_head(url, **kw):
        class _R:
            status_code = 200

        return _R()

    monkeypatch.setattr("httpx.head", fake_head)
    result = cli.invoke(app, ["doctor", "--provider", "openai", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "doctor")
    assert payload["environment"] == "dev"
    # Every check row has the required shape.
    for check in payload["checks"]:
        assert {"name", "status", "detail", "requirement"} <= set(check)


def test_doctor_error_envelope(
    cli: CliRunner, empty_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_head(url, **kw):
        class _R:
            status_code = 200

        return _R()

    monkeypatch.setattr("httpx.head", fake_head)
    result = cli.invoke(app, ["doctor", "--provider", "openai", "--json"])
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
    """JSON-envelope shape for ``docs --json``."""
    result = cli.invoke(app, ["docs", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "docs")
    assert isinstance(payload["entries"], list)
    assert payload["command_note"] == _DOCS_COMMAND_NOTE
    assert payload["audience_alias_note"] == _DOCS_AUDIENCE_ALIAS_NOTE
    assert payload["available_audience_filters"] == list(_available_docs_audience_filters())
    assert "app-builders" in payload["available_audience_filters"]
    assert "coding-agents" in payload["available_audience_filters"]
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


def _json_command_paths() -> list[tuple[str, ...]]:
    """Walk the registered CLI tree, returning every command exposing ``--json``.

    Each entry is the command path as a tuple of names (e.g. ``("plan",)`` or
    ``("validate", "quick")``). A command is included when any of its params
    declares the ``--json`` flag.
    """
    import click
    from typer.main import get_command

    from easycat.cli._app import _register_commands

    _register_commands()
    root = get_command(app)

    found: list[tuple[str, ...]] = []

    def _walk(command: click.Command, path: tuple[str, ...]) -> None:
        if isinstance(command, click.Group):
            for name, sub in command.commands.items():
                _walk(sub, (*path, name))
            return
        has_json = any("--json" in (p.opts or []) for p in command.params)
        if has_json:
            found.append(path)

    for name, sub in root.commands.items():
        _walk(sub, (name,))
    return found


# Pre-existing ``--json`` commands whose envelope ``command`` token does not
# match their CLI path and that predate this coverage guard. ``inspect`` and
# ``latency`` are top-level ALIASES of ``bundles show`` / ``validate latency``,
# so they emit (and are asserted under) the canonical tokens ``"bundles_show"``
# / ``"validate latency"`` — never their bare path. (The old substring guard
# matched them vacuously off unrelated occurrences; the AST guard below requires
# this explicit acknowledgement.) New ``--json`` commands (like ``plan``) must
# NOT be added here — they must carry a real envelope assertion instead, which is
# the whole point of the guard.
_LEGACY_JSON_COMMANDS_WITHOUT_ENVELOPE_ASSERTION = frozenset(
    {"diff", "tail", "inspect", "latency"}
)


def _asserted_envelope_command_tokens() -> set[str]:
    """Command tokens with a real ``_assert_envelope(payload, "<cmd>")`` call.

    Parsed via AST (the ``command`` is the 2nd positional arg) so an incidental
    substring — a docstring word, an unrelated dict key like ``"plan"`` — cannot
    satisfy the coverage guard vacuously the way a raw ``in source`` check could.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    tokens: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "_assert_envelope":
            continue
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            value = node.args[1].value
            if isinstance(value, str):
                tokens.add(value)
    return tokens


def test_every_json_command_has_an_envelope_assertion() -> None:
    """A new ``--json`` command must carry an explicit envelope assertion here.

    ``tests/cli/test_json_schema.py`` is the ONLY guard that exercises the
    ``--json`` envelope, and it has no registry walk — so a new ``--json``
    command would otherwise slip past uncovered. This walks the CLI tree and
    fails when a ``--json``-capable command lacks an ``_assert_envelope(payload,
    "<command>")`` (or ``"<group> <sub>"``) call in this file (matched by AST,
    not substring, so the assertion must be real). A small allow-list of legacy
    commands that predate the guard is excluded; new commands must not be added.
    """
    asserted = _asserted_envelope_command_tokens()
    missing: list[str] = []
    for path in _json_command_paths():
        # The envelope ``command`` field uses the space-joined command path
        # (matching how validate uses "validate quick"); single commands use the
        # bare name.
        command_token = " ".join(path)
        if command_token in _LEGACY_JSON_COMMANDS_WITHOUT_ENVELOPE_ASSERTION:
            continue
        if command_token not in asserted:
            missing.append(command_token)
    assert not missing, (
        "These --json commands have no envelope assertion in test_json_schema.py: "
        + ", ".join(sorted(missing))
    )


def _write_plan_manifest(tmp_path: Path, *, vad: str = "silero") -> Path:
    manifest = tmp_path / "easycat.toml"
    manifest.write_text(
        "\n".join(
            [
                "[project]",
                'name = "plan-test"',
                "",
                "[server]",
                'auth = "bearer-env:EASYCAT_SERVE_TOKEN"',
                "",
                "[voice.default]",
                'transport = "webrtc"',
                'stt = "openai/realtime"',
                'tts = "openai"',
                f'vad = "{vad}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    return manifest


def test_plan_envelope(cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The manifest selects openai (stt/tts), silero (vad), and webrtc (transport);
    # asserting no blocking errors requires those extras installed. Project CI's
    # quick lane runs without extras, so skip there rather than fail.
    pytest.importorskip("openai")
    pytest.importorskip("onnxruntime")
    pytest.importorskip("aiortc")
    manifest = _write_plan_manifest(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")

    result = cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "plan")
    assert payload["profile"] == "default"
    # selected is keyed by role, each carrying the ProviderSelection fields.
    selected = payload["selected"]
    assert set(selected) == {
        "stt",
        "tts",
        "vad",
        "transport",
        "agent",
        "noise_reducer",
        "echo_canceller",
    }
    for role, selection in selected.items():
        assert selection["role"] == role
        for key in ("provider", "config_type", "extra", "required_env", "capabilities"):
            assert key in selection
        assert isinstance(selection["capabilities"], list)
    assert isinstance(payload["missing_env"], list)
    assert isinstance(payload["missing_extras"], list)
    assert isinstance(payload["warnings"], list)
    assert isinstance(payload["blocking_errors"], list)
    assert payload["has_blocking_errors"] is False


def test_plan_missing_manifest_error_envelope(cli: CliRunner, tmp_path: Path) -> None:
    missing = tmp_path / "nope.toml"

    result = cli.invoke(app, ["plan", "--manifest", str(missing), "--json"])

    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "plan", status="error")
    assert payload["code"] == "EASYCAT_E601"
    assert payload["exit_code"] != 0


def test_plan_unknown_profile_error_envelope(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = _write_plan_manifest(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")

    result = cli.invoke(app, ["plan", "--manifest", str(manifest), "--profile", "nope", "--json"])

    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "plan", status="error")
    assert payload["code"] == "EASYCAT_E602"


def test_plan_unresolvable_backend_error_envelope(
    cli: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unknown vad backend makes the planner RAISE a bare ValueError; the CLI
    # must surface a clean coded E602 envelope, not a raw traceback.
    manifest = _write_plan_manifest(tmp_path, vad="silro")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-stub")

    result = cli.invoke(app, ["plan", "--manifest", str(manifest), "--json"])

    assert result.exit_code != 0
    payload = json.loads(result.stdout)
    _assert_envelope(payload, "plan", status="error")
    assert payload["code"] == "EASYCAT_E602"


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
