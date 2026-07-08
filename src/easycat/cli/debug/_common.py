"""Shared helpers for the ``easycat bundles`` / ``journal`` command modules.

The per-command modules (``export.py``, ``replay.py``, ``latency.py``,
``diff.py``, ``promote.py``, ``grep.py``, ``follow.py``) and the ``bundles.py``
facade all depend on this leaf: loading a bundle or SQLite journal, summarising
it, and the width-preserving table printer. Keeping these here lets each command
module import downward without a cycle back through the facade.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape
from rich.table import Table

from easycat.cli._output import emit_command_error, stdout_console
from easycat.debug._issues import build_issues
from easycat.debug._turn_timeline import record_wall_ns, safe_turn_id, turn_waterfall
from easycat.debug.bundle import (
    BundleError,
    BundleInUseError,
    BundleVersionError,
    RunBundle,
)
from easycat.validation.redaction import redact_text


def _format_ms(value: object) -> str:
    return f"{value:.1f}" if isinstance(value, int | float) else "-"


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
