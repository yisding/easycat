"""``easycat explain`` — lookup flows, --list, meta topics, fuzzy match."""

from __future__ import annotations

import json
import re

from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.cli.diagnose._codes import META_ENTRIES
from easycat.cli.scaffold._schema import SCHEMA_V1_KEYS, available_templates
from easycat.errors import REGISTRY, register


def test_explain_help_names_meta_topics(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "--help"])

    assert result.exit_code == 0
    assert "Look up errors and CLI schema topics" in result.stdout
    assert "init-schema" in result.stdout
    assert "json-schema" in result.stdout
    assert "--list" in result.stdout


def test_explain_known_code(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "E101"])
    assert result.exit_code == 0, result.stderr
    assert "EASYCAT_E101" in result.stdout
    assert "Cause" in result.stdout
    assert "Fix" in result.stdout


def test_explain_preserves_bracketed_extra_install_hints(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "E202"])

    assert result.exit_code == 0, result.stderr
    assert "uv add 'easycat[openai-agents]'" in result.stdout
    assert "uv add 'easycat'  # or" not in result.stdout


def test_explain_preserves_bracketed_provider_extra_hint(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "E104"])
    stdout = " ".join(result.stdout.split())

    assert result.exit_code == 0, result.stderr
    assert "uv add 'easycat[deepgram]'" in stdout
    assert "uv add 'easycat'" not in stdout


def test_explain_accepts_full_prefix(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "EASYCAT_E101"])
    assert result.exit_code == 0
    assert "EASYCAT_E101" in result.stdout


def test_explain_case_insensitive(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "e101"])
    assert result.exit_code == 0
    assert "EASYCAT_E101" in result.stdout


def test_explain_unknown_suggests(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "E999"])
    assert result.exit_code == 2
    assert "EASYCAT_E501" in result.stderr
    assert "Did you mean" in result.stderr


def test_explain_list(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "--list"])
    assert result.exit_code == 0
    assert "EASYCAT_E101" in result.stdout
    assert "EASYCAT_E501" in result.stdout
    assert "Meta topics" in result.stdout


def test_explain_list_preserves_bracketed_headlines(cli: CliRunner) -> None:
    code = "EASYCAT_TEST_LIST"
    register(code, "Install easycat[openai-agents] for {extra}", cause="c", fix="f")
    try:
        result = cli.invoke(app, ["explain", "--list"])
    finally:
        REGISTRY.pop(code, None)

    assert result.exit_code == 0
    assert "Install easycat[openai-agents] for <extra>" in result.stdout
    assert "Install easycat for <extra>" not in result.stdout


def test_explain_list_json(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "--list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "explain"
    assert payload["status"] == "ok"
    codes = {entry["code"] for entry in payload["codes"]}
    assert "EASYCAT_E101" in codes
    slugs = {entry["slug"] for entry in payload["meta"]}
    assert {"exit-codes", "init-schema", "json-schema"} <= slugs


def test_explain_json_known_code(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "E101", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["code"] == "EASYCAT_E101"
    assert "Cause" not in payload  # "cause" (lowercase) is the field name
    assert "cause" in payload


def test_explain_meta_exit_codes(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "exit-codes"])
    assert result.exit_code == 0
    assert "Exit codes form a stable contract" in result.stdout


def test_explain_meta_init_schema(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "init-schema"])
    stdout = re.sub(r"\s+", " ", result.stdout)
    assert result.exit_code == 0
    assert "schema_version" in result.stdout
    assert "template" in result.stdout
    assert '"transport": "local" | "webrtc" | "twilio"' in result.stdout
    assert "stdio://" in result.stdout
    assert "filesystem" in result.stdout
    assert "easycat init --list-templates --json" in result.stdout
    assert "`create_command`" in result.stdout
    assert "`repo_create_command`" in result.stdout
    assert "`best_for`" in result.stdout
    assert "top-level `command_note`" in result.stdout
    assert "repository root" in stdout
    assert "post-scaffold check/run context" in stdout
    assert "`next_step_commands`" in result.stdout
    assert "copy/sync/doctor/check/docs/run" in stdout
    assert "`run_command`" in result.stdout
    assert "`check_command`" in result.stdout
    assert "EASYCAT_E102" in result.stdout
    assert "agent.py" in result.stdout

    body = META_ENTRIES["init-schema"].body
    template_block = body.split('"template": ', 1)[1].split('"stt"', 1)[0]
    documented_templates = set(re.findall(r'"([a-z0-9-]+)"', template_block))
    assert documented_templates == set(available_templates())

    documented_keys = set(re.findall(r'^\s+"(?P<key>[a-z_]+)":', body, flags=re.MULTILINE))
    assert documented_keys == SCHEMA_V1_KEYS


def test_explain_meta_json_schema_documents_error_fix(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "json-schema"])
    stdout = re.sub(r"\s+", " ", result.stdout)
    assert result.exit_code == 0
    assert "Successful commands may add command-specific fields" in result.stdout
    assert "`entries`, `source_url`, `command_note`" in result.stdout
    assert "easycat docs --json" in result.stdout
    assert "`label`, `path`, `audience`" in stdout
    assert "`description`, `url`, and optional `commands`" in stdout
    assert "`commands` in onboarding order" in stdout
    assert "`audience` labels the intended reader" in stdout
    assert "optional `commands`" in stdout
    assert "in onboarding order" in result.stdout
    assert "bare installed CLI hints, repo-local `uv run` hints" in stdout
    assert "uppercase placeholders such as PATH" in stdout
    assert "`templates`, `catalog`, `command_note`" in result.stdout
    assert "easycat init --list-templates --json" in result.stdout
    assert "catalog entries include" in result.stdout
    assert "`best_for`" in stdout
    assert "repo-root creation" in stdout
    assert "`path`, `template`, `pyproject_name`, `files`, `agent_lines`, `git`" in result.stdout
    assert "`run_command`, `check_command`" in result.stdout
    assert "`next_step_commands`" in result.stdout
    assert "post-scaffold check/run context" in stdout
    assert "easycat init NAME --json" in result.stdout
    assert "`fix`, `context`, and `exit_code`" in result.stdout
    assert "without inventing a fake" in result.stdout
    assert "`report_path`" in result.stdout
    assert "`path`" in result.stdout
    assert "`output_path`" in result.stdout
    assert "validate report .easycat/validation/latest.json --json" in stdout
    assert "bundles show PATH --json" in result.stdout
    assert "bundles export PATH --output DIR --json" in result.stdout
    assert "branch on `command`" in result.stdout
    assert "`status`, and `exit_code`" in result.stdout


def test_explain_meta_json_schema_json_includes_command_specific_fields(
    cli: CliRunner,
) -> None:
    result = cli.invoke(app, ["explain", "json-schema", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "explain"
    assert payload["slug"] == "json-schema"
    assert "`entries`, `source_url`, `command_note`" in payload["body"]
    assert "`label`, `path`, `audience`" in payload["body"]
    assert "`description`, `url`, and optional `commands`" in payload["body"]
    normalized_body = re.sub(r"\s+", " ", payload["body"])
    assert "`commands` in onboarding order" in normalized_body
    assert "`audience` labels the intended reader" in normalized_body
    assert "optional `commands`" in payload["body"]
    assert "in onboarding order" in payload["body"]
    assert "bare installed CLI hints, repo-local `uv run` hints" in normalized_body
    assert "uppercase placeholders such as PATH" in normalized_body
    assert "`templates`, `catalog`, `command_note`" in payload["body"]
    assert "`best_for`" in payload["body"]
    assert "`path`, `template`, `pyproject_name`, `files`, `agent_lines`, `git`" in payload["body"]
    assert "`run_command`, `check_command`" in payload["body"]
    assert "`next_step_commands`" in payload["body"]
    assert "post-scaffold check/run context" in re.sub(r"\s+", " ", payload["body"])
    assert "`report_path`" in payload["body"]
    assert "validate report .easycat/validation/latest.json --json" in payload["body"]
    assert "`path`" in payload["body"]
    assert "`output_path`" in payload["body"]
    assert "without inventing a fake" in payload["body"]


def test_explain_no_arg_is_error(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain"])
    assert result.exit_code == 2
    assert "Pass an error code" in result.stderr


def test_explain_no_arg_json_uses_standard_error_envelope(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "--json"])

    assert result.exit_code == 2
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "explain"
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert "Pass an error code" in payload["message"]
    assert "easycat explain E102" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_every_registered_code_renders(cli: CliRunner) -> None:
    """Smoke test: every code in the registry renders without crashing.

    Catches regressions where a new error code's headline template has
    placeholders the explain rendering path can't handle.
    """
    for code in REGISTRY:
        result = cli.invoke(app, ["explain", code])
        assert result.exit_code == 0, f"{code} rendering failed: {result.stderr}"
