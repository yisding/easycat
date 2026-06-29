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
import shutil
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

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
from easycat.debug._issues import build_issues
from easycat.debug._turn_diff import diff_bundles
from easycat.debug._turn_timeline import record_wall_ns, safe_turn_id, turn_waterfall
from easycat.debug.annotations import load_annotations
from easycat.debug.bundle import (
    BundleError,
    BundleInUseError,
    BundleValidationError,
    BundleVersionError,
    RunBundle,
    discover_bundles_with_status,
)
from easycat.debug.export import slice_bundle_by_turn
from easycat.runtime.replay import (
    ProviderVersionMismatchError,
    ReplayError,
    ReplayFidelity,
    ReplayResult,
    ReplaySideEffectBlocked,
    ReplaySpec,
    ToolReplayPolicy,
)
from easycat.validation.latency import LatencyPercentileStats
from easycat.validation.redaction import (
    REDACTION_VERSION,
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


def _annotations_tally(annotations: Mapping[str, Any]) -> dict[str, Any]:
    """Roll a per-turn verdict sidecar map into a small pass/fail tally.

    ``annotations`` is the ``{turn_id: record}`` map from
    ``debug/annotations.load_annotations``.  Surfaces the pass/fail counts
    and a failure-type histogram so ``bundles show`` can show triage state
    at a glance without listing every turn.
    """
    passed = 0
    failed = 0
    failure_types: dict[str, int] = {}
    for record in annotations.values():
        if not isinstance(record, Mapping):
            continue
        verdict = record.get("passed")
        if verdict is True:
            passed += 1
        elif verdict is False:
            failed += 1
        failure_type = record.get("failure_type")
        if isinstance(failure_type, str) and failure_type:
            failure_types[failure_type] = failure_types.get(failure_type, 0) + 1
    return {
        "annotated": len(annotations),
        "passed": passed,
        "failed": failed,
        "failure_types": failure_types,
    }


def _add_annotations_row(table: Table, tally: object) -> None:
    """Append the reviewer-verdict tally row to *table*, or nothing.

    A bundle with no annotated turns gets no row at all so ``bundles show``
    stays terse for un-triaged captures.
    """
    if not isinstance(tally, Mapping) or not tally.get("annotated"):
        return
    passed = int(tally.get("passed", 0))
    failed = int(tally.get("failed", 0))
    annotated = int(tally.get("annotated", 0))
    verdict = f"[green]{passed} pass[/] / [red]{failed} fail[/] of {annotated} annotated"
    failure_types = tally.get("failure_types")
    if isinstance(failure_types, Mapping) and failure_types:
        tallies = ", ".join(
            f"{escape(str(name))}={count}" for name, count in sorted(failure_types.items())
        )
        verdict = f"{verdict} ({tallies})"
    table.add_row("annotations", verdict)


def _summarise_bundle(
    bundle: RunBundle, *, annotations: Mapping[str, Any] | None = None
) -> dict[str, object]:
    """Collect the high-signal fields we surface in ``bundles show``/``inspect``."""
    turn_ids: set[str] = set()
    errors = 0
    session_id = ""
    first_wall_ns: int | None = None
    last_wall_ns: int | None = None
    tool_calls = 0
    record_count = 0
    error_type: str | None = None
    failing_turn_id: str | None = None
    records = list(bundle.records())

    for record in records:
        record_count += 1
        if not session_id and record.get("session_id"):
            session_id = str(record["session_id"])
        turn_id = safe_turn_id(record.get("turn_id"))
        if turn_id is not None:
            turn_ids.add(turn_id)
        wall_ns = record_wall_ns(record)
        if wall_ns is not None:
            if first_wall_ns is None:
                first_wall_ns = wall_ns
            last_wall_ns = wall_ns
        error = record.get("error")
        if error:
            errors += 1
            # Surface the first error's type + the turn it failed on so the
            # CLI summary points straight at the failure.  ``error['type']``
            # is a bare exception/class name (redaction-safe — no payload);
            # ``_summarise_bundle`` already feeds the redacted export path.
            if error_type is None and isinstance(error, Mapping):
                etype = error.get("type")
                if etype:
                    error_type = str(etype)
                    failing_turn_id = turn_id
        # The journal sink records tool calls under the snake_case name
        # ``tool_call_started`` (see ``SessionJournalSink``), not the
        # CamelCase event class name.
        if record.get("name") == "tool_call_started":
            tool_calls += 1

    duration_ms: float | None = None
    if first_wall_ns is not None and last_wall_ns is not None:
        duration_ms = (last_wall_ns - first_wall_ns) / 1_000_000

    return {
        "session_id": session_id,
        "turn_count": len(turn_ids),
        # Per-turn latency waterfall: per-stage spans plus milestone deltas
        # (VAD endpoint → STT final → agent first token → TTS first byte),
        # shared with the debugger UI via ``debug/_turn_timeline``.
        "turns": turn_waterfall(records),
        # Severity-ranked heuristic findings (errors, tool failures, timeouts,
        # empty transcripts, slow milestones) shared with the debugger UI via
        # ``debug/_issues``.  Always present so the JSON shape is stable; the
        # ``--issues`` flag only toggles the human-readable card table.
        "issues": build_issues(records, artifact_resolver=bundle.artifact_blobs.get),
        "errors": errors,
        "error_type": error_type,
        "failing_turn_id": failing_turn_id,
        "tool_calls": tool_calls,
        "records": record_count,
        "duration_ms": duration_ms,
        # Reviewer verdict tally from the ``<bundle>.annotations.json``
        # sidecar when one exists (always present so the JSON shape is
        # stable; an absent sidecar yields all-zero counts).
        "annotations": _annotations_tally(annotations or {}),
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


_EXPORT_TARGETS = frozenset(("claude-code", "cursor", "codex"))
_REDACTION_POLICIES = frozenset(("development", "production", "regulated"))
_CONTEXT_DATA_KEYS = frozenset(
    {
        "boundary_reason",
        "bridge_latency_ms",
        "call_id",
        "cancellation_mode",
        "cause",
        "caused_by_signal_id",
        "checkpoint_id",
        "committable",
        "direction",
        "display_name",
        "duration_ms",
        "exit_reason",
        "fidelity",
        "format",
        "framework",
        "from_unit",
        "handoff_reason",
        "latency_ms",
        "model",
        "mutation_kind",
        "observed_stage",
        "parent_unit_id",
        "phase",
        "provider",
        "provider_name",
        "sample_rate",
        "signal_id",
        "signal_kind",
        "stage",
        "to_unit",
        "tool_call_id",
        "tool_name",
        "transition_kind",
        "unit_id",
        "unit_kind",
    }
)
_CONTEXT_TOP_LEVEL_KEYS = _CONTEXT_DATA_KEYS | frozenset(
    {
        "framework",
        "direction",
        "bridge_latency_ms",
    }
)
_CONTEXT_ERROR_KEYS = frozenset(("type", "code", "status"))


def _default_export_path(bundle_path: Path) -> Path:
    return bundle_path.with_name(f"{bundle_path.stem}-pack")


def _context_error(error: Any) -> dict[str, Any] | None:
    if not isinstance(error, Mapping):
        return None

    context = {
        str(key): redact_value(error[key], str(key))
        for key in sorted(error, key=str)
        if str(key) in _CONTEXT_ERROR_KEYS and error[key] not in (None, "", [], {})
    }
    omitted = sum(
        1
        for key, value in error.items()
        if str(key) not in _CONTEXT_ERROR_KEYS and value not in (None, "", [], {})
    )
    if omitted > 0:
        context["omitted_error_fields"] = omitted
    return context or None


def _context_record(record: Mapping[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for key in ("sequence", "kind", "name", "session_id", "turn_id"):
        value = record.get(key)
        if value not in (None, ""):
            context[key] = redact_value(value, key)

    wall_ns = record_wall_ns(record)
    if wall_ns is not None:
        context["wall_ns"] = wall_ns

    data = record.get("data")
    if isinstance(data, Mapping):
        safe_data = {
            str(key): redact_value(data[key], str(key))
            for key in sorted(data, key=str)
            if str(key) in _CONTEXT_DATA_KEYS
        }
        if safe_data:
            context["data"] = safe_data
        omitted = len(data) - len(safe_data)
        if omitted > 0:
            context["omitted_data_fields"] = omitted
    elif data not in (None, "", {}, []):
        context["omitted_data_fields"] = 1

    for key in sorted(_CONTEXT_TOP_LEVEL_KEYS):
        if key in record and key not in context:
            context[key] = redact_value(record[key], key)

    refs: dict[str, Any] = {}
    for key in ("input_ref", "output_ref"):
        value = record.get(key)
        if value:
            refs[key] = redact_value(value, key)
    if refs:
        context["refs"] = refs

    error = _context_error(record.get("error"))
    if error:
        context["error"] = error

    tags = record.get("tags")
    if isinstance(tags, list | tuple | set | frozenset) and tags:
        context["tags"] = redact_value(sorted(tags, key=str), "tags")

    return context


def _context_records(bundle: RunBundle) -> list[dict[str, Any]]:
    return [_context_record(record) for record in bundle.records()]


def _markdown_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return redact_text(text).replace("\n", " ").replace("|", r"\|")


def _record_stage(record: Mapping[str, Any]) -> str:
    data = record.get("data")
    if isinstance(data, Mapping):
        for key in ("stage", "observed_stage"):
            value = data.get(key)
            if value:
                return str(value)
    return ""


def _record_detail(record: Mapping[str, Any]) -> str:
    parts: list[str] = []
    data = record.get("data")
    if isinstance(data, Mapping):
        for key in ("tool_name", "tool_call_id", "call_id", "phase", "unit_id", "unit_kind"):
            value = data.get(key)
            if value not in (None, ""):
                parts.append(f"{key}={redact_text(str(value))}")
    error = record.get("error")
    if isinstance(error, Mapping):
        error_type = error.get("type")
        omitted = error.get("omitted_error_fields")
        if error_type:
            parts.append(f"error_type={redact_text(str(error_type))}")
        if omitted:
            parts.append(f"omitted_error_fields={redact_text(str(omitted))}")
    return "; ".join(parts)


def _timeline_markdown(records: list[dict[str, Any]]) -> str:
    lines = [
        "# Timeline",
        "",
        "| seq | kind | name | turn | stage | detail |",
        "|---:|---|---|---|---|---|",
    ]
    for record in records:
        lines.append(
            "| "
            + " | ".join(
                (
                    _markdown_cell(record.get("sequence")),
                    _markdown_cell(record.get("kind")),
                    _markdown_cell(record.get("name")),
                    _markdown_cell(record.get("turn_id")),
                    _markdown_cell(_record_stage(record)),
                    _markdown_cell(_record_detail(record)),
                )
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def _readme_text(
    *,
    target: str,
    source_path: str,
    summary: Mapping[str, Any],
    redaction_requested: str,
    redaction_applied: str,
) -> str:
    session_id = summary.get("session_id") or "(unknown)"
    return "\n".join(
        [
            "# EasyCat Debug Context Pack",
            "",
            f"Target: {target}",
            f"Source: {source_path}",
            f"Session: {session_id}",
            f"Records: {summary.get('records', 0)}",
            f"Errors: {summary.get('errors', 0)}",
            "",
            "## Files",
            "",
            "- summary.json: bundle metadata, provider versions, and artifact index.",
            "- timeline.md: compact redacted event timeline for quick reading.",
            "- timeline.jsonl: one redacted event per line for scripts and coding agents.",
            "",
            "## Safety Boundary",
            "",
            "This pack intentionally omits raw journal payload fields such as transcripts, "
            "prompts, generated text, tool arguments, tool results, and provider responses.",
            "Treat the original bundle or SQLite journal as sensitive.",
            f"Requested redaction: {redaction_requested}; applied redaction: {redaction_applied}.",
            "",
            "## Useful Commands",
            "",
            "```bash",
            "easycat bundles show <bundle> --json",
            "easycat replay <bundle> --json",
            "```",
            "",
        ]
    )


def _artifact_manifest(
    bundle: RunBundle,
    *,
    output_path: Path,
    include_audio: bool,
) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    artifacts_dir = output_path / "artifacts"
    if include_audio and bundle.artifact_blobs:
        artifacts_dir.mkdir()

    for ref, entry in sorted(bundle.artifact_index.items()):
        artifact: dict[str, Any] = {
            "ref": redact_value(ref, "artifact_ref"),
            "size_bytes": entry.size_bytes,
            "included": False,
        }
        data = bundle.artifact_blobs.get(ref)
        if include_audio and data is not None:
            filename = f"{ref}.bin"
            (artifacts_dir / filename).write_bytes(data)
            artifact["included"] = True
            artifact["path"] = f"artifacts/{filename}"
        artifacts.append(artifact)
    return artifacts


def _prepare_output_dir(
    output_path: Path,
    *,
    force: bool,
    command: str,
    json_output: bool,
) -> None:
    resolved = output_path.resolve(strict=False)
    if resolved == Path(resolved.anchor) or resolved == Path.cwd().resolve():
        emit_command_error(
            command,
            f"Refusing to export into {output_path}.",
            json_output=json_output,
            exit_code=1,
            output_path=str(output_path),
        )
        raise typer.Exit(1)

    if output_path.exists():
        if not force:
            emit_command_error(
                command,
                f"Output path already exists: {output_path}. Use --force to replace it.",
                json_output=json_output,
                exit_code=101,
                output_path=str(output_path),
            )
            raise typer.Exit(101)
        if output_path.is_symlink() or not output_path.is_dir():
            output_path.unlink()
        else:
            shutil.rmtree(output_path)
    output_path.mkdir(parents=True)


def _assert_context_pack_redacted(output_path: Path) -> None:
    for path in (
        output_path / "README.md",
        output_path / "summary.json",
        output_path / "timeline.md",
    ):
        if contains_unredacted_sensitive_text(path.read_text(encoding="utf-8")):
            raise RuntimeError(f"{path.name} contains unredacted sensitive text")
    timeline = output_path / "timeline.jsonl"
    if contains_unredacted_sensitive_text(timeline.read_text(encoding="utf-8")):
        raise RuntimeError("timeline.jsonl contains unredacted sensitive text")


def _write_context_pack(
    bundle: RunBundle,
    *,
    bundle_path: Path,
    output_path: Path,
    target: str,
    redaction_requested: str,
    include_audio: bool,
    force: bool,
    command: str,
    json_output: bool,
) -> dict[str, Any]:
    _prepare_output_dir(output_path, force=force, command=command, json_output=json_output)

    summary = redact_value(_summarise_bundle(bundle))
    if not isinstance(summary, dict):
        summary = {}
    records = _context_records(bundle)
    source_path = redact_text(str(bundle_path))
    # Context-pack export always applies the conservative production redaction
    # boundary regardless of the requested policy; durable journals are on by
    # default, so exports must stay safe to share without an explicit opt-in.
    redaction_applied = "production"
    files = ["README.md", "summary.json", "timeline.md", "timeline.jsonl"]

    artifacts = _artifact_manifest(bundle, output_path=output_path, include_audio=include_audio)
    if include_audio and artifacts:
        files.append("artifacts/")

    manifest = {
        "provider_versions": redact_value(bundle.manifest.provider_versions),
        "config_snapshot": redact_value(bundle.manifest.config_snapshot),
        "env_metadata": redact_value(bundle.manifest.env_metadata),
        "sharing_banner": redact_text(bundle.manifest.sharing_banner or bundle.sharing_banner),
    }
    payload = {
        "schema_version": 1,
        "target": target,
        "source_path": source_path,
        "format_version": bundle.format_version,
        "redaction": {
            "version": REDACTION_VERSION,
            "requested": redaction_requested,
            "applied": redaction_applied,
            "boundary": "context-pack-minimal",
        },
        "summary": summary,
        "manifest": manifest,
        "artifacts": artifacts,
        "files": files,
    }

    (output_path / "README.md").write_text(
        _readme_text(
            target=target,
            source_path=source_path,
            summary=summary,
            redaction_requested=redaction_requested,
            redaction_applied=redaction_applied,
        ),
        encoding="utf-8",
    )
    (output_path / "summary.json").write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    (output_path / "timeline.md").write_text(_timeline_markdown(records), encoding="utf-8")
    (output_path / "timeline.jsonl").write_text(
        "".join(json.dumps(record, sort_keys=False) + "\n" for record in records),
        encoding="utf-8",
    )

    _assert_context_pack_redacted(output_path)
    return {
        "target": target,
        "output_path": str(output_path),
        "source_path": source_path,
        "format_version": bundle.format_version,
        "redaction": payload["redaction"],
        "summary": summary,
        "files": files,
        "records": len(records),
        "artifacts": artifacts,
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


def _print_wide(renderable: object, width: int) -> None:
    """Print *renderable* through a console wide enough to avoid clipping.

    ``Console.print(..., width=)`` is clamped to the console's own width, so
    a long path in a no-wrap column would still be truncated.  Render via a
    fresh console pinned to *width*, writing to the same destination as the
    primary stdout console so capture/redirection still works.
    """
    from rich.console import Console

    wide = Console(
        file=stdout_console.file,
        force_terminal=stdout_console.is_terminal,
        no_color=stdout_console.no_color,
        width=width,
    )
    wide.print(renderable)


def _crash_dump_artifact_root(sqlite_path: Path) -> Path | None:
    """Locate the sibling artifact dir for a ``crash-dumps/<id>.sqlite`` file.

    Crash dumps live at ``.easycat/crash-dumps/<session_id>.sqlite`` and
    their artifacts at ``.easycat/artifacts/<session_id>/`` (see the
    storage layout in ``runtime/DURABILITY.md``).  Return that directory
    if it exists, else ``None`` so the journal is loaded without blobs.
    """
    artifact_root = sqlite_path.parent.parent / "artifacts" / sqlite_path.stem
    return artifact_root if artifact_root.is_dir() else None


def _load_bundle_or_journal(
    bundle_path: Path,
    *,
    command: str,
    json_output: bool,
) -> RunBundle:
    """Load a ZIP bundle or SQLite journal, mapping failures to CLI exits."""
    if not bundle_path.exists():
        emit_command_error(
            command,
            f"Bundle not found: {bundle_path}",
            json_output=json_output,
            exit_code=5,
            path=str(bundle_path),
        )
        raise typer.Exit(5)

    try:
        if bundle_path.suffix == ".sqlite":
            # Crash-dump SQLite journals are not ZIP archives; load them
            # via the partial-journal path with their sibling artifacts.
            return RunBundle.from_partial_journal(
                bundle_path,
                artifact_root=_crash_dump_artifact_root(bundle_path),
            )
        return RunBundle.load(bundle_path)
    except BundleVersionError as exc:
        emit_command_error(
            command,
            f"Bundle was written by a newer easycat ({exc}); upgrade easycat to inspect it.",
            json_output=json_output,
            exit_code=5,
            path=str(bundle_path),
        )
        raise typer.Exit(5) from None
    except BundleInUseError as exc:
        emit_command_error(
            command,
            str(exc),
            json_output=json_output,
            exit_code=5,
            path=str(bundle_path),
        )
        raise typer.Exit(5) from None
    except BundleError as exc:
        emit_command_error(
            command,
            f"Bundle corrupt or unreadable: {exc}",
            json_output=json_output,
            exit_code=5,
            path=str(bundle_path),
        )
        raise typer.Exit(5) from None


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


def _format_ms(value: object) -> str:
    return f"{value:.1f}" if isinstance(value, int | float) else "-"


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


# ── `easycat bundles export` ─────────────────────────────────────


@cli_command
def export_bundle(
    bundle_path: Path = typer.Argument(
        ...,
        help=(
            "Path to a ZIP bundle archive (``.zip``, ``.bundle``, or "
            "``.easycat-bundle``) or a ``.sqlite`` journal."
        ),
    ),
    export_for: str = typer.Option(
        "claude-code",
        "--for",
        help="Consumer format: claude-code, cursor, or codex.",
    ),
    output_path: Path | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Directory to write (default: ``./<bundle>-pack/`` next to the bundle).",
    ),
    redaction: str = typer.Option(
        "production",
        "--redaction",
        help="Requested policy: development, production, or regulated.",
    ),
    include_audio: bool = typer.Option(
        False,
        "--include-audio/--no-include-audio",
        help="Copy artifact blobs into the pack. Off by default.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Replace the output directory if it already exists.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Write a redacted context pack for a coding agent."""
    target = export_for.strip().lower()
    if target not in _EXPORT_TARGETS:
        emit_command_error(
            "bundles_export",
            "target must be one of: claude-code, cursor, codex",
            json_output=json_output,
            exit_code=2,
        )
        raise typer.Exit(2)

    redaction_requested = redaction.strip().lower()
    if redaction_requested not in _REDACTION_POLICIES:
        emit_command_error(
            "bundles_export",
            "redaction must be one of: development, production, regulated",
            json_output=json_output,
            exit_code=2,
        )
        raise typer.Exit(2)

    bundle = _load_bundle_or_journal(
        bundle_path,
        command="bundles_export",
        json_output=json_output,
    )
    destination = output_path or _default_export_path(bundle_path)

    if redaction_requested != "production" and not json_output:
        warn("Context-pack export currently applies the conservative production boundary.")
    if include_audio and not json_output:
        warn(
            "Artifact blobs may contain sensitive audio or payload data; "
            "treat the pack as sensitive."
        )

    try:
        payload = _write_context_pack(
            bundle,
            bundle_path=bundle_path,
            output_path=destination,
            target=target,
            redaction_requested=redaction_requested,
            include_audio=include_audio,
            force=force,
            command="bundles_export",
            json_output=json_output,
        )
    except typer.Exit:
        raise
    except RuntimeError as exc:
        emit_command_error(
            "bundles_export",
            f"Export failed: {exc}",
            json_output=json_output,
            exit_code=1,
            path=str(bundle_path),
            output_path=str(destination),
        )
        raise typer.Exit(1) from None

    if json_output:
        emit_json(json_envelope("bundles_export", **payload))
        raise typer.Exit(0)

    success(f"Wrote context pack to {destination}")
    stdout_console.print(escape(str(destination)), soft_wrap=True)


def _render_replay_summary(
    *,
    bundle_path: Path,
    result: ReplayResult,
    spec: ReplaySpec,
    json_output: bool,
) -> None:
    stages = sorted({frame.stage for frame in result.frames if frame.stage})
    summary = {
        "path": str(bundle_path),
        "fidelity_requested": spec.fidelity.value,
        "fidelity_effective": result.fidelity_label.value,
        "frames": len(result.frames),
        "stages": stages,
        "side_effecting": result.side_effecting,
        "tool_policy": spec.tool_policy.value,
        "blocked_tool_calls": result.blocked_tool_calls,
        "stubbed_tool_calls": result.stubbed_tool_calls,
        "allowed_tool_calls": result.allowed_tool_calls,
        "from_sequence": spec.from_sequence,
        "to_sequence": spec.to_sequence,
        "stage_filter": spec.stage_filter or [],
        "timing": spec.timing,
        "force": spec.force,
    }

    if json_output:
        emit_json(json_envelope("replay", **summary))
        raise typer.Exit(0)

    stderr_console.print(f"[bold]Replay[/] [cyan]{escape(str(bundle_path))}[/]")
    stderr_console.print()
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(style="bold", no_wrap=True)
    table.add_column()
    table.add_row("fidelity", str(summary["fidelity_effective"]))
    if summary["fidelity_effective"] != summary["fidelity_requested"]:
        table.add_row("requested_fidelity", str(summary["fidelity_requested"]))
    table.add_row("frames", str(summary["frames"]))
    table.add_row("stages", escape(", ".join(stages)) if stages else "[dim](none)[/]")
    table.add_row("tool_policy", str(summary["tool_policy"]))
    table.add_row("side_effecting", "yes" if result.side_effecting else "no")
    if result.stubbed_tool_calls:
        table.add_row("stubbed_tools", escape(", ".join(result.stubbed_tool_calls)))
    if result.allowed_tool_calls:
        table.add_row("allowed_tools", escape(", ".join(result.allowed_tool_calls)))
    stdout_console.print(table)


@cli_command
def replay_bundle(
    bundle_path: Path = typer.Argument(
        ...,
        help=(
            "Path to a ZIP bundle archive (``.zip``, ``.bundle``, or "
            "``.easycat-bundle``) or a ``.sqlite`` journal."
        ),
    ),
    fidelity: ReplayFidelity = typer.Option(
        ReplayFidelity.ARTIFACT,
        "--fidelity",
        help="Replay fidelity: artifact, simulated, or live.",
        case_sensitive=False,
    ),
    from_sequence: int | None = typer.Option(
        None,
        "--from-sequence",
        help="Start replay at a committable journal sequence.",
    ),
    to_sequence: int | None = typer.Option(
        None,
        "--to-sequence",
        help="Stop replay after this journal sequence.",
    ),
    turn: str | None = typer.Option(
        None,
        "--turn",
        help=(
            "Restrict replay to one turn id; resolves the turn's min/max "
            "journal sequence and sets the replay window (overrides "
            "--from-sequence/--to-sequence)."
        ),
    ),
    stage: list[str] | None = typer.Option(
        None,
        "--stage",
        help="Restrict replay to one stage; may be repeated.",
    ),
    timing: str = typer.Option(
        "fast",
        "--timing",
        help="Timing mode: fast or wall.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Allow version-mismatch downgrades when replay context supplies installed versions.",
    ),
    tool_policy: ToolReplayPolicy = typer.Option(
        ToolReplayPolicy.DENY,
        "--tool-policy",
        help="Tool-call policy during replay: deny, stub, or allow.",
        case_sensitive=False,
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Replay a debug bundle or SQLite journal without opening a Python REPL."""
    if timing not in ("fast", "wall"):
        emit_command_error(
            "replay",
            "timing must be 'fast' or 'wall'",
            json_output=json_output,
            exit_code=2,
        )
        raise typer.Exit(2)
    timing_value = cast(Literal["fast", "wall"], timing)

    bundle = _load_bundle_or_journal(bundle_path, command="replay", json_output=json_output)
    if turn is not None:
        turn_id = safe_turn_id(turn)
        if turn_id is None:
            emit_command_error(
                "replay",
                f"Invalid turn id: {turn!r}.",
                json_output=json_output,
                exit_code=2,
                path=str(bundle_path),
            )
            raise typer.Exit(2)
        sequences = [
            r["sequence"]
            for r in bundle.filter_by_turn(turn_id)
            if isinstance(r.get("sequence"), int)
        ]
        if not sequences:
            emit_command_error(
                "replay",
                f"No journal records found for turn {turn_id!r}.",
                json_output=json_output,
                exit_code=5,
                path=str(bundle_path),
            )
            raise typer.Exit(5)
        from_sequence = min(sequences)
        to_sequence = max(sequences)
    spec = ReplaySpec(
        fidelity=fidelity,
        from_sequence=from_sequence,
        to_sequence=to_sequence,
        stage_filter=stage or None,
        timing=timing_value,
        force=force,
        tool_policy=tool_policy,
    )
    try:
        result = bundle.replay(spec)
    except ReplaySideEffectBlocked as exc:
        message = (
            f"Replay blocked: {exc}. Use --tool-policy stub to replace tool effects, "
            "or --tool-policy allow only when side effects are safe."
        )
        emit_command_error(
            "replay",
            message,
            json_output=json_output,
            exit_code=6,
            path=str(bundle_path),
        )
        raise typer.Exit(6) from None
    except ProviderVersionMismatchError as exc:
        message = (
            f"Replay provider version check failed: {exc}. "
            "Use --force to continue with downgraded fidelity."
        )
        emit_command_error(
            "replay",
            message,
            json_output=json_output,
            exit_code=6,
            path=str(bundle_path),
        )
        raise typer.Exit(6) from None
    except ReplayError as exc:
        emit_command_error(
            "replay",
            f"Replay failed: {exc}",
            json_output=json_output,
            exit_code=6,
            path=str(bundle_path),
        )
        raise typer.Exit(6) from None

    _render_replay_summary(
        bundle_path=bundle_path,
        result=result,
        spec=spec,
        json_output=json_output,
    )


# ── `easycat latency` ────────────────────────────────────────────

# The five critical-path milestone deltas, in pipeline order, paired with
# the compact column labels the per-turn table renders.  These mirror the
# debugger critical-path panel (debugger/static/index.html CP_SEGMENTS) and
# the ``turn_waterfall`` milestone keys, so the CLI and SPA stay in lockstep.
_LATENCY_MILESTONES: tuple[tuple[str, str], ...] = (
    ("vad->stt", "vad_endpoint_to_stt_final_ms"),
    ("stt->req", "stt_final_to_agent_request_ms"),
    ("req->token", "agent_request_to_first_token_ms"),
    ("token->tts", "agent_first_token_to_tts_first_byte_ms"),
    ("vad->tts", "vad_endpoint_to_tts_first_byte_ms"),
)


def _latency_percentiles(turns: list[dict[str, Any]]) -> dict[str, LatencyPercentileStats]:
    """Per-milestone p50/p90/p95/p99 across all turns.

    Reuses ``validation.latency.LatencyPercentileStats.from_values`` (which
    drops ``None`` deltas) so the CLI never reimplements percentile math.
    """
    stats: dict[str, LatencyPercentileStats] = {}
    for label, key in _LATENCY_MILESTONES:
        values = [turn.get("milestones", {}).get(key) for turn in turns]
        stats[label] = LatencyPercentileStats.from_values(values)
    return stats


def _latency_turn_table(turns: list[dict[str, Any]]) -> Table:
    """One row per turn: the five critical-path milestone deltas in ms."""
    table = Table(
        title="Per-turn critical path (ms) — see docs/latency.md",
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 1),
        title_justify="left",
    )
    table.add_column("turn", no_wrap=True, overflow="fold")
    for label, _key in _LATENCY_MILESTONES:
        table.add_column(label, justify="right", no_wrap=True)
    for turn in turns:
        milestones = turn.get("milestones") or {}
        table.add_row(
            escape(str(turn.get("turn_id", ""))),
            *[_format_ms(milestones.get(key)) for _label, key in _LATENCY_MILESTONES],
        )
    return table


def _latency_percentile_table(stats: dict[str, LatencyPercentileStats]) -> Table:
    """Percentile summary: one row per milestone, count + p50/p90/p95/p99."""
    table = Table(
        title="Critical-path percentiles (ms) — see docs/latency.md",
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 1),
        title_justify="left",
    )
    table.add_column("milestone", no_wrap=True)
    table.add_column("count", justify="right", no_wrap=True)
    table.add_column("p50", justify="right", no_wrap=True)
    table.add_column("p90", justify="right", no_wrap=True)
    table.add_column("p95", justify="right", no_wrap=True)
    table.add_column("p99", justify="right", no_wrap=True)
    for label, _key in _LATENCY_MILESTONES:
        stat = stats[label]
        table.add_row(
            label,
            str(stat.count),
            _format_ms(stat.p50),
            _format_ms(stat.p90),
            _format_ms(stat.p95),
            _format_ms(stat.p99),
        )
    return table


@cli_command
def latency_command(
    bundle_path: Path = typer.Argument(
        ...,
        help=(
            "Path to a ZIP bundle archive (``.zip``, ``.bundle``, or "
            "``.easycat-bundle``) or a ``.sqlite`` journal."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Summarise critical-path latency percentiles across a bundle's turns.

    Rolls up the VAD endpoint → STT final → agent request → first token →
    TTS first byte milestone deltas per turn and reports p50/p90/p95/p99
    for each, without opening the debugger UI.  See docs/latency.md.
    """
    bundle = _load_bundle_or_journal(bundle_path, command="latency", json_output=json_output)
    turns = turn_waterfall(list(bundle.records()))
    percentiles = _latency_percentiles(turns)

    if json_output:
        emit_json(
            json_envelope(
                "latency",
                path=str(bundle_path),
                turns=turns,
                percentiles={label: stat.to_dict() for label, stat in percentiles.items()},
            )
        )
        raise typer.Exit(0)

    stderr_console.print(f"[bold]Latency[/] [cyan]{escape(str(bundle_path))}[/]")
    stderr_console.print()
    if not turns:
        warn("No turn-scoped records found; nothing to summarise.")
        return
    width = max(stdout_console.width, 120)
    _print_wide(_latency_turn_table(turns), width)
    stdout_console.print()
    _print_wide(_latency_percentile_table(percentiles), width)


# ── `easycat diff` ───────────────────────────────────────────────

# Transcript fields whose free-form text must be redacted before any diff
# row is emitted (JSON envelope or human table).  A regressed milestone or a
# cost delta is just numbers; only the transcript can carry a phone number or
# secret a caller said aloud.
_DIFF_TRANSCRIPT_TEXT_FIELDS = ("user_a", "user_b", "agent_a", "agent_b")


def _redact_diff_result(result: dict[str, Any]) -> dict[str, Any]:
    """Redact every transcript string in a ``diff_bundles`` result in place.

    The diff carries raw user/agent transcripts so the ``changed`` flag is
    meaningful, but the CLI must never print unredacted caller text.  Each
    transcript cell's text fields are passed through :func:`redact_text`;
    milestones, costs, and the summary are numbers and pass through untouched.
    """
    for turn in result.get("turns", ()):
        transcript = turn.get("transcript")
        if not isinstance(transcript, dict):
            continue
        for field_name in _DIFF_TRANSCRIPT_TEXT_FIELDS:
            value = transcript.get(field_name)
            if isinstance(value, str) and value:
                transcript[field_name] = redact_text(value)
    return result


def _diff_turn_filter(result: dict[str, Any], turn: str | None) -> dict[str, Any]:
    """Restrict a diff result to a single positional turn ``index`` (string).

    ``turn`` is matched against each turn's positional ``index`` so a user can
    drill into one before/after pair.  The summary is left intact so the
    worst-regression headline still reflects the whole run.
    """
    if turn is None:
        return result
    try:
        wanted = int(turn)
    except (TypeError, ValueError):
        wanted = None
    filtered = [t for t in result.get("turns", ()) if wanted is not None and t["index"] == wanted]
    return {**result, "turns": filtered}


def _diff_table(turns: list[dict[str, Any]]) -> Table:
    """Render the per-turn diff: regressed milestones in red, cost delta, drift.

    One row per aligned turn pair: the positional index, both turn ids, each
    milestone's ``a→b`` delta (red when it regressed), whether the transcript
    changed, and the cost delta.  Unmatched turns (a dropped or extra turn)
    render the missing side as ``-``.
    """
    table = Table(
        title="Two-source diff (ms / USD) — regressions in red",
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 1),
        title_justify="left",
    )
    table.add_column("idx", justify="right", no_wrap=True)
    table.add_column("turn (a→b)", no_wrap=True, overflow="fold")
    table.add_column("milestones (Δms)", overflow="fold")
    table.add_column("transcript", no_wrap=True)
    table.add_column("cost Δ", justify="right", no_wrap=True)
    for turn in turns:
        turn_a = turn.get("turn_id_a")
        turn_b = turn.get("turn_id_b")
        turn_label = f"{turn_a or '-'}→{turn_b or '-'}"
        if turn.get("unmatched"):
            turn_label = f"[yellow]{escape(turn_label)} (unmatched)[/]"
        else:
            turn_label = escape(turn_label)
        cells = []
        for name, cell in (turn.get("milestones") or {}).items():
            delta = cell.get("delta_ms")
            text = f"{name.removesuffix('_ms')}={_format_ms(delta)}"
            cells.append(f"[red]{escape(text)}[/]" if cell.get("regressed") else escape(text))
        milestone_text = ", ".join(cells) if cells else "[dim](none)[/]"
        transcript = turn.get("transcript") or {}
        drift = "[yellow]changed[/]" if transcript.get("changed") else "same"
        cost_delta = (turn.get("cost") or {}).get("delta")
        table.add_row(
            str(turn.get("index", "")),
            turn_label,
            milestone_text,
            drift,
            f"{cost_delta:+.4f}" if isinstance(cost_delta, int | float) else "-",
        )
    return table


@cli_command
def diff_command(
    path_a: Path = typer.Argument(
        ...,
        help="Baseline bundle or ``.sqlite`` journal (the 'before' run).",
    ),
    path_b: Path = typer.Argument(
        ...,
        help="Comparison bundle or ``.sqlite`` journal (the 'after' run).",
    ),
    turn: str | None = typer.Option(
        None,
        "--turn",
        help="Restrict the diff to a single positional turn index.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Diff two bundles turn-by-turn: milestone, transcript, and cost deltas.

    Aligns turns positionally (turn 0 of A vs turn 0 of B) and reports each
    milestone's ``b - a`` delta, whether it regressed (default: >10% AND >5ms
    slower), whether the transcript drifted, and the per-turn cost delta.
    The summary names the single worst regression across the whole run.
    Transcript text is redacted before it is printed.  See docs/latency.md.
    """
    bundle_a = _load_bundle_or_journal(path_a, command="diff", json_output=json_output)
    bundle_b = _load_bundle_or_journal(path_b, command="diff", json_output=json_output)

    result = diff_bundles(list(bundle_a.records()), list(bundle_b.records()))
    _redact_diff_result(result)
    result = _diff_turn_filter(result, turn)

    if json_output:
        emit_json(
            json_envelope(
                "diff",
                a=str(path_a),
                b=str(path_b),
                **result,
            )
        )
        raise typer.Exit(0)

    stderr_console.print(
        f"[bold]Diff[/] [cyan]{escape(str(path_a))}[/] → [cyan]{escape(str(path_b))}[/]"
    )
    stderr_console.print()
    turns = result["turns"]
    if not turns:
        warn("No aligned turns to diff.")
        return
    _print_wide(_diff_table(turns), max(stdout_console.width, 120))
    worst = result["summary"]["worst_regression"]
    if worst:
        stdout_console.print()
        stdout_console.print(
            f"[red]Worst regression[/]: turn {worst['index']} "
            f"{escape(str(worst['milestone']))} +{_format_ms(worst['delta_ms'])}ms"
        )


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
    """Return the turn's ``agent_final`` reply text, or ``None`` if absent."""
    for record in records:
        if record.get("name") != "agent_final":
            continue
        data = record.get("data")
        if isinstance(data, Mapping) and isinstance(data.get("text"), str):
            return data["text"]
        text = record.get("text")
        if isinstance(text, str):
            return text
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
            stdout_console.print(json.dumps(record_dict, sort_keys=False))
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
        while True:
            try:
                await _stream_follow(
                    view,
                    from_sequence=from_sequence,
                    errors_only=errors_only,
                    turn_id=turn,
                    json_output=json_output,
                )
                return
            except (FileNotFoundError, sqlite3.OperationalError):
                # The live writer may not have created the table yet, or the
                # file is mid-rotation; back off briefly and retry the tail.
                await asyncio.sleep(0.25)

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
