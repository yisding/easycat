"""``easycat bundles`` — journal-bundle inspection and replay commands.

Commands that operate directly on recorded bundles and crash journals land here:

Exported bundle files are ZIP archives regardless of whether they use
``.zip``, ``.bundle``, or ``.easycat-bundle``. Crash dumps are instead
``.sqlite`` journals (one per crashed session) and are inspected via the
partial-journal recovery path. The ``--json`` flag controls CLI summary
output; it is not a separate bundle file format.

``bundles list``
    Print every bundle found in ``.easycat/recordings`` and
    ``.easycat/crash-dumps`` (or an explicit ``--path`` directory) with
    size and modification time. ``easycat bundles list`` is the
    fastest way to answer "what got recorded last night?" without
    opening a Python REPL.

``bundles show <path>`` / ``inspect <path>``
    Summarize a single bundle or SQLite journal: session id, turn count,
    error count, provider versions, first + last record timestamps.
    Deliberately avoids printing raw journal lines — that's what
    ``--json`` is for when a machine-readable summary is needed.

``bundles export <path>``
    Write a redacted context pack for coding agents. The pack keeps
    summary metadata and allowlisted event structure, but omits raw
    transcripts, prompts, generated text, tool payloads, and provider
    responses until the full redaction-policy layer lands.

``replay <path>``
    Walk a debug bundle or SQLite journal through ``RunBundle.replay``
    with safe defaults, including artifact fidelity and denied tool
    side effects.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from easycat.cli._errors import cli_command
from easycat.cli._output import (
    emit_command_error,
    emit_json,
    json_envelope,
    stderr_console,
    stdout_console,
)
from easycat.cli.debug._common import (
    _add_annotations_row,
    _format_ms,
    _load_bundle_or_journal,
    _print_wide,
    _summarise_bundle,
)
from easycat.cli.debug.diff import diff_command
from easycat.cli.debug.export import export_bundle
from easycat.cli.debug.follow import follow_journal
from easycat.cli.debug.grep import journal_grep
from easycat.cli.debug.latency import latency_command
from easycat.cli.debug.promote import promote_turn
from easycat.cli.debug.replay import replay_bundle
from easycat.debug._issues import build_issues
from easycat.debug.annotations import load_annotations
from easycat.debug.bundle import (
    RunBundle,
    discover_bundles_with_status,
)

bundles_app = typer.Typer(
    name="bundles",
    help="Inspect captured debug bundles and crash dumps.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

journal_app = typer.Typer(
    name="journal",
    help="Search and tail captured journals and crash dumps.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

debugger_app = typer.Typer(
    name="debugger",
    help="Open the browser debugging UI for bundles and journals.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


# ── Helpers ──────────────────────────────────────────────────────


def _format_size(num_bytes: int) -> str:
    """Human-friendly byte count.  Keep the format stable for scripting."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _format_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")


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
    bundle_paths = discover_bundles_with_status(data_dir=data_dir)

    entries: list[dict[str, Any]] = []
    for bundle_path, status in bundle_paths:
        try:
            stat = bundle_path.stat()
        except OSError:
            # Discovery already skips entries that do not resolve, but the
            # window between it and here is real: a retention sweep or the
            # crash-dump promoter can move a file mid-listing. Skip it rather
            # than replacing the whole table with a traceback (gh 1107).
            continue
        entries.append(
            {
                "path": str(bundle_path),
                "size_bytes": stat.st_size,
                "mtime": stat.st_mtime,
                "status": status,
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
        stderr_console.print(f"No bundles found under [cyan]{escape(scan_target)}[/].")
        stderr_console.print(
            "[dim]Use [cyan]EasyConfig(record_to=...)[/], "
            "[cyan]create_text_session(record_to=...)[/], or "
            "[cyan]session.export_debug_bundle()[/] to capture one.[/]"
        )
        stderr_console.print(
            "[dim]Durable journals need [cyan]debug='full'[/] (opt in; the default "
            "[cyan]debug='light'[/] is in-memory); see "
            "[cyan]easycat explain journal[/] for how recordings are captured.[/]"
        )
        raise typer.Exit(0)

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    # Keep the path on a single, un-clipped line so it stays greppable: with
    # the extra ``status`` column an 80-col console would otherwise fold (and
    # interleave the other columns' cells between the path fragments) or clip
    # it with an ellipsis.  Render through a console sized to the longest row
    # so a long absolute path is never truncated.
    table.add_column("path", no_wrap=True, overflow="ignore")
    table.add_column("size", justify="right", no_wrap=True)
    table.add_column("modified", no_wrap=True)
    table.add_column("status", no_wrap=True)
    longest_path = max((len(str(entry["path"])) for entry in entries), default=0)
    longest_status = max((len(str(entry["status"])) for entry in entries), default=0)
    # path + size(~8) + modified(~20) + status + inter-column padding/margin.
    render_width = max(stdout_console.width, longest_path + longest_status + 40)
    for entry in entries:
        status = str(entry["status"])
        # Flag crashed-but-unswept journals in red so they stand out.
        status_fmt = escape(status)
        if status.startswith("crashed"):
            status_fmt = f"[red]{status_fmt}[/]"
        table.add_row(
            escape(str(entry["path"])),
            _format_size(int(entry["size_bytes"])),
            _format_mtime(float(entry["mtime"])),
            status_fmt,
        )
    _print_wide(table, render_width)


def _build_issue_summary(bundle: RunBundle) -> dict[str, Any]:
    """Compute the severity-ranked issue rollup for *bundle*."""
    return build_issues(list(bundle.records()), artifact_resolver=bundle.artifact_blobs.get)


def _add_journal_rows(table: Table, summary: Mapping[str, Any]) -> None:
    table.add_row("records", str(summary["records"]))
    dropped_records = int(summary["journal_dropped_records"])
    table.add_row(
        "journal_dropped",
        f"[red]{dropped_records}[/]" if dropped_records else "0",
    )


def _show_bundle_summary(bundle_path: Path, *, json_output: bool, issues: bool = False) -> None:
    """Load and render the bundle or SQLite journal summary used by all aliases."""
    bundle = _load_bundle_or_journal(
        bundle_path,
        command="bundles_show",
        json_output=json_output,
    )
    # Reviewer verdicts live in a sidecar next to the bundle, never inside
    # the (read-only) journal; a missing/corrupt sidecar loads as empty.
    annotations = load_annotations(bundle_path)
    summary: dict[str, Any] = _summarise_bundle(bundle, annotations=annotations)

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

    stderr_console.print(f"[bold]Bundle[/] [cyan]{escape(str(bundle_path))}[/]")
    stderr_console.print()
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    table.add_row("session_id", escape(str(summary["session_id"])) or "[dim](unknown)[/]")
    table.add_row("format_version", str(bundle.format_version))
    _add_journal_rows(table, summary)
    table.add_row("turns", str(summary["turn_count"]))
    duration = summary["duration_ms"]
    table.add_row(
        "duration",
        f"{float(duration):.1f}ms" if isinstance(duration, float) else "[dim]n/a[/]",
    )
    table.add_row("tool_calls", str(summary["tool_calls"]))
    errors = int(summary["errors"])
    errors_fmt = f"[red]{errors}[/]" if errors else "0"
    table.add_row("errors", errors_fmt)
    if errors:
        error_type = summary["error_type"]
        if error_type:
            table.add_row("error_type", f"[red]{escape(str(error_type))}[/]")
        failing_turn_id = summary["failing_turn_id"]
        if failing_turn_id:
            table.add_row("failing_turn_id", escape(str(failing_turn_id)))
    _add_annotations_row(table, summary["annotations"])
    table.add_row("artifacts", str(summary["artifact_count"]))
    entry_points = summary["replay_entry_points"]
    if isinstance(entry_points, list) and entry_points:
        rendered = escape(", ".join(str(ep["checkpoint_id"]) for ep in entry_points))
        table.add_row("replay_entry_points", rendered)
    else:
        table.add_row("replay_entry_points", "0")
    providers = summary["provider_versions"]
    if isinstance(providers, dict) and providers:
        pv = escape(", ".join(f"{k}={v}" for k, v in sorted(providers.items())))
        table.add_row("providers", pv)
    stdout_console.print(table)

    turns = summary["turns"]
    if isinstance(turns, list) and turns:
        # The waterfall now carries milestone, barge-in, and interrupt columns;
        # render wide so the spans column is never clipped on an 80-col stdout.
        _print_wide(_turn_waterfall_table(turns), max(stdout_console.width, 120))

    _print_troubleshooting_pointer(errors, turns)

    if issues:
        report = summary["issues"]
        if isinstance(report, Mapping):
            stdout_console.print(_issues_table(report))


def _print_troubleshooting_pointer(errors: int, turns: object) -> None:
    """Print a static symptom-first pointer when a call looks problematic.

    Surfaces a one-line route to ``easycat explain troubleshooting`` when the
    bundle carries errors or any interruption — not full card rendering (that
    is ``--issues``).  Stays quiet on clean bundles.
    """
    interruptions = 0
    if isinstance(turns, list):
        interruptions = sum(int(turn.get("interruption_count", 0) or 0) for turn in turns)
    if not (errors or interruptions):
        return
    stdout_console.print()
    stdout_console.print(
        "[dim]Likely issues:[/] run [cyan]easycat explain troubleshooting[/] to route by symptom."
    )


_ISSUE_SEVERITY_STYLE = {"error": "red", "warning": "yellow", "info": "cyan"}


def _issues_table(report: Mapping[str, Any]) -> Table:
    """Render the severity-ranked issue rollup for ``--issues``.

    One row per finding: severity, code, the turn/sequence it points at, and
    a short title.  ``report`` is the ``build_issues`` envelope, so an empty
    journal renders an all-clear table.
    """
    found = report.get("issues") or []
    summary = report.get("summary") or {}
    counts = ", ".join(f"{summary.get(sev, 0)} {sev}" for sev in ("error", "warning", "info"))
    table = Table(
        title=f"Issues — {counts}",
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 1),
        title_justify="left",
    )
    table.add_column("severity", no_wrap=True)
    table.add_column("code", no_wrap=True)
    table.add_column("turn", no_wrap=True, overflow="fold")
    table.add_column("seq", justify="right", no_wrap=True)
    table.add_column("title", overflow="fold")
    if not found:
        table.add_row("[green]ok[/]", "—", "—", "—", "No issues detected.")
        return table
    for issue in found:
        severity = str(issue.get("severity") or "info")
        style = _ISSUE_SEVERITY_STYLE.get(severity, "cyan")
        turn_id = issue.get("turn_id")
        seq = issue.get("sequence")
        table.add_row(
            f"[{style}]{escape(severity)}[/]",
            escape(str(issue.get("code") or "")),
            escape(str(turn_id)) if turn_id else "[dim]-[/]",
            str(seq) if isinstance(seq, int) else "[dim]-[/]",
            escape(str(issue.get("title") or "")),
        )
    return table


def _turn_waterfall_table(turns: list[dict[str, Any]]) -> Table:
    """Render the per-turn latency waterfall for ``bundles show``/``inspect``.

    One row per turn: total wall time, the milestone deltas (VAD endpoint
    → STT final → agent request → agent first token → TTS first byte), and
    the per-stage spans as ``stage duration@offset``.  ``docs/latency.md``
    explains how to read the numbers and which defaults to tune.
    """
    table = Table(
        title="Per-turn latency (ms) — see docs/latency.md",
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 1),
        title_justify="left",
    )
    table.add_column("turn", no_wrap=True, overflow="fold")
    table.add_column("wall", justify="right", no_wrap=True)
    table.add_column("vad→stt", justify="right", no_wrap=True)
    table.add_column("stt→req", justify="right", no_wrap=True)
    table.add_column("req→token", justify="right", no_wrap=True)
    table.add_column("agent→tts", justify="right", no_wrap=True)
    table.add_column("vad→tts", justify="right", no_wrap=True)
    table.add_column("barge-in", justify="right", no_wrap=True)
    table.add_column("interrupts", justify="right", no_wrap=True)
    table.add_column("spans (dur@off)", overflow="fold")
    for turn in turns:
        milestones = turn.get("milestones") or {}
        spans = ", ".join(
            f"{span['stage']} {_format_ms(span['duration_ms'])}@{_format_ms(span['offset_ms'])}"
            for span in turn.get("spans", ())
        )
        table.add_row(
            escape(str(turn.get("turn_id", ""))),
            _format_ms(turn.get("wall_ms")),
            _format_ms(milestones.get("vad_endpoint_to_stt_final_ms")),
            _format_ms(milestones.get("stt_final_to_agent_request_ms")),
            _format_ms(milestones.get("agent_request_to_first_token_ms")),
            _format_ms(milestones.get("agent_first_token_to_tts_first_byte_ms")),
            _format_ms(milestones.get("vad_endpoint_to_tts_first_byte_ms")),
            _format_ms(milestones.get("user_speech_start_to_bot_stopped_ms")),
            str(turn.get("interruption_count", 0)),
            escape(spans) if spans else "[dim](no stage spans)[/]",
        )
    return table


# ── `easycat debugger serve` ─────────────────────────────────────


@debugger_app.command("serve")
@cli_command
def serve_debugger_ui(
    bundle_path: Path = typer.Argument(
        ...,
        help=(
            "Path to a ZIP bundle archive (``.zip``, ``.bundle``, or "
            "``.easycat-bundle``) or a ``.sqlite`` journal."
        ),
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Host to bind. Non-loopback requires --allow-remote.",
    ),
    port: int = typer.Option(8765, "--port", help="Port to bind."),
    open_browser: bool = typer.Option(
        True,
        "--open-browser/--no-open-browser",
        help="Open the debugger URL in a browser after the server starts.",
    ),
    allow_remote: bool = typer.Option(
        False,
        "--allow-remote",
        help="Allow binding to a non-loopback host. The debugger has no auth.",
    ),
) -> None:
    """Serve the first-class browser debugger for a bundle or SQLite journal."""
    bundle = _load_bundle_or_journal(
        bundle_path,
        command="debugger_serve",
        json_output=False,
    )
    from easycat.debugger import serve_run_bundle

    url = f"http://{host}:{port}"
    stderr_console.print(
        f"[bold]EasyCat debugger[/] serving [cyan]{escape(str(bundle_path))}[/] at "
        f"[cyan]{escape(url)}[/]"
    )
    stderr_console.print(
        "[yellow]Journals can contain transcripts, audio, prompts, and tool payloads; "
        "keep this server on loopback unless you add your own network controls.[/]"
    )
    try:
        serve_run_bundle(
            bundle,
            label=bundle_path.name,
            # SQLite journals are pathless ``RunBundle`` views for debugger
            # purposes; only an immutable ZIP bundle has a sidecar surface.
            annotate_path=bundle_path if bundle_path.suffix != ".sqlite" else None,
            host=host,
            port=port,
            open_browser=open_browser,
            allow_remote=allow_remote,
        )
    except RuntimeError as exc:
        emit_command_error(
            "debugger_serve",
            str(exc),
            json_output=False,
            exit_code=2,
            path=str(bundle_path),
        )
        raise typer.Exit(2) from None


# ── `easycat bundles show` / `easycat inspect` ───────────────────


@cli_command
def show_bundle(
    bundle_path: Path = typer.Argument(
        ...,
        help=(
            "Path to a ZIP bundle archive (``.zip``, ``.bundle``, or "
            "``.easycat-bundle``) or a ``.sqlite`` journal."
        ),
    ),
    issues: bool = typer.Option(
        False,
        "--issues",
        help="Render the severity-ranked issue rollup (errors, slow milestones, …).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Summarise a debug bundle or SQLite journal."""
    _show_bundle_summary(bundle_path, json_output=json_output, issues=issues)


@cli_command
def inspect_bundle(
    bundle_path: Path = typer.Argument(
        ...,
        help=(
            "Path to a ZIP bundle archive (``.zip``, ``.bundle``, or "
            "``.easycat-bundle``) or a ``.sqlite`` journal."
        ),
    ),
    issues: bool = typer.Option(
        False,
        "--issues",
        help="Render the severity-ranked issue rollup (errors, slow milestones, …).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Friendly alias for ``easycat bundles show`` for bundles and SQLite journals."""
    _show_bundle_summary(bundle_path, json_output=json_output, issues=issues)


journal_app.command(
    name="follow", help="Live-tail a SQLite journal as it grows, redacting every line."
)(follow_journal)


journal_app.command(
    name="grep", help="Full-text search a journal or bundle, redacting every match."
)(journal_grep)


journal_app.command(
    name="promote",
    help="Promote one turn into a replayable, self-contained regression bundle.",
)(promote_turn)


bundles_app.command(name="list", help="List captured bundles and crash dumps under .easycat/.")(
    list_bundles
)
bundles_app.command(name="show", help="Summarise a debug bundle or SQLite journal.")(show_bundle)
bundles_app.command(name="export", help="Write a redacted context pack for a coding agent.")(
    export_bundle
)


__all__: list[str] = [
    "bundles_app",
    "diff_command",
    "export_bundle",
    "follow_journal",
    "inspect_bundle",
    "journal_app",
    "journal_grep",
    "latency_command",
    "promote_turn",
    "replay_bundle",
]
