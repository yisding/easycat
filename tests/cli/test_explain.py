"""``easycat explain`` — lookup flows, --list, meta topics, fuzzy match."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from easycat.cli._app import app
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


def test_explain_no_arg_is_error(cli: CliRunner) -> None:
    result = cli.invoke(app, ["explain"])
    assert result.exit_code == 2
    assert "Pass an error code" in result.stderr


def test_every_registered_code_renders(cli: CliRunner) -> None:
    """Smoke test: every code in the registry renders without crashing.

    Catches regressions where a new error code's headline template has
    placeholders the explain rendering path can't handle.
    """
    for code in REGISTRY:
        result = cli.invoke(app, ["explain", code])
        assert result.exit_code == 0, f"{code} rendering failed: {result.stderr}"
