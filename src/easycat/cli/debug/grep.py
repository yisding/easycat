"""``easycat journal grep`` — full-text search a journal, redacting matches."""

from __future__ import annotations

from collections.abc import Mapping
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
    warn,
)
from easycat.cli.debug._common import (
    _load_bundle_or_journal,
    _print_wide,
    _record_detail,
    redact_free_text_data,
)
from easycat.validation.redaction import redact_text, redact_value


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
        # ``redact_value`` alone leaves free-form ``data`` keys (``text``,
        # ``transcript_text``, tool ``result``, …) to pattern matching, so a
        # matched utterance streamed verbatim from a command whose whole job is
        # a "fully redacted summary row" (gh 1102).
        "data": redact_free_text_data(
            cast(dict[str, Any], redact_value(record.get("data") or {}, "data"))
        ),
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
    from easycat.debugger._records import _SEARCH_SCAN_LIMIT, _search_records

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
