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

import json
import shutil
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
from easycat.debug._turn_timeline import record_wall_ns, turn_waterfall
from easycat.debug.bundle import (
    BundleError,
    BundleInUseError,
    BundleVersionError,
    RunBundle,
    discover_bundles,
)
from easycat.runtime.replay import (
    ProviderVersionMismatchError,
    ReplayError,
    ReplayFidelity,
    ReplayResult,
    ReplaySideEffectBlocked,
    ReplaySpec,
    ToolReplayPolicy,
)
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


def _summarise_bundle(bundle: RunBundle) -> dict[str, object]:
    """Collect the high-signal fields we surface in ``bundles show``/``inspect``."""
    turn_ids: set[str] = set()
    errors = 0
    session_id = ""
    first_wall_ns: int | None = None
    last_wall_ns: int | None = None
    tool_calls = 0
    record_count = 0
    records = list(bundle.records())

    for record in records:
        record_count += 1
        if not session_id and record.get("session_id"):
            session_id = str(record["session_id"])
        turn_id = record.get("turn_id")
        if turn_id:
            turn_ids.add(str(turn_id))
        wall_ns = record_wall_ns(record)
        if wall_ns is not None:
            if first_wall_ns is None:
                first_wall_ns = wall_ns
            last_wall_ns = wall_ns
        if record.get("error"):
            errors += 1
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
        stderr_console.print(f"No bundles found under [cyan]{escape(scan_target)}[/].")
        stderr_console.print(
            "[dim]Use [cyan]EasyConfig(record_to=...)[/], "
            "[cyan]create_text_session(record_to=...)[/], or "
            "[cyan]session.export_debug_bundle()[/] to capture one.[/]"
        )
        raise typer.Exit(0)

    table = Table(show_header=True, header_style="bold", box=None, padding=(0, 1))
    table.add_column("path", no_wrap=False, overflow="fold")
    table.add_column("size", justify="right", no_wrap=True)
    table.add_column("modified", no_wrap=True)
    for entry in entries:
        table.add_row(
            escape(str(entry["path"])),
            _format_size(int(entry["size_bytes"])),
            _format_mtime(float(entry["mtime"])),
        )
    stdout_console.print(table)


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


def _show_bundle_summary(bundle_path: Path, *, json_output: bool) -> None:
    """Load and render the bundle or SQLite journal summary used by all aliases."""
    bundle = _load_bundle_or_journal(
        bundle_path,
        command="bundles_show",
        json_output=json_output,
    )
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
    duration_str = f"{float(duration):.1f}ms" if isinstance(duration, float) else "[dim]n/a[/]"
    table.add_row("duration", duration_str)
    table.add_row("tool_calls", str(summary["tool_calls"]))
    errors = int(summary["errors"])
    errors_fmt = f"[red]{errors}[/]" if errors else "0"
    table.add_row("errors", errors_fmt)
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
        stdout_console.print(_turn_waterfall_table(turns))


def _format_ms(value: object) -> str:
    return f"{value:.1f}" if isinstance(value, int | float) else "-"


def _turn_waterfall_table(turns: list[dict[str, Any]]) -> Table:
    """Render the per-turn latency waterfall for ``bundles show``/``inspect``.

    One row per turn: total wall time, the milestone deltas (VAD endpoint
    → STT final → agent first token → TTS first byte), and the per-stage
    spans as ``stage duration@offset``.  ``docs/latency.md`` explains how
    to read the numbers and which defaults to tune.
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
    table.add_column("stt→agent", justify="right", no_wrap=True)
    table.add_column("agent→tts", justify="right", no_wrap=True)
    table.add_column("vad→tts", justify="right", no_wrap=True)
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
            _format_ms(milestones.get("stt_final_to_agent_first_token_ms")),
            _format_ms(milestones.get("agent_first_token_to_tts_first_byte_ms")),
            _format_ms(milestones.get("vad_endpoint_to_tts_first_byte_ms")),
            escape(spans) if spans else "[dim](no stage spans)[/]",
        )
    return table


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
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Summarise a debug bundle or SQLite journal."""
    _show_bundle_summary(bundle_path, json_output=json_output)


@cli_command
def inspect_bundle(
    bundle_path: Path = typer.Argument(
        ...,
        help=(
            "Path to a ZIP bundle archive (``.zip``, ``.bundle``, or "
            "``.easycat-bundle``) or a ``.sqlite`` journal."
        ),
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable output."),
) -> None:
    """Friendly alias for ``easycat bundles show`` for bundles and SQLite journals."""
    _show_bundle_summary(bundle_path, json_output=json_output)


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
    stdout_console.print(escape(str(destination)))


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


bundles_app.command(name="list", help="List captured bundles and crash dumps under .easycat/.")(
    list_bundles
)
bundles_app.command(name="show", help="Summarise a debug bundle or SQLite journal.")(show_bundle)
bundles_app.command(name="export", help="Write a redacted context pack for a coding agent.")(
    export_bundle
)


__all__: list[str] = ["bundles_app", "export_bundle", "inspect_bundle", "replay_bundle"]
