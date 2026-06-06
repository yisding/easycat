"""Tests for bundle listing, inspection, export, and replay CLI flows."""

from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.cli.debug.bundles import _format_size
from easycat.debug.bundle import FORMAT_VERSION
from easycat.debug.export import export_debug_bundle
from easycat.runtime.records import ErrorInfo, JournalRecord, TimingInfo


class _FakeJournal:
    """Minimal journal stub exposing ``read`` for ``export_debug_bundle``."""

    def __init__(self, records: list[JournalRecord]) -> None:
        self._records = records

    def read(self, start: int = 0, limit: int | None = None) -> list[JournalRecord]:
        return self._records[start:]


class _FakeSession:
    """Minimal session stub for driving the real bundle export path."""

    def __init__(self, *, records: list[JournalRecord]) -> None:
        self._debug = "light"
        self._journal = _FakeJournal(records)
        self._artifact_store = None
        self._config = None


def _make_bundle(
    path: Path,
    records: list[dict],
    *,
    provider_versions: dict[str, str] | None = None,
) -> None:
    """Roll a minimal valid bundle zip at *path*."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format_version": FORMAT_VERSION,
                    "provider_versions": provider_versions or {"stt": "openai-realtime-1.0"},
                    "replay_entry_points": [{"sequence": 7, "stage": "stt", "unit_id": "u1"}],
                }
            ),
        )
        zf.writestr("journal.ndjson", "\n".join(json.dumps(r) for r in records))


def _make_crash_dump(path: Path, records: list[dict]) -> None:
    """Write a minimal crash-dump SQLite journal at *path*.

    Mirrors the ``journal`` table schema that ``_read_journal_ndjson``
    reads from a crashed session's SQLite file.
    """
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE journal ("
            "sequence INTEGER, session_id TEXT, kind TEXT, name TEXT, "
            "wall_ns INTEGER, mono_ns INTEGER, turn_id TEXT, data TEXT, "
            "error_type TEXT, error_msg TEXT, input_ref TEXT, output_ref TEXT, tags TEXT)"
        )
        for r in records:
            conn.execute(
                "INSERT INTO journal (sequence, session_id, kind, name, wall_ns, "
                "mono_ns, turn_id, data) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    r.get("sequence"),
                    r.get("session_id"),
                    r.get("kind", "event"),
                    r.get("name"),
                    r.get("wall_ns"),
                    r.get("mono_ns"),
                    r.get("turn_id"),
                    json.dumps(r.get("data", {})),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_bundles_list_empty(cli: CliRunner, tmp_path: Path) -> None:
    # Pointing ``--path`` at an empty dir reports no bundles and exits 0.
    result = cli.invoke(app, ["bundles", "list", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "No bundles found" in result.stderr


def test_bundles_list_empty_renders_bracketed_path_literally(
    cli: CliRunner, tmp_path: Path
) -> None:
    scan_path = tmp_path / "recordings[red]"
    scan_path.mkdir()

    result = cli.invoke(app, ["bundles", "list", "--path", str(scan_path)])

    assert result.exit_code == 0
    assert "recordings[red]" in result.stderr


def test_bundles_list_finds_recordings(cli: CliRunner, tmp_path: Path) -> None:
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    _make_bundle(recordings / "sess-a.zip", [{"sequence": 1, "name": "TurnStarted"}])
    _make_bundle(recordings / "sess-b.bundle", [{"sequence": 1, "name": "TurnStarted"}])

    result = cli.invoke(app, ["bundles", "list", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "sess-a.zip" in result.stdout
    assert "sess-b.bundle" in result.stdout


def test_bundles_list_json(cli: CliRunner, tmp_path: Path) -> None:
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    _make_bundle(recordings / "one.zip", [{"sequence": 1, "name": "TurnStarted"}])

    result = cli.invoke(app, ["bundles", "list", "--path", str(tmp_path), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "bundles_list"
    assert len(payload["bundles"]) == 1
    assert payload["bundles"][0]["path"].endswith("one.zip")


def test_bundles_show_summary(cli: CliRunner, tmp_path: Path) -> None:
    # Regression: drive the fixture through the real ``export_debug_bundle``
    # serialization so the journal records carry the production shape — the
    # timestamp nested under ``timing.wall_ns`` and tool calls recorded under
    # the snake_case name ``tool_call_started`` (not the CamelCase event
    # class name). The summary must surface duration and tool_calls correctly.
    records = [
        JournalRecord(
            sequence=1,
            session_id="sess-xyz",
            name="turn_started",
            turn_id="t1",
            timing=TimingInfo(wall_ns=1_000_000_000),
        ),
        JournalRecord(
            sequence=2,
            session_id="sess-xyz",
            name="stt_final",
            turn_id="t1",
            timing=TimingInfo(wall_ns=1_100_000_000),
            data={"text": "hi"},
        ),
        JournalRecord(
            sequence=3,
            session_id="sess-xyz",
            name="tool_call_started",
            turn_id="t1",
            timing=TimingInfo(wall_ns=1_200_000_000),
            data={"tool_name": "calc"},
        ),
        JournalRecord(
            sequence=4,
            session_id="sess-xyz",
            name="error",
            turn_id="t1",
            timing=TimingInfo(wall_ns=1_300_000_000),
            error=ErrorInfo(type="BoomError", message="kaboom"),
        ),
        JournalRecord(
            sequence=5,
            session_id="sess-xyz",
            name="turn_ended",
            turn_id="t1",
            timing=TimingInfo(wall_ns=1_400_000_000),
        ),
    ]
    bundle = tmp_path / "demo.zip"
    export_debug_bundle(_FakeSession(records=records), bundle)

    result = cli.invoke(app, ["bundles", "show", str(bundle), "--json"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["session_id"] == "sess-xyz"
    # duration_ms = (last - first) / 1e6 = 400.
    assert payload["duration_ms"] == pytest.approx(400.0)
    assert payload["tool_calls"] == 1
    assert payload["errors"] == 1

    human = cli.invoke(app, ["bundles", "show", str(bundle)])
    assert human.exit_code == 0, human.stderr
    assert "sess-xyz" in human.stdout
    # duration_ms = (last - first) / 1e6 = 400 → "400.0ms"
    assert "400.0ms" in human.stdout


def test_bundles_show_json(cli: CliRunner, tmp_path: Path) -> None:
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

    result = cli.invoke(app, ["bundles", "show", str(bundle), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["command"] == "bundles_show"
    assert payload["session_id"] == "sess-xyz"
    assert payload["records"] == 1
    assert payload["turns"] == 1
    assert payload["replay_entry_points"][0]["checkpoint_id"] == "cp_7"


def test_bundles_show_renders_bracketed_summary_values_literally(
    cli: CliRunner, tmp_path: Path
) -> None:
    bundle = tmp_path / "demo[red].zip"
    _make_bundle(
        bundle,
        [
            {
                "sequence": 1,
                "name": "TurnStarted",
                "turn_id": "t1",
                "session_id": "sess[red]",
            }
        ],
        provider_versions={"stt[red]": "provider[red]"},
    )

    result = cli.invoke(app, ["bundles", "show", str(bundle)])

    assert result.exit_code == 0, result.stderr
    assert "demo[red].zip" in result.stderr
    assert "sess[red]" in result.stdout
    assert "stt[red]=provider[red]" in result.stdout


def test_bundles_export_context_pack(cli: CliRunner, tmp_path: Path) -> None:
    bundle = tmp_path / "demo.zip"
    output = tmp_path / "pack"
    _make_bundle(
        bundle,
        [
            {
                "sequence": 1,
                "kind": "event",
                "name": "stt_final",
                "turn_id": "t1",
                "session_id": "sess-xyz",
                "data": {
                    "stage": "stt",
                    "transcript": "customer said account token tok-secretvalue123456",
                    "text": "raw caller text",
                    "api_key": "sk-secretvalue123456",
                },
            },
            {
                "sequence": 2,
                "kind": "event",
                "name": "error",
                "turn_id": "t1",
                "session_id": "sess-xyz",
                "data": {"stage": "agent"},
                "error": {
                    "type": "ProviderError",
                    "message": "Authorization: Bearer tok-secretvalue123456 failed",
                    "traceback": 'File "/home/yi/project/app.py", line 7, in run',
                },
            },
        ],
    )

    result = cli.invoke(
        app,
        ["bundles", "export", str(bundle), "--for", "claude-code", "--output", str(output)],
    )

    assert result.exit_code == 0, result.stderr
    assert str(output) in result.stdout
    assert (output / "README.md").exists()
    assert (output / "summary.json").exists()
    assert (output / "timeline.md").exists()
    assert (output / "timeline.jsonl").exists()

    summary = json.loads((output / "summary.json").read_text())
    assert summary["target"] == "claude-code"
    assert summary["redaction"]["version"] == 1
    assert summary["redaction"]["applied"] == "production"
    assert summary["summary"]["records"] == 2

    timeline_records = [
        json.loads(line) for line in (output / "timeline.jsonl").read_text().splitlines()
    ]
    assert timeline_records[0]["data"] == {"stage": "stt"}
    assert timeline_records[0]["omitted_data_fields"] == 3
    assert timeline_records[1]["error"]["message"] == "Authorization: [REDACTED_SECRET] failed"
    assert "~" in timeline_records[1]["error"]["traceback"]

    pack_text = "\n".join(path.read_text() for path in output.iterdir() if path.is_file())
    assert "tok-secretvalue123456" not in pack_text
    assert "sk-secretvalue123456" not in pack_text
    assert "customer said" not in pack_text
    assert "raw caller text" not in pack_text


def test_bundles_export_renders_output_path_literally(cli: CliRunner, tmp_path: Path) -> None:
    bundle = tmp_path / "demo.zip"
    output = tmp_path / "pack[red]"
    _make_bundle(bundle, [{"sequence": 1, "name": "TurnStarted", "session_id": "sess-xyz"}])

    result = cli.invoke(app, ["bundles", "export", str(bundle), "--output", str(output)])

    assert result.exit_code == 0, result.stderr
    assert str(output) in result.stdout
    assert (output / "summary.json").exists()


def test_bundles_export_json(cli: CliRunner, tmp_path: Path) -> None:
    bundle = tmp_path / "demo.zip"
    output = tmp_path / "pack"
    _make_bundle(bundle, [{"sequence": 1, "name": "TurnStarted", "session_id": "sess-xyz"}])

    result = cli.invoke(
        app,
        ["bundles", "export", str(bundle), "--output", str(output), "--json"],
    )

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "bundles_export"
    assert payload["output_path"] == str(output)
    assert payload["target"] == "claude-code"
    assert payload["records"] == 1
    assert payload["format_version"] == FORMAT_VERSION
    assert payload["summary"]["records"] == 1
    assert payload["redaction"]["applied"] == "production"
    assert set(payload["files"]) == {"README.md", "summary.json", "timeline.md", "timeline.jsonl"}


def test_bundles_export_refuses_existing_output_without_force(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "demo.zip"
    output = tmp_path / "pack"
    output.mkdir()
    (output / "old.txt").write_text("old")
    _make_bundle(bundle, [{"sequence": 1, "name": "TurnStarted", "session_id": "sess-xyz"}])

    result = cli.invoke(app, ["bundles", "export", str(bundle), "--output", str(output)])

    assert result.exit_code == 101
    assert "already exists" in result.stderr
    assert (output / "old.txt").exists()

    forced = cli.invoke(
        app,
        ["bundles", "export", str(bundle), "--output", str(output), "--force"],
    )
    assert forced.exit_code == 0, forced.stderr
    assert not (output / "old.txt").exists()
    assert (output / "summary.json").exists()


def test_bundles_export_refuses_existing_output_json_envelope(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "demo.zip"
    output = tmp_path / "pack"
    output.mkdir()
    (output / "old.txt").write_text("old")
    _make_bundle(bundle, [{"sequence": 1, "name": "TurnStarted", "session_id": "sess-xyz"}])

    result = cli.invoke(
        app,
        ["bundles", "export", str(bundle), "--output", str(output), "--json"],
    )

    assert result.exit_code == 101
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "bundles_export"
    assert payload["status"] == "error"
    assert payload["exit_code"] == 101
    assert payload["output_path"] == str(output)
    assert "already exists" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload
    assert (output / "old.txt").exists()


def test_bundles_export_rejects_unknown_target(cli: CliRunner, tmp_path: Path) -> None:
    bundle = tmp_path / "demo.zip"
    _make_bundle(bundle, [{"sequence": 1, "name": "TurnStarted", "session_id": "sess-xyz"}])

    result = cli.invoke(app, ["bundles", "export", str(bundle), "--for", "raw"])

    assert result.exit_code == 2
    assert "claude-code, cursor, codex" in result.stderr


def test_bundles_export_rejects_unknown_target_json_envelope(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "demo.zip"
    _make_bundle(bundle, [{"sequence": 1, "name": "TurnStarted", "session_id": "sess-xyz"}])

    result = cli.invoke(app, ["bundles", "export", str(bundle), "--for", "raw", "--json"])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "bundles_export"
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert "claude-code, cursor, codex" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_inspect_alias_matches_bundles_show(cli: CliRunner, tmp_path: Path) -> None:
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
    assert inspect.exit_code == 0
    assert json.loads(inspect.stdout) == json.loads(show.stdout)


def test_replay_bundle_json_summary(cli: CliRunner, tmp_path: Path) -> None:
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

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["command"] == "replay"
    assert payload["fidelity_requested"] == "artifact"
    assert payload["fidelity_effective"] == "artifact"
    assert payload["frames"] == 2
    assert payload["stages"] == ["stt"]
    assert payload["side_effecting"] is False
    assert payload["tool_policy"] == "deny"

    human = cli.invoke(app, ["replay", str(bundle)])
    assert human.exit_code == 0, human.stderr
    assert "Replay" in human.stderr
    assert "artifact" in human.stdout
    assert "frames" in human.stdout


def test_replay_summary_renders_bracketed_stage_and_tool_names_literally(
    cli: CliRunner, tmp_path: Path
) -> None:
    bundle = tmp_path / "replay[red].zip"
    _make_bundle(
        bundle,
        [
            {
                "sequence": 1,
                "kind": "event",
                "name": "stage_start",
                "turn_id": "t1",
                "session_id": "sess-xyz",
                "data": {"stage": "agent[red]"},
            },
            {
                "sequence": 2,
                "kind": "framework_transition",
                "name": "tool_call_started",
                "turn_id": "t1",
                "session_id": "sess-xyz",
                "data": {"stage": "agent[red]", "tool_name": "lookup[red]", "call_id": "c1"},
            },
        ],
    )

    result = cli.invoke(app, ["replay", str(bundle), "--tool-policy", "stub"])

    assert result.exit_code == 0, result.stderr
    assert "replay[red].zip" in result.stderr
    assert "lookup[red](c1)" in result.stdout


def test_replay_bundle_blocks_tool_side_effects_by_default(
    cli: CliRunner,
    tmp_path: Path,
) -> None:
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

    blocked = cli.invoke(app, ["replay", str(bundle)])

    assert blocked.exit_code == 6
    assert "Replay blocked" in blocked.stderr
    assert "get_weather(c1)" in blocked.stderr
    assert "--tool-policy stub" in blocked.stderr

    blocked_json = cli.invoke(app, ["replay", str(bundle), "--json"])
    assert blocked_json.exit_code == 6
    payload = json.loads(blocked_json.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "replay"
    assert payload["status"] == "error"
    assert payload["exit_code"] == 6
    assert payload["path"] == str(bundle)
    assert "Replay blocked" in payload["message"]
    assert "get_weather(c1)" in payload["message"]
    assert "--tool-policy stub" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload

    stubbed = cli.invoke(app, ["replay", str(bundle), "--tool-policy", "stub", "--json"])
    assert stubbed.exit_code == 0, stubbed.stderr
    payload = json.loads(stubbed.stdout)
    assert payload["stubbed_tool_calls"] == ["get_weather(c1)"]
    assert payload["side_effecting"] is False


def test_bundles_show_sqlite_crash_dump(cli: CliRunner, tmp_path: Path) -> None:
    """A ``.sqlite`` crash dump from ``discover_bundles`` is inspectable.

    Regression: ``discover_bundles`` lists crash-dump SQLite files, so
    ``inspect``/``show`` must route them through ``from_partial_journal``
    instead of failing with a corrupt-ZIP exit 5.
    """
    crash_dir = tmp_path / "crash-dumps"
    crash_dir.mkdir()
    crash = crash_dir / "sess-crash.sqlite"
    _make_crash_dump(
        crash,
        [
            {
                "sequence": 1,
                "name": "TurnStarted",
                "turn_id": "t1",
                "session_id": "sess-crash",
            }
        ],
    )

    show = cli.invoke(app, ["bundles", "show", str(crash), "--json"])
    inspect = cli.invoke(app, ["inspect", str(crash), "--json"])
    assert show.exit_code == 0
    assert inspect.exit_code == 0
    assert json.loads(inspect.stdout) == json.loads(show.stdout)
    payload = json.loads(show.stdout)
    assert payload["command"] == "bundles_show"
    assert payload["session_id"] == "sess-crash"
    assert payload["turns"] == 1


def test_inspect_locked_sqlite_journal_message(
    cli: CliRunner,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = tmp_path / "live.sqlite"
    journal.touch()

    def raise_locked(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("easycat.debug.bundle.sqlite3.connect", raise_locked)

    result = cli.invoke(app, ["inspect", str(journal)])
    assert result.exit_code == 5
    assert "currently in use" in result.stderr
    assert "Stop the session before inspecting it" in result.stderr
    assert "session.export_debug_bundle(...)" in result.stderr
    assert "Bundle corrupt or unreadable" not in result.stderr
    assert "bundles list" not in result.stderr


def test_bundles_show_missing_path(cli: CliRunner, tmp_path: Path) -> None:
    missing = tmp_path / "nope.zip"
    result = cli.invoke(app, ["bundles", "show", str(missing)])
    assert result.exit_code == 5
    assert "not found" in result.stderr


def test_bundles_show_missing_path_json_envelope(cli: CliRunner, tmp_path: Path) -> None:
    missing = tmp_path / "nope.zip"
    result = cli.invoke(app, ["bundles", "show", str(missing), "--json"])

    assert result.exit_code == 5
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert payload["command"] == "bundles_show"
    assert payload["status"] == "error"
    assert payload["exit_code"] == 5
    assert payload["path"] == str(missing)
    assert "not found" in payload["message"]
    assert "code" not in payload
    assert "fix" not in payload
    assert "context" not in payload


def test_bundles_show_corrupt(cli: CliRunner, tmp_path: Path) -> None:
    """A non-zip file should exit 5 with a clear message."""
    corrupt = tmp_path / "not-a-zip.zip"
    corrupt.write_text("definitely not a zip archive")
    result = cli.invoke(app, ["bundles", "show", str(corrupt)])
    assert result.exit_code == 5
    assert "corrupt or unreadable" in result.stderr


def test_bundles_show_corrupt_member(cli: CliRunner, tmp_path: Path) -> None:
    """A ZIP with a damaged member CRC should use the bundle error path."""
    corrupt = tmp_path / "bad-member.zip"
    journal_payload = json.dumps({"sequence": 1, "name": "TurnStarted"}).encode()
    with zipfile.ZipFile(corrupt, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr(
            "manifest.json",
            json.dumps({"format_version": FORMAT_VERSION}),
        )
        zf.writestr("journal.ndjson", journal_payload)

    raw = corrupt.read_bytes()
    assert b"TurnStarted" in raw
    corrupt.write_bytes(raw.replace(b"TurnStarted", b"XurnStarted", 1))

    result = cli.invoke(app, ["bundles", "show", str(corrupt)])
    assert result.exit_code == 5
    assert "corrupt or unreadable" in result.stderr


def test_bundles_show_missing_journal(cli: CliRunner, tmp_path: Path) -> None:
    corrupt = tmp_path / "missing-journal.zip"
    with zipfile.ZipFile(corrupt, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({"format_version": FORMAT_VERSION}))

    result = cli.invoke(app, ["bundles", "show", str(corrupt)])
    assert result.exit_code == 5
    assert "corrupt or unreadable" in result.stderr


def test_bundles_show_newer_version(cli: CliRunner, tmp_path: Path) -> None:
    """A forward-version bundle gets an 'upgrade easycat' message, not 'corrupt'."""
    newer = tmp_path / "from-the-future.zip"
    with zipfile.ZipFile(newer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps({"format_version": FORMAT_VERSION + 1}))
        zf.writestr("journal.ndjson", "")

    result = cli.invoke(app, ["bundles", "show", str(newer)])
    assert result.exit_code == 6
    assert "newer easycat" in result.stderr
    assert "upgrade easycat" in result.stderr
    assert "corrupt or unreadable" not in result.stderr


@pytest.mark.parametrize(
    ("num_bytes", "expected"),
    [
        (0, "0B"),
        (512, "512B"),
        (1023, "1023B"),
        (1024, "1.0KB"),
        (2048, "2.0KB"),
        (1024 * 1024, "1.0MB"),
        (5 * 1024 * 1024, "5.0MB"),
        (3 * 1024 * 1024 * 1024, "3.0GB"),
        (2048 * 1024 * 1024 * 1024, "2048.0GB"),
    ],
)
def test_format_size(num_bytes: int, expected: str) -> None:
    assert _format_size(num_bytes) == expected
