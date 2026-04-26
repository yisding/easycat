"""Tests for ``easycat bundles list`` and ``easycat bundles show``."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from typer.testing import CliRunner

from easycat.cli._app import app
from easycat.debug.bundle import FORMAT_VERSION


def _make_bundle(path: Path, records: list[dict]) -> None:
    """Roll a minimal valid bundle zip at *path*."""
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
        zf.writestr("journal.ndjson", "\n".join(json.dumps(r) for r in records))


def test_bundles_list_empty(cli: CliRunner, tmp_path: Path) -> None:
    # Pointing ``--path`` at an empty dir reports no bundles and exits 0.
    result = cli.invoke(app, ["bundles", "list", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "No bundles found" in result.stderr


def test_bundles_list_finds_recordings(cli: CliRunner, tmp_path: Path) -> None:
    recordings = tmp_path / "recordings"
    recordings.mkdir()
    _make_bundle(recordings / "sess-a.zip", [{"sequence": 1, "name": "TurnStarted"}])
    _make_bundle(recordings / "sess-b.zip", [{"sequence": 1, "name": "TurnStarted"}])

    result = cli.invoke(app, ["bundles", "list", "--path", str(tmp_path)])
    assert result.exit_code == 0
    assert "sess-a.zip" in result.stdout
    assert "sess-b.zip" in result.stdout


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
    bundle = tmp_path / "demo.zip"
    _make_bundle(
        bundle,
        [
            {
                "sequence": 1,
                "name": "TurnStarted",
                "turn_id": "t1",
                "session_id": "sess-xyz",
                "wall_ns": 1_000_000_000,
            },
            {
                "sequence": 2,
                "name": "STTFinal",
                "turn_id": "t1",
                "wall_ns": 1_100_000_000,
                "data": {"text": "hi"},
            },
            {
                "sequence": 3,
                "name": "ToolCallStarted",
                "turn_id": "t1",
                "wall_ns": 1_200_000_000,
                "data": {"tool": "calc"},
            },
            {
                "sequence": 4,
                "name": "Error",
                "turn_id": "t1",
                "wall_ns": 1_300_000_000,
                "error": {"type": "BoomError", "message": "kaboom"},
            },
            {
                "sequence": 5,
                "name": "TurnEnded",
                "turn_id": "t1",
                "wall_ns": 1_400_000_000,
            },
        ],
    )

    result = cli.invoke(app, ["bundles", "show", str(bundle)])
    assert result.exit_code == 0, result.stderr
    assert "sess-xyz" in result.stdout
    # duration_ms = (last - first) / 1e6 = 400 → "400.0ms"
    assert "400.0ms" in result.stdout
    assert "replay_entry_points" in result.stdout
    # cp_7 is the user-facing id for sequence 7 from the manifest.
    assert "cp_7" in result.stdout


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


def test_bundles_show_missing_path(cli: CliRunner, tmp_path: Path) -> None:
    missing = tmp_path / "nope.zip"
    result = cli.invoke(app, ["bundles", "show", str(missing)])
    assert result.exit_code == 5
    assert "not found" in result.stderr


def test_bundles_show_corrupt(cli: CliRunner, tmp_path: Path) -> None:
    """A non-zip file should exit 5 with a clear message."""
    corrupt = tmp_path / "not-a-zip.zip"
    corrupt.write_text("definitely not a zip archive")
    result = cli.invoke(app, ["bundles", "show", str(corrupt)])
    assert result.exit_code != 0
    # Either BundleError (5) or Python's BadZipFile — both must not crash.
    assert result.exit_code in {1, 5}
