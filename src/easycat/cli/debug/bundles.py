"""``easycat bundles`` — journal-bundle inspection commands.

Two commands land here:

``bundles list``
    Print every bundle found in ``.easycat/recordings`` and
    ``.easycat/crash-dumps`` (or an explicit ``--path`` directory) with
    size and modification time. This mirrors the UX
    ``peripheral-cli.md`` promises: ``easycat bundles list`` is the
    fastest way to answer "what got recorded last night?" without
    opening a Python REPL.

``bundles show <path>`` / ``inspect <path>``
    Summarize a single bundle: session id, turn count, error count,
    provider versions, first + last record timestamps. Deliberately
    avoids printing raw journal lines — that's what
    ``bundles export --for=claude-code`` (M3 follow-up) is for.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.table import Table

from easycat.cli._errors import cli_command
from easycat.cli._output import emit_json, json_envelope, stderr_console, stdout_console
from easycat.debug.bundle import (
    BundleError,
    RunBundle,
    checkpoint_id,
    discover_bundles,
)

bundles_app = typer.Typer(
    name="bundles",
    help="Inspect captured debug bundles.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ── Helpers ──────────────────────────────────────────────────────


def _format_size(num_bytes: int) -> str:
    """Human-friendly byte count.  Keep the format stable for scripting."""
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024 or unit == "GB":
            return f"{num_bytes:.0f}{unit}" if unit == "B" else f"{num_bytes / 1024:.1f}{unit}"
        num_bytes //= 1024
    return f"{num_bytes}B"


def _format_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _summarise_bundle(bundle: RunBundle) -> dict[str, object]:
    """Collect the high-signal fields we surface in ``bundles show``."""
    turns: set[str] = set()
    errors = 0
    session_id = ""
    first_wall_ns: int | None = None
    last_wall_ns: int | None = None
    tool_calls = 0
    record_count = 0

    for record in bundle.records():
        record_count += 1
        if not session_id and record.get("session_id"):
            session_id = str(record["session_id"])
        turn_id = record.get("turn_id")
        if turn_id:
            turns.add(str(turn_id))
        wall_ns = record.get("wall_ns")
        if isinstance(wall_ns, int):
            if first_wall_ns is None:
                first_wall_ns = wall_ns
            last_wall_ns = wall_ns
        if record.get("error"):
            errors += 1
        if record.get("name") == "ToolCallStarted":
            tool_calls += 1

    duration_ms: float | None = None
    if first_wall_ns is not None and last_wall_ns is not None:
        duration_ms = (last_wall_ns - first_wall_ns) / 1_000_000

    return {
        "session_id": session_id,
        "turns": len(turns),
        "errors": errors,
        "tool_calls": tool_calls,
        "records": record_count,
        "duration_ms": duration_ms,
        "provider_versions": dict(bundle.manifest.provider_versions),
        "artifact_count": len(bundle.artifact_index),
        "replay_entry_points": [
            {
                "sequence": cp.sequence,
                "checkpoint_id": cp.checkpoint_id,
                "stage": cp.stage,
                "unit_id": cp.unit_id,
            }
            for cp in bundle.replay_entry_points
        ],
    }


# ── `easycat bundles list` ───────────────────────────────────────


@cli_command
def list_bundles(
    path: Path | None = typer.Option(
        None,
        "--path",
        help="Directory to scan (default: ``.easycat``).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """List every bundle under the data directory."""
    data_dir = str(path) if path is not None else None
    bundle_paths = discover_bundles(data_dir=data_dir)

    entries: list[dict[str, object]] = []
    for bundle_path in bundle_paths:
        stat = bundle_path.stat()
        entries.append(
            {
                "path": str(bundle_path),
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
            }
        )

    if json_output:
        emit_json(
            json_envelope(
                "bundles_list",
                bundles=entries,
                scanned=str(path) if path is not None else ".easycat",
            )
        )
        raise typer.Exit(0)

    if not entries:
        scan_target = str(path) if path is not None else ".easycat"
        stderr_console.print(f"No bundles found under [cyan]{scan_target}[/].")
        stderr_console.print(
            "[dim]Use [cyan]EasyCatConfig(record_to=...)[/] or "
            "[cyan]session.export_debug_bundle()[/] to capture one.[/]"
        )
        raise typer.Exit(0)

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("path", no_wrap=False, overflow="fold")
    table.add_column("size", justify="right", no_wrap=True)
    table.add_column("modified", no_wrap=True)
    for entry in entries:
        table.add_row(
            str(entry["path"]),
            _format_size(int(entry["size_bytes"])),
            _format_mtime(float(entry["mtime"])),
        )
    stdout_console.print(table)


def _show_bundle_summary(bundle_path: Path, *, json_output: bool) -> None:
    """Load and render the bundle summary used by all inspect aliases."""
    if not bundle_path.exists():
        stderr_console.print(f"  [red]✗[/] Bundle not found: [red]{bundle_path}[/]")
        raise typer.Exit(5)

    try:
        bundle = RunBundle.load(bundle_path)
    except BundleError as exc:
        stderr_console.print(f"  [red]✗[/] Bundle corrupt or unreadable: {exc}")
        raise typer.Exit(5) from None

    summary = _summarise_bundle(bundle)

    if json_output:
        emit_json(
            json_envelope(
                "bundles_show",
                path=str(bundle_path),
                format_version=bundle.format_version,
                **summary,
            )
        )
        raise typer.Exit(0)

    stderr_console.print(f"[bold]Bundle[/] [cyan]{bundle_path}[/]")
    stderr_console.print()
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    table.add_row("session_id", str(summary["session_id"]) or "[dim](unknown)[/]")
    table.add_row("format_version", str(bundle.format_version))
    table.add_row("records", str(summary["records"]))
    table.add_row("turns", str(summary["turns"]))
    duration = summary["duration_ms"]
    duration_str = f"{float(duration):.1f}ms" if isinstance(duration, float) else "[dim]n/a[/]"
    table.add_row("duration", duration_str)
    table.add_row("tool_calls", str(summary["tool_calls"]))
    errors = int(summary["errors"])
    errors_fmt = f"[red]{errors}[/]" if errors else "0"
    table.add_row("errors", errors_fmt)
    table.add_row("artifacts", str(summary["artifact_count"]))
    entry_points = summary["replay_entry_points"]
    if isinstance(entry_points, list) and entry_points:
        rendered = ", ".join(str(ep["checkpoint_id"]) for ep in entry_points)
        table.add_row("replay_entry_points", rendered)
    else:
        table.add_row("replay_entry_points", "0")
    providers = summary["provider_versions"]
    if isinstance(providers, dict) and providers:
        pv = ", ".join(f"{k}={v}" for k, v in sorted(providers.items()))
        table.add_row("providers", pv)
    stdout_console.print(table)


# ── `easycat bundles show` / `easycat inspect` ───────────────────


@cli_command
def show_bundle(
    bundle_path: Path = typer.Argument(..., help="Path to a ``.zip`` bundle."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Summarise a single bundle: turns, errors, timings, provider versions."""
    _show_bundle_summary(bundle_path, json_output=json_output)


@cli_command
def inspect_bundle(
    bundle_path: Path = typer.Argument(..., help="Path to a ``.zip`` bundle."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Friendly alias for ``easycat bundles show``."""
    _show_bundle_summary(bundle_path, json_output=json_output)


bundles_app.command(name="list", help="List captured bundles under .easycat/.")(list_bundles)
bundles_app.command(name="show", help="Summarise a single bundle.")(show_bundle)


__all__: list[str] = ["bundles_app", "inspect_bundle"]

# Silence "imported but unused" for shared-helper imports that stay in
# the file for parity with other CLI modules.
_ = (json, checkpoint_id)
