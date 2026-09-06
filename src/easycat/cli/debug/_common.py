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
from easycat.debug._bundle_summary import summarise_annotations, summarise_bundle_records
from easycat.debug._issues import build_issues
from easycat.debug._turn_timeline import turn_waterfall
from easycat.debug.bundle import (
    BundleError,
    BundleInUseError,
    BundleVersionError,
    RunBundle,
)
from easycat.runtime.crash_sweep import crash_dump_artifact_root
from easycat.validation.redaction import REDACTED_TRANSCRIPT, redact_text

# Free-form STT/agent/tool text that ``SessionJournalSink`` stores under generic
# ``data`` keys.  None of these names are in ``redact_value``'s field-name
# allowlist (``UNSAFE_TEXT_FIELDS``), so they would only get pattern-based
# redaction and otherwise stream verbatim utterances — medical or account
# details, say.  Every CLI surface that emits a journal ``data`` dict replaces
# them wholesale with the shared transcript placeholder.
#
# They deliberately live here rather than in ``UNSAFE_TEXT_FIELDS``: that
# allowlist governs redaction everywhere, including bundle export, where
# ``journal_redaction="secrets"`` keeps this text on purpose so a bundle stays
# replayable.  This is a *diagnostic-output* suppression list (gh 1102).
FREE_TEXT_DATA_KEYS: tuple[str, ...] = (
    "delta",
    "note",
    "original_text",
    "prepared_text",
    "result",
    "stripped_text",
    "text",
    "text_spoken",
    "transcript_text",
)


def redact_free_text_data(data: dict[str, Any]) -> dict[str, Any]:
    """Replace every known free-text key in a journal ``data`` dict in place."""
    for key in FREE_TEXT_DATA_KEYS:
        if key in data:
            data[key] = REDACTED_TRANSCRIPT
    return data


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
    records = list(bundle.records())
    record_summary = summarise_bundle_records(records)

    return {
        **record_summary.to_dict(),
        # Per-turn latency waterfall: per-stage spans plus milestone deltas
        # (VAD endpoint → STT final → agent first token → TTS first byte),
        # shared with the debugger UI via ``debug/_turn_timeline``.
        "turns": turn_waterfall(records),
        # Severity-ranked heuristic findings (errors, tool failures, timeouts,
        # empty transcripts, slow milestones) shared with the debugger UI via
        # ``debug/_issues``.  Always present so the JSON shape is stable; the
        # ``--issues`` flag only toggles the human-readable card table.
        "issues": build_issues(records, artifact_resolver=bundle.artifact_blobs.get),
        # Reviewer verdict tally from the ``<bundle>.annotations.json``
        # sidecar when one exists (always present so the JSON shape is
        # stable; an absent sidecar yields all-zero counts).
        "annotations": summarise_annotations(annotations or {}).to_dict(),
        "provider_versions": dict(bundle.manifest.provider_versions),
        "journal_dropped_records": bundle.manifest.journal_dropped_records,
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

    New crash dumps own a sibling ``<id>.artifacts/`` snapshot.  Its presence
    deliberately suppresses the legacy shared ``artifacts/<id>/`` fallback:
    that directory may now belong to a later session that reused the id.
    Old dumps without the sibling directory retain the legacy lookup.
    """
    owned_root = crash_dump_artifact_root(sqlite_path)
    if owned_root.is_dir():
        return owned_root if any(owned_root.rglob("*.bin")) else None
    legacy_root = sqlite_path.parent.parent / "artifacts" / sqlite_path.stem
    return legacy_root if legacy_root.is_dir() else None


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
