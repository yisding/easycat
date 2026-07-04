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
    size and modification time. This mirrors the UX
    ``peripheral-cli.md`` promises: ``easycat bundles list`` is the
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

import asyncio
import json
import keyword
import re
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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
    success,
    warn,
)
from easycat.cli.debug._common import (
    _add_annotations_row,
    _format_ms,
    _load_bundle_or_journal,
    _print_wide,
    _record_detail,
    _record_stage,
    _summarise_bundle,
)
from easycat.cli.debug.diff import diff_command
from easycat.cli.debug.export import export_bundle
from easycat.cli.debug.latency import latency_command
from easycat.cli.debug.replay import replay_bundle
from easycat.debug._issues import build_issues
from easycat.debug._turn_timeline import safe_turn_id
from easycat.debug.annotations import load_annotations
from easycat.debug.bundle import (
    BundleError,
    BundleValidationError,
    RunBundle,
    discover_bundles_with_status,
)
from easycat.debug.export import slice_bundle_by_turn
from easycat.runtime.replay import (
    ReplayError,
    ReplayFidelity,
    ReplaySideEffectBlocked,
    ReplaySpec,
    ToolReplayPolicy,
)
from easycat.validation.redaction import (
    REDACTED_TRANSCRIPT,
    contains_unredacted_sensitive_text,
    redact_text,
    redact_value,
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

    entries: list[dict[str, object]] = []
    for bundle_path, status in bundle_paths:
        stat = bundle_path.stat()
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
            "[dim]Durable journals need [cyan]debug='full'[/] (the default); see "
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
    summary = _summarise_bundle(bundle, annotations=annotations)

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
    table.add_row("records", str(summary["records"]))
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


# ── `easycat journal promote` ────────────────────────────────────


def _replay_signature(bundle: RunBundle) -> tuple[tuple[int, str, str | None], ...]:
    """Replay *bundle* at ARTIFACT/DENY/fast and return a determinism key.

    The key is the ``(sequence, name, output_ref)`` tuple of every frame —
    stable across runs of a deterministic turn, so two equal keys prove the
    slice replays the same way twice.  Tool side effects are denied and
    timing is masked (``fast``) so a barge-in or a non-committable tool call
    surfaces as a raised exception rather than a silent non-match.
    """
    result = bundle.replay(
        ReplaySpec(
            fidelity=ReplayFidelity.ARTIFACT,
            tool_policy=ToolReplayPolicy.DENY,
            timing="fast",
        )
    )
    return tuple((f.sequence, f.name, f.output_ref) for f in result.frames)


def _promote_stub_test_name(turn_id: str) -> str:
    """Return a Python identifier-safe pytest name suffix for a turn id."""
    suffix = re.sub(r"[^0-9A-Za-z_]+", "_", turn_id).strip("_")
    if not suffix:
        suffix = "turn"
    if suffix[0].isdigit() or keyword.iskeyword(suffix):
        suffix = f"turn_{suffix}"
    return suffix


def _promote_test_stub(*, bundle_name: str, turn_id: str, expected: str | None) -> str:
    """Render a copy-pasteable pytest regression stub for a promoted turn.

    Uses the ``easycat_bundle`` fixture plus ``assert_no_error`` /
    ``assert_turn_completed`` / ``assert_exact_match`` from
    :mod:`easycat.debug.testing`.  When the turn's ``agent_final`` text was
    captured we assert it exactly; otherwise we emit a ``TODO`` so the
    author fills in the expected reply.
    """
    safe_id = _promote_stub_test_name(turn_id)
    if expected is not None:
        match_line = f"    assert_exact_match(bundle, expected={expected!r})"
    else:
        match_line = (
            "    # TODO: fill in the expected agent reply for this turn.\n"
            '    # assert_exact_match(bundle, expected="...")'
        )
    return "\n".join(
        [
            "from easycat.debug.testing import (",
            "    assert_exact_match,",
            "    assert_no_error,",
            "    assert_turn_completed,",
            ")",
            "",
            "",
            f"def test_{safe_id}(easycat_bundle):",
            f"    bundle = easycat_bundle({bundle_name!r})",
            "    assert_no_error(bundle)",
            f"    assert_turn_completed(bundle, {turn_id!r})",
            match_line,
            "",
        ]
    )


def _promoted_agent_text(records: list[dict[str, Any]]) -> str | None:
    """Return a safe ``agent_final`` expected value, or ``None`` if sensitive.

    ``journal promote`` prints a copy-pasteable pytest stub.  Journals can
    contain transcripts, tool payloads, and provider text, so never echo an
    ``agent_final`` value when the shared redaction policy would modify it or
    still considers it sensitive after redaction.  Returning ``None`` keeps the
    promoted bundle usable while making the stub ask the author to fill in the
    exact expectation locally.
    """
    for record in records:
        if record.get("name") != "agent_final":
            continue
        data = record.get("data")
        expected: str | None = None
        if isinstance(data, Mapping) and isinstance(data.get("text"), str):
            expected = data["text"]
        else:
            text = record.get("text")
            if isinstance(text, str):
                expected = text
        if expected is None:
            return None
        redacted = redact_text(expected)
        if redacted != expected or contains_unredacted_sensitive_text(redacted):
            return None
        return expected
    return None


def _validate_promoted_slice(
    sliced: RunBundle, tmp_path: Path, *, turn_id: str
) -> tuple[int | None, str | None]:
    """Write *sliced* to *tmp_path*, reload it, and replay twice.

    Returns ``(frame_count, None)`` when the slice replays cleanly and
    deterministically; ``(None, message)`` otherwise.  On any failure the
    temp file is removed so no half-written bundle lingers next to ``--out``.
    """
    try:
        sliced.save(tmp_path)
        reloaded = RunBundle.load(tmp_path)
        first = _replay_signature(reloaded)
        second = _replay_signature(reloaded)
    except (BundleError, BundleValidationError, ReplayError, ReplaySideEffectBlocked) as exc:
        tmp_path.unlink(missing_ok=True)
        return None, f"Promoted turn does not replay cleanly: {exc}"
    except Exception as exc:  # noqa: BLE001 - never leave a temp behind
        tmp_path.unlink(missing_ok=True)
        return None, f"Promoted turn failed validation replay: {exc}"

    if first != second:
        tmp_path.unlink(missing_ok=True)
        return None, (
            f"Turn {turn_id!r} replays non-deterministically; "
            "refusing to promote a flaky regression."
        )
    return len(first), None


@cli_command
def promote_turn(
    bundle_path: Path = typer.Argument(
        ...,
        help=(
            "Path to a ZIP bundle archive (``.zip``, ``.bundle``, or "
            "``.easycat-bundle``) or a ``.sqlite`` journal."
        ),
    ),
    turn_id: str = typer.Argument(
        ...,
        help="Turn id to promote into a self-contained, replayable regression bundle.",
    ),
    out: Path = typer.Option(
        ...,
        "--out",
        "-o",
        help="Destination ``.zip`` for the single-turn regression bundle.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Overwrite the destination if it already exists.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Promote one turn into a replayable, self-contained regression bundle.

    Slices the turn's journal records and the artifact blobs they reference
    into a new bundle, then validates it before writing: the slice is
    replayed twice at ARTIFACT fidelity (tools denied, fast timing) and must
    both succeed and produce an identical frame signature.  A turn that
    replays non-deterministically (a live tool call, a barge-in) is rejected
    so a flaky bundle never lands as a regression test.  On success the
    bundle is written atomically and a copy-pasteable pytest stub using the
    ``easycat_bundle`` fixture is printed.
    """
    safe_id = safe_turn_id(turn_id)
    if safe_id is None:
        emit_command_error(
            "journal_promote",
            f"Invalid turn id: {turn_id!r}.",
            json_output=json_output,
            exit_code=2,
            path=str(bundle_path),
        )
        raise typer.Exit(2)

    if out.exists() and not force:
        emit_command_error(
            "journal_promote",
            f"Output already exists: {out}. Use --force to overwrite.",
            json_output=json_output,
            exit_code=101,
            path=str(bundle_path),
            out=str(out),
        )
        raise typer.Exit(101)

    if out.exists() and out.is_dir() and not out.is_symlink():
        # --force overwrites a destination *file*; refuse to recursively delete
        # a directory (e.g. the regressions dir passed in place of a .zip name).
        emit_command_error(
            "journal_promote",
            f"Output path is a directory: {out}. Pass a .zip file path, not a directory.",
            json_output=json_output,
            exit_code=2,
            path=str(bundle_path),
            out=str(out),
        )
        raise typer.Exit(2)

    bundle = _load_bundle_or_journal(
        bundle_path, command="journal_promote", json_output=json_output
    )
    turn_records = bundle.filter_by_turn(safe_id)
    if not turn_records:
        emit_command_error(
            "journal_promote",
            f"No journal records found for turn {safe_id!r}.",
            json_output=json_output,
            exit_code=5,
            path=str(bundle_path),
        )
        raise typer.Exit(5)

    try:
        sliced = slice_bundle_by_turn(bundle, safe_id)
    except ValueError:
        emit_command_error(
            "journal_promote",
            f"No journal records found for turn {safe_id!r}.",
            json_output=json_output,
            exit_code=5,
            path=str(bundle_path),
        )
        raise typer.Exit(5) from None

    # Validate-before-write: serialise the slice to a temp .zip, reload it,
    # and replay twice. Only an identical, successful double replay earns a
    # write to --out; anything else deletes the temp and exits 6.
    import tempfile

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(dir=out.parent, suffix=".zip", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)
    frame_count, error_message = _validate_promoted_slice(sliced, tmp_path, turn_id=safe_id)
    if error_message is not None:
        emit_command_error(
            "journal_promote",
            error_message,
            json_output=json_output,
            exit_code=6,
            path=str(bundle_path),
        )
        raise typer.Exit(6)

    if out.exists():
        # A directory destination was rejected up front, so only a file or
        # symlink can be here — replace it without ever deleting a tree.
        out.unlink()
    tmp_path.rename(out)

    expected = _promoted_agent_text(turn_records)
    stub = _promote_test_stub(bundle_name=out.name, turn_id=safe_id, expected=expected)

    if json_output:
        emit_json(
            json_envelope(
                "journal_promote",
                path=str(bundle_path),
                turn_id=safe_id,
                out=str(out),
                records=len(turn_records),
                artifact_count=len(sliced.artifact_blobs),
                frames=frame_count,
                stub=stub,
            )
        )
        raise typer.Exit(0)

    success(f"Promoted turn {safe_id} to {out}")
    stdout_console.print(stub)


# ── `easycat journal grep` ───────────────────────────────────────


def _redact_grep_match(record: Mapping[str, Any]) -> dict[str, Any]:
    """Project a matched record into a small, fully redacted summary row.

    Every field that can carry caller text — ``data``, the error
    type/message, and the matched field list — is routed through
    :func:`redact_value` / :func:`redact_text` so a phone number or secret in
    a tool argument never reaches stdout, JSON, or a Rich table.
    """
    error = record.get("error")
    error_summary: dict[str, Any] | None = None
    if isinstance(error, Mapping):
        error_summary = {
            "type": redact_text(str(error.get("type"))) if error.get("type") else None,
            "message": redact_text(str(error.get("message"))) if error.get("message") else None,
        }
    return {
        "sequence": record.get("sequence"),
        "turn_id": redact_text(str(record["turn_id"])) if record.get("turn_id") else None,
        "name": redact_text(str(record.get("name") or "")),
        "match_fields": list(record.get("_match_fields") or []),
        "data": redact_value(record.get("data") or {}, "data"),
        "error": error_summary,
    }


def _grep_match_table(matches: list[dict[str, Any]]) -> Table:
    """Render redacted grep matches: sequence, turn, name, matched fields."""
    table = Table(
        title=f"Matches — {len(matches)}",
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 1),
        title_justify="left",
    )
    table.add_column("seq", justify="right", no_wrap=True)
    table.add_column("turn", no_wrap=True, overflow="fold")
    table.add_column("name", no_wrap=True, overflow="fold")
    table.add_column("fields", no_wrap=True)
    table.add_column("detail", overflow="fold")
    if not matches:
        table.add_row("—", "—", "[dim]no matches[/]", "—", "—")
        return table
    for match in matches:
        seq = match.get("sequence")
        turn_id = match.get("turn_id")
        table.add_row(
            str(seq) if isinstance(seq, int) else "[dim]-[/]",
            escape(str(turn_id)) if turn_id else "[dim]-[/]",
            escape(str(match.get("name") or "")),
            escape(", ".join(match.get("match_fields") or []) or "-"),
            escape(_record_detail(match)) or "[dim]-[/]",
        )
    return table


@cli_command
def journal_grep(
    bundle_path: Path = typer.Argument(
        ...,
        help=(
            "Path to a ZIP bundle archive (``.zip``, ``.bundle``, or "
            "``.easycat-bundle``) or a ``.sqlite`` journal."
        ),
    ),
    query: str = typer.Option(
        ...,
        "--query",
        "-q",
        help="Full-text query matched against record data, errors, name, and turn id.",
    ),
    use_regex: bool = typer.Option(
        False,
        "--regex",
        help="Treat the query as a case-insensitive regular expression.",
    ),
    errors_only: bool = typer.Option(
        False,
        "--errors",
        help="Only search records that carry an error.",
    ),
    turn: str | None = typer.Option(
        None,
        "--turn",
        help="Restrict the search to a single turn id.",
    ),
    limit: int = typer.Option(
        500,
        "--limit",
        help="Maximum number of matches to report.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Full-text search a captured journal or bundle, redacting every match.

    Reuses the debugger's ``_search_records`` scan so the CLI and the SPA
    search box agree on what matches.  Every matched field is redacted before
    it is printed, so the output is safe to paste into a bug report.
    """
    from easycat.debugger.server import _SEARCH_SCAN_LIMIT, _search_records

    if limit <= 0:
        emit_command_error(
            "journal_grep",
            "--limit must be greater than 0.",
            json_output=json_output,
            exit_code=2,
            path=str(bundle_path),
        )
        raise typer.Exit(2)

    bundle = _load_bundle_or_journal(bundle_path, command="journal_grep", json_output=json_output)
    records = [r for r in bundle.records() if turn is None or r.get("turn_id") == turn]
    try:
        matched, scan_truncated = _search_records(
            records, query=query, use_regex=use_regex, errors_only=errors_only
        )
    except ValueError as exc:
        emit_command_error(
            "journal_grep",
            f"Invalid query: {exc}.",
            json_output=json_output,
            exit_code=2,
            path=str(bundle_path),
        )
        raise typer.Exit(2) from None

    total = len(matched)
    matched = matched[:limit]
    redacted = [_redact_grep_match(record) for record in matched]

    if json_output:
        emit_json(
            json_envelope(
                "journal_grep",
                path=str(bundle_path),
                query=redact_text(query),
                total=total,
                scan_truncated=scan_truncated,
                scan_limit=_SEARCH_SCAN_LIMIT,
                matches=redacted,
            )
        )
        raise typer.Exit(0)

    stderr_console.print(f"[bold]Journal grep[/] [cyan]{escape(str(bundle_path))}[/]")
    stderr_console.print()
    if scan_truncated:
        warn(f"Scan capped at {_SEARCH_SCAN_LIMIT} records; results may be incomplete.")
    _print_wide(_grep_match_table(redacted), max(stdout_console.width, 120))
    if total > len(redacted):
        stderr_console.print(
            f"[dim]Showing {len(redacted)} of {total} matches; raise --limit to see more.[/]"
        )


# ── `easycat journal follow` / `easycat tail` ────────────────────


# Record names whose ``data['audio_bytes']`` we render as a compact audio
# bar so a tail watcher can eyeball codec/frame throughput without opening
# the debugger UI.
_FOLLOW_AUDIO_NAMES = frozenset(("tts_frame", "stt_audio_in"))
# The TTS first-byte names mirror ``debug/_turn_timeline._TTS_FIRST`` — the
# first such record per turn closes the critical-path milestone, so the
# follow line flags it as a per-turn milestone landmark.
_FOLLOW_TTS_FIRST = frozenset(("tts_frame", "tts_audio"))


def _follow_audio_bar(record: Mapping[str, Any]) -> str:
    """Render a tiny throughput bar for an audio record, or ``""``.

    Reads ``data['audio_bytes']`` (the per-frame byte count the audio
    stages journal) and maps it onto a short block-glyph bar so a long
    tail stays scannable.  Never raises on malformed data.
    """
    data = record.get("data")
    if not isinstance(data, Mapping):
        return ""
    audio_bytes = data.get("audio_bytes")
    if not isinstance(audio_bytes, int) or audio_bytes <= 0:
        return ""
    # ~1 block per kilobyte, capped so a large frame can't blow out the line.
    blocks = min(20, max(1, audio_bytes // 1024))
    return f"audio={audio_bytes}B {'▮' * blocks}"


def _format_follow_line(record: Mapping[str, Any]) -> str:
    """Render one live-tail line for *record* — pure and table-testable.

    Shape: ``[seq] turn=.. name=.. stage=.. detail``.  Two special cases:

    - A synthetic :class:`BufferOverflow` gap notice
      (``data['dropped_from'] == 'follow_gap'``) renders as a one-line
      ``-- gap: N records dropped --`` marker so a non-contiguous sequence
      stream is obvious in the tail.
    - The first TTS byte of a turn and audio frames append a milestone or
      throughput annotation; both reuse ``_record_stage`` / ``_record_detail``
      so the CLI and the bundle timeline agree on field projection.
    """
    data = record.get("data")
    if isinstance(data, Mapping) and data.get("dropped_from") == "follow_gap":
        gap = data.get("gap")
        count = gap if isinstance(gap, int) and gap > 0 else "?"
        return f"-- gap: {count} records dropped --"

    seq = record.get("sequence")
    seq_text = str(seq) if isinstance(seq, int) else "-"
    turn_id = safe_turn_id(record.get("turn_id")) or "-"
    name = str(record.get("name") or "-")
    stage = _record_stage(record) or "-"

    parts = [f"[{seq_text}]", f"turn={turn_id}", f"name={name}", f"stage={stage}"]
    detail = _record_detail(record)
    if detail:
        parts.append(detail)
    # The first TTS byte of a turn closes the critical-path milestone; callers
    # that have already flagged it for a turn pass ``_no_milestone`` to drop
    # the landmark on later frames of the same turn.
    if name in _FOLLOW_TTS_FIRST and not record.get("_no_milestone"):
        parts.append("milestone=tts_first_byte")
    audio_bar = _follow_audio_bar(record)
    if audio_bar:
        parts.append(audio_bar)
    return " ".join(parts)


async def _stream_follow(
    view: Any,
    *,
    from_sequence: int | None,
    errors_only: bool,
    turn_id: str | None,
    json_output: bool,
    cursor: list[int | None] | None = None,
) -> None:
    """Drive a :meth:`JournalView.follow` loop, printing one line per record.

    Persistent SQLite journals are written by a separate live session, so
    transient ``FileNotFoundError`` / ``sqlite3.OperationalError`` (a half-open
    file, a table not yet created) are swallowed and retried on the next poll
    rather than aborting the tail.  Per-turn milestone names ride the formatted
    line so a tail watcher sees the critical-path landmarks inline.
    """
    seen_tts_first: set[str] = set()
    async for record in view.follow(from_sequence=from_sequence, poll_interval=0.25):
        record_dict = _record_to_follow_dict(record)
        # Advance the resume cursor for EVERY yielded record, before any
        # ``errors_only``/``turn_id`` filtering below drops it: a post-outage
        # retry must resume past filtered-out records too, or they are re-read.
        seq = record_dict.get("sequence")
        if cursor is not None and isinstance(seq, int):
            cursor[0] = seq if cursor[0] is None else max(cursor[0], seq)
        # ``errors_only`` filters to records that carry an error, but always
        # let the synthetic gap notice through so a dropped-record warning is
        # never hidden by the filter.
        is_gap = (
            isinstance(record_dict.get("data"), Mapping)
            and record_dict["data"].get("dropped_from") == "follow_gap"
        )
        if errors_only and not record_dict.get("error") and not is_gap:
            continue
        rec_turn = safe_turn_id(record_dict.get("turn_id"))
        if turn_id is not None and not is_gap and rec_turn != turn_id:
            continue

        if json_output:
            # Newline-delimited JSON, one record per line (NOT a single
            # envelope) so a consumer can ``read`` the stream incrementally.
            # Write straight to the file handle, bypassing Rich: Rich soft-wraps
            # at terminal width, which would split long records across lines and
            # mangle the NDJSON when consumers pipe it into ``jq`` or ``read``.
            line = json.dumps(_redact_follow_record(record_dict), sort_keys=False)
            stdout_console.file.write(line + "\n")
            stdout_console.file.flush()
            continue

        # Only the FIRST TTS byte of a turn is the milestone landmark; later
        # frames of the same turn keep the throughput bar but drop the tag.
        name = str(record_dict.get("name") or "")
        if name in _FOLLOW_TTS_FIRST and rec_turn is not None:
            if rec_turn in seen_tts_first:
                record_dict = {**record_dict, "_no_milestone": True}
            else:
                seen_tts_first.add(rec_turn)
        stdout_console.print(escape(_format_follow_line(record_dict)))


async def _follow_with_retry(
    view: Any,
    *,
    from_sequence: int | None,
    errors_only: bool,
    turn_id: str | None,
    json_output: bool,
) -> None:
    """Drive :func:`_stream_follow`, resuming past records already streamed.

    Persistent SQLite journals are written by a separate live session, so a
    mid-stream ``FileNotFoundError`` / ``sqlite3.OperationalError`` (the writer
    has not created the table yet, or the file is mid-rotation) is retried after
    a short back-off.  The retry MUST resume from ``last_yielded + 1`` rather
    than the original ``from_sequence``: keeping the original argument would
    re-emit every already-printed record (``--from-sequence 0``) or recompute
    ``latest_sequence + 1`` at retry time and silently skip records written
    during the outage.  A shared ``cursor`` holder carries the highest yielded
    sequence back out even when the generator unwinds via a propagating
    exception rather than a normal return.
    """
    cursor: list[int | None] = [None]
    resume = from_sequence
    while True:
        try:
            await _stream_follow(
                view,
                from_sequence=resume,
                errors_only=errors_only,
                turn_id=turn_id,
                json_output=json_output,
                cursor=cursor,
            )
            return
        except (FileNotFoundError, sqlite3.OperationalError):
            if cursor[0] is not None:
                resume = cursor[0] + 1
            await asyncio.sleep(0.25)


def _record_to_follow_dict(record: Any) -> dict[str, Any]:
    """Project a ``JournalRecord`` (or dict) into the follow-line dict shape."""
    if isinstance(record, dict):
        return record
    out: dict[str, Any] = {}
    for attr in ("sequence", "session_id", "name", "turn_id", "data", "input_ref", "output_ref"):
        out[attr] = getattr(record, attr, None)
    kind = getattr(record, "kind", None)
    out["kind"] = getattr(kind, "value", kind)
    error = getattr(record, "error", None)
    if error is not None:
        out["error"] = {
            "type": getattr(error, "type", None),
            "message": getattr(error, "message", None),
        }
    timing = getattr(record, "timing", None)
    if timing is not None:
        out["timing"] = {k: getattr(timing, k, None) for k in ("wall_ns", "mono_ns", "cpu_ns")}
    return out


# Free-form STT/agent text that ``SessionJournalSink`` stores under generic
# ``data`` keys: final/partial transcript and model output land under
# ``data.text`` and streamed tokens under ``data.delta``.  Neither key is in
# ``redact_value``'s field-name allowlist, so they would only get pattern-based
# redaction and otherwise stream verbatim utterances (e.g. medical or account
# details).  Replace them wholesale with the shared transcript placeholder.
_FOLLOW_FREE_TEXT_KEYS = ("text", "delta")


def _redact_follow_record(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return a redacted copy of a follow record before JSON streaming.

    Human follow output already renders a narrow, redacted summary.  JSON mode
    intentionally preserves the same record shape for incremental consumers,
    but must pass every projected field through the shared redaction policy
    before writing newline-delimited records to stdout.  Free-form transcript
    and model text under ``data.text`` / ``data.delta`` is stripped explicitly
    because those generic keys fall outside the shared field-name allowlist.
    """
    redacted = cast(dict[str, Any], redact_value(dict(record)))
    data = redacted.get("data")
    if isinstance(data, dict):
        for key in _FOLLOW_FREE_TEXT_KEYS:
            if key in data:
                data[key] = REDACTED_TRANSCRIPT
    return redacted


@cli_command
def follow_journal(
    bundle_path: Path = typer.Argument(
        ...,
        help="Path to a live or crash-dump ``.sqlite`` journal to tail.",
    ),
    from_sequence: int | None = typer.Option(
        None,
        "--from-sequence",
        help="Start the tail at this sequence (default: only future records; 0 replays history).",
    ),
    errors_only: bool = typer.Option(
        False,
        "--errors",
        help="Only print records that carry an error.",
    ),
    turn: str | None = typer.Option(
        None,
        "--turn",
        help="Restrict the tail to a single turn id.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Stream newline-delimited JSON, one record per line (not a single envelope).",
    ),
) -> None:
    """Live-tail a SQLite journal as it grows, redacting every printed line.

    Wraps a :class:`ReadonlySqliteJournal` in a :class:`JournalView` and
    drives :meth:`JournalView.follow`, so a tail keeps up with a session
    writing the same ``.sqlite`` file.  Exported ZIP bundles are immutable
    and cannot grow, so they exit with guidance to use ``bundles show``.
    """
    from easycat.runtime import JournalView
    from easycat.runtime.journal_views import ReadonlySqliteJournal

    if bundle_path.suffix != ".sqlite":
        emit_command_error(
            "journal_follow",
            "Live tail only works on a .sqlite journal; ZIP bundles are immutable. "
            "Use 'easycat bundles show <path>' or 'easycat journal grep <path>' instead.",
            json_output=json_output,
            exit_code=2,
            path=str(bundle_path),
        )
        raise typer.Exit(2)
    if not bundle_path.exists():
        emit_command_error(
            "journal_follow",
            f"Journal not found: {bundle_path}",
            json_output=json_output,
            exit_code=5,
            path=str(bundle_path),
        )
        raise typer.Exit(5)

    view = JournalView(ReadonlySqliteJournal(bundle_path))
    if not json_output:
        stderr_console.print(
            f"[bold]Tailing[/] [cyan]{escape(str(bundle_path))}[/] — Ctrl-C to stop."
        )

    async def _runner() -> None:
        await _follow_with_retry(
            view,
            from_sequence=from_sequence,
            errors_only=errors_only,
            turn_id=turn,
            json_output=json_output,
        )

    # A bare Ctrl-C propagates out of ``asyncio.run`` as ``KeyboardInterrupt``;
    # the top-level ``main()`` handler maps it to a clean exit code 130.
    asyncio.run(_runner())


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
