"""``easycat explain`` — lookup flows, --list, meta topics, fuzzy match."""

from __future__ import annotations

import json
import re

from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.cli.diagnose._codes import META_ENTRIES
from easycat.cli.scaffold._schema import SCHEMA_V1_KEYS, available_templates
from easycat.errors import REGISTRY


def test_explain_known_code(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain", "E101"])
    assert result.exit_code == 0, result.stderr
    assert "EASYCAT_E101" in result.stdout
    assert "Cause" in result.stdout
    assert "Fix" in result.stdout


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
    assert result.exit_code == 0
    assert "schema_version" in result.stdout
    assert "template" in result.stdout
    assert '"transport": "local" | "webrtc" | "twilio"' in result.stdout
    assert "stdio://" in result.stdout
    assert "filesystem" in result.stdout
    assert "easycat init --list-templates --json" in result.stdout
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
    assert result.exit_code == 0
    assert "`fix`, `context`, and `exit_code`" in result.stdout
    assert "without inventing a fake" in result.stdout
    assert "`report_path`" in result.stdout
    assert "`path`" in result.stdout
    assert "`output_path`" in result.stdout
    assert "validate report PATH --json" in result.stdout
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
    assert "`report_path`" in payload["body"]
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
